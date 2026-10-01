from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import pathlib
from pathlib import Path
_SKIPPED: list[str] = []
_HERE = Path(__file__).resolve().parent

def _fake_tools_dir(home: Path) -> Path:
    tools = home / '.claude' / 'tools' / 'ccpick'
    if sys.platform == 'darwin':
        stable = home / '.local' / 'share' / 'tyk-fixed-egress' / 'app' / 'ccpick'
        stable.mkdir(parents=True)
        (stable / 'ccpick.py').write_text('', encoding='utf-8')
        (stable / 'ccpick_coordination.py').write_text('', encoding='utf-8')
        (stable / 'quota-policy.json').write_text('{}', encoding='utf-8')
        tools.parent.mkdir(parents=True)
        tools.symlink_to(stable, target_is_directory=True)
    else:
        tools.mkdir(parents=True)
    for directory in (tools, home / 'bin'):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'ccpick_account_context.py').write_text('def manager():\n    return None\ndef dispatch(*args):\n    return None\n', encoding='utf-8')
    return tools
_spec = importlib.util.spec_from_file_location('decide', _HERE / 'claude-autoswitch-decide.py')
decide = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decide)
_hspec = importlib.util.spec_from_file_location('helper', _HERE / 'claude-autoswitch-helper.py')
helper = importlib.util.module_from_spec(_hspec)
_hspec.loader.exec_module(helper)

def _isolate(tmpdir: str) -> None:
    decide.SAMPLES = Path(tmpdir) / 'samples.json'

def _fresh(tmpdir: str) -> None:
    _isolate(tmpdir)
    if decide.SAMPLES.exists():
        decide.SAMPLES.unlink()

def check_rate_needs_two_samples(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0002@example.com', {'5h': 10.0}, 1000.0)
    assert decide.burn_eta('account-0002@example.com', {'5h': 10.0}, 1000.0) == (None, None)
    decide.record_sample('account-0002@example.com', {'5h': 27.0}, 1060.0)
    rate, eta = decide.burn_eta('account-0002@example.com', {'5h': 27.0}, 1060.0)
    assert abs(rate - 17.0) < 0.01, rate
    assert abs(eta - (100 - 27) / 17.0 * 60) < 0.5, eta
    return 3

def check_stale_cache_is_not_zero_rate(tmp: str) -> int:
    _fresh(tmp)
    for _ in range(5):
        decide.record_sample('account-0002@example.com', {'5h': 40.0}, 2000.0)
    assert len(decide._read(decide.SAMPLES)['samples']) == 1
    assert decide.burn_eta('account-0002@example.com', {'5h': 40.0}, 2100.0) == (None, None)
    return 2

def check_worst_window_wins(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0002@example.com', {'5h': 10.0, '7d': 60.0}, 3000.0)
    decide.record_sample('account-0002@example.com', {'5h': 11.0, '7d': 78.0}, 3060.0)
    rate, eta = decide.burn_eta('account-0002@example.com', {'5h': 11.0, '7d': 78.0}, 3060.0)
    assert abs(rate - 18.0) < 0.01, rate
    assert abs(eta - (100 - 78) / 18.0 * 60) < 0.5, eta
    return 2

def check_uncounted_model_window_does_not_drive_rate(tmp: str) -> int:
    samples = ((3000.0, {'5h': 10.0, '7d': 5.0, 'Fable': 60.0}), (3060.0, {'5h': 11.0, '7d': 5.0, 'Fable': 78.0}))
    with _Env(CCSWITCH_MODELS=None):
        _fresh(tmp)
        for t, u in samples:
            decide.record_sample('account-0002@example.com', u, t)
        rate, _ = decide.burn_eta('account-0002@example.com', samples[-1][1], 3060.0)
        assert abs(rate - 1.0) < 0.01, rate
        assert 'Fable' in decide._read(decide.SAMPLES)['samples'][-1][1]
    with _Env(CCSWITCH_MODELS='Fable'):
        rate, _ = decide.burn_eta('account-0002@example.com', samples[-1][1], 3060.0)
        assert abs(rate - 18.0) < 0.01, rate
    return 3

def check_pace_ignores_uncounted_for_gate_and_hot_ceiling(tmp: str) -> int:
    rate = 2.0
    with _Env(CCSWITCH_MODELS=None):
        assert abs(decide.eta_to_gate({'5h': 10.0, 'Fable': 92.0}, rate) - 2400) < 1
        _fresh(tmp)
        decide.record_sample('account-0002@example.com', {'5h': 8.0, 'Fable': 90.0}, 1000.0)
        decide.record_sample('account-0002@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        pace = decide.pace_of('account-0002@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        assert pace['nextCheckS'] == decide.NEXT_CHECK_MAX_S, pace
        assert pace['urgent'] is False, pace
    with _Env(CCSWITCH_MODELS='Fable'):
        assert abs(decide.eta_to_gate({'5h': 10.0, 'Fable': 92.0}, rate) - 150) < 1
        pace = decide.pace_of('account-0002@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        assert pace['nextCheckS'] <= decide.NEXT_CHECK_MAX_HOT_S, pace
    return 4

def check_burst_not_averaged(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0002@example.com', {'5h': 10.0}, 4000.0)
    decide.record_sample('account-0002@example.com', {'5h': 10.0}, 4030.0)
    decide.record_sample('account-0002@example.com', {'5h': 30.0}, 4090.0)
    rate, _ = decide.burn_eta('account-0002@example.com', {'5h': 30.0}, 4090.0)
    assert abs(rate - 20.0) < 0.01, rate
    return 1

def check_window_expires(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0002@example.com', {'5h': 10.0}, 5000.0)
    decide.record_sample('account-0002@example.com', {'5h': 30.0}, 5060.0)
    now = 5060.0 + decide.RATE_WINDOW_S + 10
    assert decide.burn_eta('account-0002@example.com', {'5h': 30.0}, now) == (None, None)
    return 1

def check_parse_active_email(tmp: str) -> int:
    assert decide.parse_active_email('Status: account-0003@example.com (not managed)') == 'account-0003@example.com'
    assert decide.parse_active_email('Status: #3 (account-0004@example.com) 5h 12%') == 'account-0004@example.com'
    assert decide.parse_active_email('Status: not signed in') == ''
    assert decide.parse_active_email('') == ''
    return 4

def check_interval_tracks_decision_gate_not_wall(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0002@example.com', {'5h': 84.0}, 1000.0)
    decide.record_sample('account-0002@example.com', {'5h': 85.0}, 1060.0)
    pace = decide.pace_of('account-0002@example.com', {'5h': 85.0}, 1060.0)
    assert abs(pace['etaS'] - 900) < 2, pace
    assert abs(pace['nextCheckS'] - 75) < 2, pace
    assert pace['burnRate'] == 1.0, pace
    _fresh(tmp)
    decide.record_sample('account-0005@example.com', {'5h': 94.0}, 1000.0)
    decide.record_sample('account-0005@example.com', {'5h': 95.0}, 1060.0)
    pace = decide.pace_of('account-0005@example.com', {'5h': 95.0}, 1060.0)
    assert abs(pace['etaS'] - 300) < 2, pace
    assert abs(pace['nextCheckS'] - 30) < 2, pace
    _fresh(tmp)
    decide.record_sample('account-0006@example.com', {'5h': 50.0}, 1000.0)
    pace = decide.pace_of('account-0006@example.com', {'5h': 50.0}, 1000.0)
    assert pace['nextCheckS'] is None and pace['etaS'] is None, pace
    return 7

def check_offline_round_keeps_water_level(tmp: str) -> int:
    dest = os.path.join(tmp, 'status.json')
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '85|85|40|12|0', '107|429|0.7|account-0007@example.com'])
    first = json.loads(open(dest, encoding='utf-8').read())
    assert first['usedPct'] == 85.0 and first['nextCheckS'] == 107.0, first
    helper.cmd_write([dest, 'offline', '连不上 claude.ai', '下一轮自己重试', '90', '', ''])
    off = json.loads(open(dest, encoding='utf-8').read())
    assert off['state'] == 'offline', off
    assert off['usedPct'] == 85.0, off
    assert off['win5h'] == 85.0 and off['win7d'] == 40.0, off
    assert off['activeEmail'] == 'account-0007@example.com', off
    assert off['nextCheckS'] is None and off['etaS'] is None, off
    assert off['burnRate'] is None, off
    dest2 = os.path.join(tmp, 'status-fresh.json')
    helper.cmd_write([dest2, 'offline', '连不上', '', '90', '', ''])
    fresh = json.loads(open(dest2, encoding='utf-8').read())
    assert fresh['usedPct'] is None and fresh['activeEmail'] is None, fresh
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '30|30|10|5|0', '300|1200|0.2|account-0008@example.com'])
    now = json.loads(open(dest, encoding='utf-8').read())
    assert now['usedPct'] == 30.0 and now['activeEmail'] == 'account-0008@example.com', now
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '20|10|20|99|0|7d|Fable|0', '300|1200|0.2|account-0008@example.com'])
    b = json.loads(open(dest, encoding='utf-8').read())
    assert (b['binding'], b['modelName'], b['modelCounted']) == ('7d', 'Fable', False), b
    helper.cmd_write([dest, 'offline', '连不上', '', '90', '', ''])
    b2 = json.loads(open(dest, encoding='utf-8').read())
    assert (b2['binding'], b2['modelName'], b2['modelCounted']) == ('7d', 'Fable', False), b2
    helper.cmd_write([dest2, 'ok', '在盯着', '', '90', '30|30|10|5|0', ''])
    b3 = json.loads(open(dest2, encoding='utf-8').read())
    assert b3['binding'] is None and b3['modelCounted'] is None, b3
    return 13

def check_gate_is_every_threshold_not_just_a_hundred(tmp: str) -> int:
    rate = 1.0
    assert abs(decide.eta_to_gate({'5h': 85.0}, rate) - 300) < 1
    assert abs(decide.eta_to_gate({'5h': 91.0}, rate) - 360) < 1
    assert abs(decide.eta_to_gate({'5h': 96.0}, rate) - 60) < 1
    assert decide.next_check_s(decide.eta_to_gate({'5h': 96.0}, rate)) == 20.0
    assert abs(decide.eta_to_gate({'5h': 98.0}, rate) - 120) < 1
    assert abs(decide.eta_to_gate({'5h': 90.0}, rate) - 420) < 1
    assert abs(decide.eta_to_gate({'5h': 97.0}, rate) - 180) < 1
    assert abs(decide.eta_to_gate({'5h': 10.0, '7d': 96.0}, rate) - 60) < 1
    return 8

def check_hot_zone_never_relaxes(tmp: str) -> int:
    rate = 1.0

    def nxt(used):
        return decide.next_check_s(decide.eta_to_gate({'5h': float(used)}, rate), used)
    for used in (90, 91, 93, 97, 98):
        assert nxt(used) <= decide.NEXT_CHECK_MAX_HOT_S, (used, nxt(used))
    assert nxt(50) == decide.NEXT_CHECK_MAX_S
    assert nxt(70) == decide.NEXT_CHECK_MAX_S
    assert nxt(96) == 20.0, nxt(96)
    prev = None
    for used in range(50, 100):
        v = nxt(used)
        if prev is not None:
            assert v <= max(prev, decide.NEXT_CHECK_MAX_HOT_S), (used, prev, v)
        prev = v
    return 9

def check_removed_account_is_not_a_candidate(tmp: str) -> int:
    orig_cache, orig_seq = (decide.CACHE, decide.SEQ)
    decide.CACHE = pathlib.Path(tmp) / 'usage-ghost.json'
    decide.SEQ = pathlib.Path(tmp) / 'seq-ghost.json'
    try:

        def _acct(email, pct):
            return {'email': email, 'lastAttemptAt': 1000.0, 'lastGood': {'five_hour': {'pct': pct, 'resets_at': None}}}
        decide.CACHE.write_text(json.dumps({'accounts': {'3': _acct('account-0009@example.com', 5.0), '4': _acct('account-0010@example.com', 50.0)}}), encoding='utf-8')
        decide.SEQ.write_text(json.dumps({'accounts': {'4': {'email': 'account-0010@example.com'}}}), encoding='utf-8')
        emails = [r['email'] for r in decide.rows_from_cache()]
        assert emails == ['account-0010@example.com'], emails
        best, _ = decide.pick(decide.rows_from_cache(), set())
        assert best is not None and best['email'] == 'account-0010@example.com', best
        decide.SEQ = pathlib.Path(tmp) / 'seq-absent.json'
        emails = sorted((r['email'] for r in decide.rows_from_cache()))
        assert emails == ['account-0009@example.com', 'account-0010@example.com'], emails
    finally:
        decide.CACHE, decide.SEQ = (orig_cache, orig_seq)
    return 3

class _Env:

    def __init__(self, **kv):
        self._kv = kv
        self._old = {}

    def __enter__(self):
        for k, v in self._kv.items():
            self._old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False

class _Patch:

    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        try:
            for k, v in self._attrs.items():
                self._orig[k] = getattr(decide, k)
                setattr(decide, k, v)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(decide, k, v)
        return False

def _drow(email: str, slot: str, used: dict, resets: str='', error=None, age: float=5.0) -> dict:
    counted = decide.usage.counted_windows(dict(used))
    binding = max(counted, key=counted.get) if counted else ''
    return {'slot': slot, 'email': email, 'used': dict(used), 'counted': counted, 'worstUsed': counted[binding] if binding else None, 'binding': binding, 'resetsAt': resets, 'age': age, 'error': error}
_MOVE = object()
_LAST_PLAN_FAKE = None

class _SwitchFake:

    def __init__(self, me: str, switch_rc: int=0, land_on=_MOVE, after_switch=None):
        self.current = me
        self.switch_rc = switch_rc
        self.land_on = land_on
        self.after_switch = after_switch
        self.calls: list[list[str]] = []

    def cswap(self, args, timeout=90):
        self.calls.append(list(args))
        if args[:1] == ['switch'] and self.switch_rc == 0:
            if self.land_on is _MOVE:
                self.current = args[1]
            elif self.land_on is not None:
                self.current = self.land_on
            if self.after_switch:
                self.after_switch(self.current)
        rc = self.switch_rc if args[:1] == ['switch'] else 0
        return subprocess.CompletedProcess(args, rc, stdout='', stderr='')

class _NoSleep:
    time = staticmethod(__import__('time').time)

    @staticmethod
    def sleep(_s):
        return None

def check_soonest_skips_past_and_dead(tmp: str) -> int:
    now = 1790000000.0
    past = '2026-09-01T09:59:59+00:00'
    dead_soon = '2026-09-22T00:00:00+00:00'
    real = '2026-09-23T12:00:00+00:00'
    rows = [_drow('account-0007@example.com', '1', {'5h': 99.0}, resets='2026-09-23T15:00:00+00:00'), _drow('account-0009@example.com', '2', {'7d': 100.0}, resets=past), _drow('account-0011@example.com', '3', {'5h': 100.0}, resets=dead_soon, error='invalid_grant'), _drow('account-0010@example.com', '4', {'5h': 96.0}, resets=real)]
    fake = _SwitchFake('account-0007@example.com')
    with _Patch(rows_from_cache=lambda: rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, is_vacated=lambda e: False, _cswap=fake.cswap, SAMPLES=Path(tmp) / 'samples-soonest.json', time=type('T', (), {'time': staticmethod(lambda: now), 'sleep': staticmethod(lambda s: None)})):
        res = decide.decide(dry_run=False)
    assert res['action'] == 'blocked', res
    assert res['soonest'] == 'account-0010@example.com', res
    assert res['soonestAt'] == real, res
    return 3

def check_ledger_only_after_really_leaving(tmp: str) -> int:
    ledger = Path(tmp) / 'ledger-leave.json'
    rows = [_drow('account-0007@example.com', '1', {'5h': 98.5}, resets='2026-09-30T00:00:00+00:00'), _drow('account-0012@example.com', '2', {'5h': 20.0})]
    rows3 = rows + [_drow('account-0013@example.com', '3', {'5h': 30.0})]
    cases = ((1, _MOVE, False), (0, None, False), (0, '', False), (0, 'account-0013@example.com', True), (0, _MOVE, True))
    for rc, land, expect_in_ledger in cases:
        if ledger.exists():
            ledger.unlink()
        fake = _SwitchFake('account-0007@example.com', switch_rc=rc, land_on=land)
        with _Patch(rows_from_cache=lambda: rows3, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=ledger, SAMPLES=Path(tmp) / 'samples-leave.json', time=_NoSleep, _record_history=lambda frm, to: None):
            res = decide.decide(dry_run=False)
            in_ledger = 'account-0007@example.com' in decide.load_ledger()
        assert in_ledger is expect_in_ledger, (rc, land, res, decide._read(ledger))
    return len(cases)

def check_burned_landing_is_not_a_dead_end(tmp: str) -> int:
    ledger = Path(tmp) / 'ledger-burned.json'
    for with_t2 in (True, False):
        if ledger.exists():
            ledger.unlink()
        state = {'account-0007@example.com': 92.0, 'account-0014@example.com': 50.0, 'account-0015@example.com': 85.0}
        if not with_t2:
            del state['account-0015@example.com']

        def rows():
            return [_drow(e, str(i), {'5h': p}) for i, (e, p) in enumerate(state.items())]

        def landed(email):
            if email == 'account-0014@example.com':
                state['account-0014@example.com'] = 99.0
        fake = _SwitchFake('account-0007@example.com', after_switch=landed)
        with _Patch(rows_from_cache=rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=ledger, SAMPLES=Path(tmp) / 'samples-burned.json', time=_NoSleep, _record_history=lambda frm, to: None):
            res = decide.decide(dry_run=False)
            led = decide.load_ledger()
        res.setdefault('activeEmail', res.get('to') or res.get('active') or '')
        assert res['activeEmail'] == fake.current, (res, fake.current)
        assert fake.current != 'account-0014@example.com', (with_t2, res)
        assert 'account-0014@example.com' in led, led
        if with_t2:
            assert res['action'] == 'switched' and res['to'] == 'account-0015@example.com', res
        else:
            assert res['action'] == 'stay' and fake.current == 'account-0007@example.com', res
            assert '切回原号' in res.get('why', ''), res
            assert 'account-0007@example.com' not in led, led
    return 7

def _landing_run(tmp: str, name: str, cached: dict, landed_as: dict, land_on=_MOVE, samples=None, me='account-0007@example.com', land_once=False):
    ledger = Path(tmp) / ('ledger-%s.json' % name)
    sample_file = Path(tmp) / ('samples-%s.json' % name)
    for f in (ledger, sample_file):
        if f.exists():
            f.unlink()
    if samples:
        sample_file.write_text(json.dumps({'email': me, 'samples': samples}), encoding='utf-8')
    state = dict(cached)

    def rows():
        return [_drow(e, str(i), {'5h': v}) for i, (e, v) in enumerate(state.items())]

    def after(email):
        if email in landed_as:
            state[email] = landed_as[email]
        if land_once:
            fake.land_on = _MOVE
    fake = _SwitchFake(me, land_on=land_on, after_switch=after)
    hist = []
    with _Patch(rows_from_cache=rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=ledger, SAMPLES=sample_file, time=_NoSleep, _record_history=lambda *a: hist.append(a)):
        res = decide.decide(dry_run=False)
        led = decide.load_ledger()
    res.setdefault('activeEmail', res.get('to') or res.get('active') or '')
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    return (res, fake.current, led, switches, hist)

def check_stranded_on_burned_goes_home(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'stranded', {'account-0007@example.com': 92.0, 'account-0014@example.com': 50.0, 'account-0015@example.com': 60.0, 'account-0016@example.com': 70.0}, {'account-0014@example.com': 99.5, 'account-0015@example.com': 99.5, 'account-0016@example.com': 99.5})
    assert cur == 'account-0007@example.com' and res['activeEmail'] == 'account-0007@example.com', (res, cur, sw)
    assert sw[-1] == 'account-0007@example.com' and len(sw) <= decide.MAX_SWITCH_TRIES, sw
    assert 'account-0007@example.com' not in led, led
    assert 'account-0014@example.com' in led, led
    return 4

def check_home_is_not_held_to_target_ceiling(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'ceiling', {'account-0007@example.com': 96.0, 'account-0014@example.com': 50.0}, {'account-0014@example.com': 99.0})
    assert cur == 'account-0007@example.com', (res, cur, sw)
    assert 'account-0007@example.com' not in led and 'account-0014@example.com' in led, led
    return 2

def check_third_account_landing_can_recover(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'third', {'account-0007@example.com': 92.0, 'account-0014@example.com': 80.0, 'account-0017@example.com': 96.0, 'account-0015@example.com': 88.0}, {}, land_on='account-0017@example.com', land_once=True)
    assert cur == 'account-0015@example.com' and res['action'] == 'switched' and (res['to'] == 'account-0015@example.com'), (res, sw)
    assert 'account-0007@example.com' in led, led
    res, cur, led, sw, _ = _landing_run(tmp, 'third-home', {'account-0007@example.com': 92.0, 'account-0014@example.com': 80.0, 'account-0017@example.com': 96.0}, {}, land_on='account-0017@example.com', land_once=True)
    assert cur == 'account-0007@example.com' and res['action'] == 'stay', (res, sw)
    assert 'account-0007@example.com' not in led, led
    return 5

def check_move_resets_pace_and_reports_switch(tmp: str) -> int:
    now = __import__('time').time()
    res, cur, led, sw, hist = _landing_run(tmp, 'pace', {'account-0007@example.com': 99.0, 'account-0014@example.com': 50.0}, {'account-0014@example.com': 99.5}, samples=[[now - 120, {'5h': 89.0}], [now - 60, {'5h': 94.0}], [now - 5, {'5h': 99.0}]])
    assert cur == 'account-0014@example.com', (res, cur)
    assert res['activeEmail'] == 'account-0014@example.com', res
    assert res.get('burnRate') is None and res.get('etaS') is None, res
    assert res.get('urgent') is False, res
    assert res.get('from') == 'account-0007@example.com' and res.get('to') == 'account-0014@example.com', res
    assert 'account-0007@example.com' not in led, led
    assert hist and hist[-1][-1] == 'account-0014@example.com', hist
    return 7

def check_net_move_that_stays_is_a_switch(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'netmove', {'account-0007@example.com': 99.0, 'account-0014@example.com': 50.0, 'account-0017@example.com': 96.0}, {}, land_on='account-0017@example.com', land_once=True)
    assert cur == 'account-0017@example.com', (res, sw)
    assert res['action'] == 'switched' and res['to'] == 'account-0017@example.com', res
    assert res['activeEmail'] == 'account-0017@example.com', res
    assert 'account-0007@example.com' in led, led
    return 4

def check_history_identity_is_final_account(tmp: str) -> int:
    hist_file = Path(tmp) / 'history-multihop.jsonl'
    got_identity = []
    urows = [{'slot': '2', 'email': 'account-0015@example.com', 'active': False, 'error': None, 'fetched_at': 1.0, 'windows': {'5h': {'pct': 85.0, 'resets_at': ''}}}]

    def fake_collect(**kw):
        got_identity.append(kw.get('identity'))
        return [dict(r, active=r['email'] == kw.get('identity')) for r in urows]
    orig_hist, orig_collect = (decide.usage.HISTORY, decide.usage.collect)
    decide.usage.HISTORY, decide.usage.collect = (hist_file, fake_collect)
    try:
        state = {'account-0007@example.com': 92.0, 'account-0014@example.com': 50.0, 'account-0015@example.com': 85.0}

        def rows():
            return [_drow(e, str(i), {'5h': v}) for i, (e, v) in enumerate(state.items())]

        def after(email):
            if email == 'account-0014@example.com':
                state['account-0014@example.com'] = 99.0
        fake = _SwitchFake('account-0007@example.com', after_switch=after)
        with _Patch(rows_from_cache=rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=Path(tmp) / 'ledger-multihop.json', SAMPLES=Path(tmp) / 'samples-multihop.json', time=_NoSleep):
            res = decide.decide(dry_run=False)
    finally:
        decide.usage.HISTORY, decide.usage.collect = (orig_hist, orig_collect)
    assert res['action'] == 'switched' and res['to'] == 'account-0015@example.com', res
    assert got_identity == ['account-0015@example.com'], got_identity
    rec = json.loads(hist_file.read_text(encoding='utf-8').splitlines()[-1])
    assert 'account-0014@example.com' in rec['reason'] and 'account-0015@example.com' in rec['reason'], rec
    assert rec['accounts'][0]['active'] is True, rec
    return 4

def check_load_usage_rejects_stale_module(tmp: str) -> int:
    import shutil as _sh
    import sys as _sys
    home = Path(tmp) / 'stalehome'
    (home / 'bin').mkdir(parents=True)
    dst = home / 'bin' / 'claude-autoswitch-decide.py'
    _sh.copy(_HERE / 'claude-autoswitch-decide.py', dst)
    tools = _fake_tools_dir(home)
    (tools / 'ccpick_usage.py').write_text('def collect():\n    return []\n', encoding='utf-8')
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1')
    r = subprocess.run([_sys.executable, str(dst), '--help'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode != 0, r
    assert 'fetch_error_kind' in (r.stderr or ''), r.stderr
    (tools / 'ccpick_usage.py').write_text("def fetch_error_kind(e):\n    return ''\ndef snapshot(rows, reason=''):\n    pass\ndef collect(now=None, identity=None):\n    return []\n", encoding='utf-8')
    r = subprocess.run([_sys.executable, str(dst), '--help'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode != 0 and 'counted_windows' in (r.stderr or ''), (r.returncode, r.stderr)
    _sh.copy(_HERE.parent / 'ccpick_usage.py', home / 'bin' / 'ccpick_usage.py')
    r = subprocess.run([_sys.executable, str(dst), '--help'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr[-400:])
    return 4

def check_load_usage_from_home_bin_copy(tmp: str) -> int:
    import shutil as _sh
    import sys as _sys
    home = Path(tmp) / 'fakehome'
    (home / 'bin').mkdir(parents=True)
    dst = home / 'bin' / 'claude-autoswitch-decide.py'
    _sh.copy(_HERE / 'claude-autoswitch-decide.py', dst)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1')
    r = subprocess.run([_sys.executable, str(dst), '--help'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode != 0, r
    assert 'ccpick_usage' in (r.stderr or ''), r.stderr
    tools = _fake_tools_dir(home)
    _sh.copy(_HERE.parent / 'ccpick_usage.py', tools / 'ccpick_usage.py')
    r = subprocess.run([_sys.executable, str(dst), '--help'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    return 3

def check_failed_fetch_does_not_fake_fresh_data(tmp: str) -> int:
    cache = Path(tmp) / 'usage-age.json'
    samples = Path(tmp) / 'samples-age.json'
    t0 = 1790000000.0
    fut = '2099-01-01T00:00:00+00:00'

    def write(fetched, attempted, pct):
        cache.write_text(json.dumps({'accounts': {'7': {'email': 'account-0007@example.com', 'fetchedAt': fetched, 'lastAttemptAt': attempted, 'lastGood': {'five_hour': {'pct': pct, 'resets_at': fut}}}}}), encoding='utf-8')
    clock = {'now': t0}
    fake_time = type('T', (), {'time': staticmethod(lambda: clock['now']), 'sleep': staticmethod(lambda s: None)})
    with _Patch(CACHE=cache, SEQ=Path(tmp) / 'seq-age-absent.json', SAMPLES=samples, time=fake_time):
        for i, pct in enumerate((64.0, 70.0, 76.0)):
            clock['now'] = t0 + 60 * i + 5
            write(t0 + 60 * i, t0 + 60 * i, pct)
            row, = decide.rows_from_cache()
            decide.record_sample('account-0007@example.com', row['used'], clock['now'] - row['age'])
        for j in (3, 4):
            clock['now'] = t0 + 60 * j + 5
            write(t0 + 120, t0 + 60 * j, 76.0)
            row, = decide.rows_from_cache()
            decide.record_sample('account-0007@example.com', row['used'], clock['now'] - row['age'])
        assert row['age'] >= 120, row
        rate, eta = decide.burn_eta('account-0007@example.com', row['used'], clock['now'])
    assert rate is not None and abs(rate - 6.0) < 0.01, (rate, eta)
    return 2

def check_soonest_waits_for_every_blocking_window(tmp: str) -> int:
    import datetime as _dt
    now = 1790000000.0
    iso = lambda s: _dt.datetime.fromtimestamp(now + s, _dt.timezone.utc).isoformat()
    cache = Path(tmp) / 'usage-soonest.json'

    def acct(email, fh, fh_at, sd, sd_at):
        return {'email': email, 'fetchedAt': now - 10, 'lastAttemptAt': now - 10, 'lastGood': {'five_hour': {'pct': fh, 'resets_at': iso(fh_at)}, 'seven_day': {'pct': sd, 'resets_at': iso(sd_at)}}}
    cache.write_text(json.dumps({'accounts': {'1': acct('account-0002@example.com', 100.0, 3600, 97.0, 3 * 86400), '2': acct('account-0005@example.com', 100.0, 7200, 50.0, 5 * 86400), '3': acct('account-0006@example.com', 100.0, 1800, 100.0, 2 * 86400)}}), encoding='utf-8')
    fake_time = type('T', (), {'time': staticmethod(lambda: now), 'sleep': staticmethod(lambda s: None)})
    (Path(tmp) / 'seq-soonest-absent.json').write_text(json.dumps({'accounts': {slot: {'email': row['email']} for slot, row in json.loads(cache.read_text(encoding='utf-8'))['accounts'].items()}}), encoding='utf-8')
    with _Patch(CACHE=cache, SEQ=Path(tmp) / 'seq-soonest-absent.json', time=fake_time):
        rows = decide.rows_from_cache()
        best = decide.soonest_recovery(rows, now)
        only_c = decide.soonest_recovery([r for r in rows if r['email'] == 'account-0006@example.com'], now)
    assert best is not None and best['email'] == 'account-0005@example.com', best
    assert best['recoversAt'] == iso(7200), best
    assert only_c['recoversAt'] == iso(2 * 86400), only_c
    return 3

def check_transient_fetch_errors_keep_target(tmp: str) -> int:
    ok_errors = (None, 'timeout', 'network', 'http-429', 'http-500', 'http-529', 'bad-response', 'transient', 'RemoteDisconnected', 'invalid_client', 'refresh-failed', 'consume-busy')
    bad_errors = ('invalid_grant', 'no_refresh_token', 'no-access-token', 'http-401', 'http-403', 'something-new')
    with _Patch(is_vacated=lambda e: False):
        for err in ok_errors:
            best, _ = decide.pick([_drow('account-0012@example.com', '2', {'5h': 30.0}, error=err)], set())
            assert best is not None, err
        for err in bad_errors:
            best, _ = decide.pick([_drow('account-0012@example.com', '2', {'5h': 30.0}, error=err)], set())
            assert best is None, err
    kinds = {e: decide.usage.fetch_error_kind(e) for e in ok_errors + bad_errors}
    for err in ok_errors:
        assert kinds[err] in ('', 'transient'), (err, kinds[err])
    for err in bad_errors:
        assert kinds[err] in ('dead', 'unknown'), (err, kinds[err])
    return len(ok_errors) * 2 + len(bad_errors) * 2

def check_usable_implies_autoswitch_target(tmp: str) -> int:
    levels = (0.0, 50.0, 89.0, 90.0, 94.0, 95.0, 96.0, 97.0, 99.0, 100.0)
    total = 0
    for models in ('', 'Fable', 'all'):
        with _Env(CCSWITCH_MODELS=models):
            n = _usable_implies_target_grid(levels, tmp)
        assert n > 0, models
        total += n
    return total

def _usable_implies_target_grid(levels, tmp: str) -> int:
    import itertools
    import time as _t
    future = '2099-01-01T00:00:00+00:00'
    now = _t.time()
    combos, accounts = ({}, {})
    for i, (p5, p7, pf, err) in enumerate(itertools.product(levels, levels, (None, 0.0, 96.0, 99.0), (None, 'timeout', 'invalid_grant'))):
        email = 't%account-0018@example.com' % i
        lg = {'five_hour': {'pct': p5, 'resets_at': future}, 'seven_day': {'pct': p7, 'resets_at': future}}
        if pf is not None:
            lg['scoped'] = [{'name': 'Fable', 'pct': pf, 'resets_at': future}]
        accounts[str(i)] = {'email': email, 'lastGood': lg, 'lastError': err, 'fetchedAt': now, 'lastAttemptAt': now}
        combos[email] = (p5, p7, pf, err)
    cache = Path(tmp) / 'usage-invariant.json'
    cache.write_text(json.dumps({'accounts': accounts}), encoding='utf-8')
    no_seq = Path(tmp) / 'seq-invariant-absent.json'
    no_seq.write_text(json.dumps({'accounts': {slot: {'email': row['email']} for slot, row in accounts.items()}}), encoding='utf-8')
    u = decide.usage
    orig = (u.CACHE, u.SEQ, u.known_status)
    u.CACHE, u.SEQ, u.known_status = (cache, no_seq, lambda: {})
    try:
        urows = u.collect(identity='')
    finally:
        u.CACHE, u.SEQ, u.known_status = orig
    with _Patch(CACHE=cache, SEQ=no_seq, is_vacated=lambda e: False):
        drows = {r['email']: r for r in decide.rows_from_cache()}
    assert len(urows) == len(drows) == len(combos), (len(urows), len(drows), len(combos))
    n = 0
    with _Patch(is_vacated=lambda e: False):
        for urow in urows:
            ok, why = u.is_usable(urow)
            if ok:
                best, _ = decide.pick([drows[urow['email']]], set())
                assert best is not None, (os.environ.get('CCSWITCH_MODELS'), combos[urow['email']], why)
                n += 1
    return n

def check_expired_window_not_counted(tmp: str) -> int:
    orig_cache, orig_seq = (decide.CACHE, decide.SEQ)
    decide.CACHE = Path(tmp) / 'usage-expired.json'
    decide.SEQ = Path(tmp) / 'seq-expired-absent.json'
    try:
        decide.CACHE.write_text(json.dumps({'accounts': {'4': {'email': 'account-0002@example.com', 'lastAttemptAt': 1000.0, 'lastGood': {'five_hour': {'pct': 100.0, 'resets_at': '2020-01-01T00:00:00+00:00'}, 'seven_day': {'pct': 30.0, 'resets_at': '2099-01-01T00:00:00+00:00'}}}}}), encoding='utf-8')
        row, = decide.rows_from_cache()
    finally:
        decide.CACHE, decide.SEQ = (orig_cache, orig_seq)
    assert row['worstUsed'] == 30.0 and row['binding'] == '7d', row
    assert '5h' not in row['used'], row
    return 2

def check_switch_is_recorded_in_usage_history(tmp: str) -> int:
    hist = Path(tmp) / 'history-switch.jsonl'
    rows = [_drow('account-0007@example.com', '1', {'5h': 98.5}), _drow('account-0012@example.com', '2', {'5h': 20.0})]
    urows = [{'slot': '1', 'email': 'account-0007@example.com', 'active': False, 'error': None, 'fetched_at': 1234.5, 'windows': {'5h': {'pct': 98.5, 'resets_at': ''}}}]
    fake = _SwitchFake('account-0007@example.com')
    orig_hist, orig_collect = (decide.usage.HISTORY, decide.usage.collect)
    seen_identity = []
    decide.usage.HISTORY, decide.usage.collect = (hist, lambda **kw: seen_identity.append(kw.get('identity')) or urows)
    try:
        with _Patch(rows_from_cache=lambda: rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, is_vacated=lambda e: False, _cswap=fake.cswap, LEDGER=Path(tmp) / 'ledger-hist.json', SAMPLES=Path(tmp) / 'samples-hist.json', time=_NoSleep):
            res = decide.decide(dry_run=False)
    finally:
        decide.usage.HISTORY, decide.usage.collect = (orig_hist, orig_collect)
    assert res['action'] == 'switched', res
    lines = hist.read_text(encoding='utf-8').splitlines() if hist.exists() else []
    assert len(lines) == 1, lines
    rec = json.loads(lines[0])
    assert 'account-0007@example.com' in rec['reason'] and 'account-0012@example.com' in rec['reason'], rec
    assert rec['accounts'][0]['fetched_at'] == 1234.5, rec
    assert seen_identity == ['account-0012@example.com'], seen_identity
    return 4

def _iso_in(**kw) -> str:
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(**kw)).isoformat()

def _write_pool(tmp: str, name: str, accounts, roster=None):
    import time as _t
    now = _t.time()
    cache = {'schemaVersion': 2, 'accounts': {}}
    for slot, email, used, resets, err, age in accounts:
        lg = {}
        for key, win in (('five_hour', '5h'), ('seven_day', '7d')):
            if win in used:
                w = {'pct': float(used[win])}
                if resets.get(win):
                    w['resets_at'] = resets[win]
                lg[key] = w
        scoped = [dict({'name': n, 'pct': float(p)}, **{'resets_at': resets[n]} if resets.get(n) else {}) for n, p in used.items() if n not in ('5h', '7d')]
        if scoped:
            lg['scoped'] = scoped
        cache['accounts'][slot] = {'email': email, 'lastGood': lg, 'lastError': err, 'fetchedAt': now - age, 'lastAttemptAt': now - age}
    seq = {'activeAccountNumber': 0, 'accounts': {s: {'email': e} for s, e, *_ in (roster if roster is not None else accounts)}}
    cpath = Path(tmp) / ('pool-%s-usage.json' % name)
    spath = Path(tmp) / ('pool-%s-sequence.json' % name)
    cpath.write_text(json.dumps(cache), encoding='utf-8')
    spath.write_text(json.dumps(seq), encoding='utf-8')
    return (cpath, spath)

def _run_pool(tmp: str, name: str, accounts, active: str, roster=None, dry_run=True, switch_rc=None, samples=None):
    cpath, spath = _write_pool(tmp, name, accounts, roster)
    ledger = Path(tmp) / ('pool-%s-ledger.json' % name)
    sample_file = Path(tmp) / ('pool-%s-samples.json' % name)
    for f in (ledger, sample_file):
        if f.exists():
            f.unlink()
    if samples:
        sample_file.write_text(json.dumps({'email': active, 'samples': samples}), encoding='utf-8')
    fake = _SwitchFake(active, switch_rc=switch_rc if switch_rc is not None else 0)
    cswap = fake.cswap if switch_rc is not None else lambda *a, **k: None
    empty_cfg = Path(tmp) / 'configs-empty'
    empty_cfg.mkdir(exist_ok=True)
    orig_cfg = decide.usage.CONFIGS
    decide.usage.CONFIGS = empty_cfg
    try:
        with _Patch(CACHE=cpath, SEQ=spath, LEDGER=ledger, SAMPLES=sample_file, active_email=lambda: fake.current, refresh=lambda slots, budget=2: True, _cswap=cswap, time=_NoSleep, _record_history=lambda *a: None):
            res = decide.decide(dry_run=dry_run)
            led = decide.load_ledger()
    finally:
        decide.usage.CONFIGS = orig_cfg
    return (res, led, fake)
_REAL_0915 = [('1', 'account-0019@example.com', {'5h': 0, '7d': 100, 'Fable': 91}, {}, None, 300), ('2', 'account-0020@example.com', {'5h': 0, '7d': 96, 'Fable': 97}, {}, None, 300), ('6', 'account-0021@example.com', {'5h': 99, '7d': 68, 'Fable': 88}, {}, None, 200), ('8', 'account-0004@example.com', {'5h': 2, '7d': 95, 'Fable': 95}, {}, None, 100), ('9', 'account-0003@example.com', {'5h': 82, '7d': 96, 'Fable': 100}, {}, None, 300)]

def check_no_better_target_is_stay_not_blocked(tmp: str) -> int:
    n = 0
    for models in ('', 'all'):
        with _Env(CCSWITCH_MODELS=models):
            res, led, _ = _run_pool(tmp, 'stay-%s' % (models or 'default'), _REAL_0915, 'account-0004@example.com')
        assert res['action'] == 'stay', (models, res)
        assert '更好' in (res.get('why') or ''), (models, res)
        assert res.get('used') == 95.0, (models, res)
        n += 3
    import contextlib as _cl
    import io as _io
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = ['decide', '--dry-run']
    try:
        with _Patch(decide=lambda dry: dict(res)), _cl.redirect_stdout(_io.StringIO()):
            rc = decide.main()
    finally:
        _sys.argv = old_argv
    assert rc == 2, rc
    return n + 1

def check_truly_exhausted_still_blocked(tmp: str) -> int:
    pool = [('8', 'account-0004@example.com', {'5h': 99, '7d': 99, 'Fable': 99}, {'5h': _iso_in(hours=2), '7d': _iso_in(days=3)}, None, 100), ('9', 'account-0003@example.com', {'5h': 99, '7d': 99, 'Fable': 100}, {'5h': _iso_in(hours=1), '7d': _iso_in(days=2)}, None, 300)]
    res, _, _ = _run_pool(tmp, 'exhausted', pool, 'account-0004@example.com')
    assert res['action'] == 'blocked', res
    assert 'soonest' in res and 'soonestAt' in res, res
    assert res['soonest'] == 'account-0003@example.com', res
    return 3

def check_urgent_with_nowhere_to_go_stays(tmp: str) -> int:
    import time as _t
    now = _t.time()
    pool = [('1', 'account-0007@example.com', {'5h': 70, '7d': 30}, {}, None, 5), ('2', 'account-0002@example.com', {'5h': 96, '7d': 30}, {}, None, 50), ('3', 'account-0005@example.com', {'5h': 10, '7d': 99}, {}, None, 50)]
    res, _, _ = _run_pool(tmp, 'urgent', pool, 'account-0007@example.com', samples=[[now - 125, {'5h': 20.0, '7d': 30.0}], [now - 65, {'5h': 40.0, '7d': 30.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'stay' and '更好' in (res.get('why') or ''), res
    return 2

def check_all_switches_fail_but_current_usable_stays(tmp: str) -> int:
    pool3 = [('1', 'account-0007@example.com', {'5h': 92, '7d': 30}, {}, None, 5), ('2', 'account-0014@example.com', {'5h': 50, '7d': 30}, {}, None, 5), ('3', 'account-0015@example.com', {'5h': 60, '7d': 30}, {}, None, 5), ('4', 'account-0016@example.com', {'5h': 70, '7d': 30}, {}, None, 5)]
    res, led, fake = _run_pool(tmp, 'fail3', pool3, 'account-0007@example.com', dry_run=False, switch_rc=1)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert len(switches) == decide.MAX_SWITCH_TRIES, switches
    assert res['action'] == 'stay' and '切换没成功' in (res.get('why') or ''), res
    assert res.get('switchFailed') is True, res
    assert 'account-0007@example.com' not in led, led
    res, led, _ = _run_pool(tmp, 'fail1', pool3[:2], 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'stay' and '试过 1 个' in (res.get('why') or ''), res
    assert res.get('switchFailed') is True, res
    hot = [('1', 'account-0007@example.com', {'5h': 98, '7d': 30}, {}, None, 5)] + pool3[1:]
    res, _, _ = _run_pool(tmp, 'fail-hot', hot, 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'blocked', res
    solo = [('1', 'account-0007@example.com', {'5h': 93, '7d': 30}, {}, None, 5), ('2', 'account-0014@example.com', {'5h': 99, '7d': 30}, {}, None, 5)]
    res, _, _ = _run_pool(tmp, 'nowhere', solo, 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'stay' and (not res.get('switchFailed')), res
    return 9

def check_soonest_ignores_uncounted_windows(tmp: str) -> int:
    import datetime as _dt
    import time as _t
    pool = [('3', 'account-0022@example.com', {'5h': 38, '7d': 99, 'Fable': 67}, {'7d': _iso_in(days=-3)}, 'invalid_grant', 692795), ('8', 'account-0004@example.com', {'5h': 50, '7d': 99, 'Fable': 99}, {'7d': _iso_in(hours=9)}, None, 100), ('9', 'account-0003@example.com', {'5h': 50, '7d': 99, 'Fable': 100}, {'7d': _iso_in(hours=3)}, None, 300)]
    with _Env(CCSWITCH_MODELS=None):
        res, _, _ = _run_pool(tmp, 'soonest3', pool, 'account-0004@example.com')
    assert res['action'] == 'blocked', res
    assert res.get('soonest') == 'account-0003@example.com', res
    at = decide._iso_ts(res.get('soonestAt') or '')
    assert at is not None and at > _t.time(), res
    with _Env(CCSWITCH_MODELS='all'):
        res, _, _ = _run_pool(tmp, 'soonest3-all', pool, 'account-0004@example.com')
    assert res['action'] == 'blocked' and res.get('soonest') == '', res
    return 5

def check_soonest_cross_offset(tmp: str) -> int:
    import datetime as _dt
    later_utc = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)
    sooner = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)).astimezone(_dt.timezone(_dt.timedelta(hours=9)))
    pool = [('8', 'account-0002@example.com', {'5h': 10, '7d': 99}, {'7d': later_utc.isoformat()}, None, 100), ('9', 'account-0005@example.com', {'5h': 10, '7d': 99}, {'7d': sooner.isoformat()}, None, 100)]
    res, _, _ = _run_pool(tmp, 'tz', pool, 'account-0002@example.com')
    assert res['action'] == 'blocked' and res.get('soonest') == 'account-0005@example.com', res
    return 1

def check_roster_slot_and_email(tmp: str) -> int:
    ghost = [('3', 'account-0022@example.com', {'5h': 0, '7d': 10, 'Fable': 10}, {}, None, 100), ('8', 'account-0004@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100)]
    res, _, _ = _run_pool(tmp, 'ghost', ghost, 'account-0004@example.com', roster=ghost[1:])
    assert res['action'] == 'stay' and res.get('to') != 'account-0022@example.com', res
    n = 1

    def both(cache, seq):
        with _Patch(CACHE=cache, SEQ=seq):
            d_emails = sorted((r['email'] for r in decide.rows_from_cache()))
        u = decide.usage
        orig = (u.CACHE, u.SEQ)
        u.CACHE, u.SEQ = (cache, seq)
        try:
            u_emails = sorted((r['email'] for r in u.collect(identity='')))
        finally:
            u.CACHE, u.SEQ = orig
        assert d_emails == u_emails, (d_emails, u_emails)
        return d_emails
    cache, seq = _write_pool(tmp, 'roster', ghost, roster=ghost[1:])
    assert both(cache, seq) == ['account-0004@example.com']
    cache, seq = _write_pool(tmp, 'moved', ghost, roster=[('3', 'account-0023@example.com'), ('8', 'account-0004@example.com')])
    assert both(cache, seq) == ['account-0004@example.com']
    cache, seq = _write_pool(tmp, 'case', ghost, roster=[('3', 'account-0022@example.com'), ('8', 'account-0004@example.com')])
    assert len(both(cache, seq)) == 2
    seq.write_text('{ this is not json', encoding='utf-8')
    assert len(both(cache, seq)) == 2
    seq.write_text(json.dumps({'accounts': {}}), encoding='utf-8')
    assert both(cache, seq) == []
    return n + 5

def check_model_window_display_only_by_default(tmp: str) -> int:
    pool = [('2', 'account-0020@example.com', {'5h': 0, '7d': 64, 'Fable': 97}, {}, None, 300), ('8', 'account-0004@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100), ('9', 'account-0003@example.com', {'5h': 82, '7d': 96, 'Fable': 100}, {}, None, 300)]
    with _Env(CCSWITCH_MODELS=None):
        res, _, _ = _run_pool(tmp, 'fable-default', pool, 'account-0004@example.com')
    assert res['action'] == 'stay' and res['used'] == 69.0, res
    assert res.get('binding') == '7d', res
    assert (res.get('windows') or {}).get('Fable') == 95.0, res
    with _Env(CCSWITCH_MODELS=None):
        fin = decide._finalize(dict(res))
    assert fin['countedWindows'] == ['5h', '7d'], fin
    with _Env(CCSWITCH_MODELS='Fable'):
        fin = decide._finalize(dict(res))
    assert fin['countedWindows'] == ['5h', '7d', 'Fable'], fin
    return 5

def check_ccswitch_models_opt_in(tmp: str) -> int:
    pool = [('2', 'account-0020@example.com', {'5h': 0, '7d': 64, 'Fable': 97}, {}, None, 300), ('8', 'account-0004@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100)]
    n = 0
    for models, want_used, want_binding in (('Fable', 95.0, 'Fable'), ('fable', 95.0, 'Fable'), ('FABLE,Opus', 95.0, 'Fable'), ('all', 95.0, 'Fable'), ('Opus', 69.0, '7d'), ('none', 69.0, '7d')):
        with _Env(CCSWITCH_MODELS=models):
            res, _, _ = _run_pool(tmp, 'optin', pool, 'account-0004@example.com')
        assert res.get('used') == want_used and res.get('binding') == want_binding, (models, res)
        n += 1
    return n

def check_ledger_entry_on_uncounted_window_is_short_hold(tmp: str) -> int:
    import time as _t
    ledger = Path(tmp) / 'ledger-uncounted.json'
    ledger.write_text(json.dumps({'account-0024@example.com': {'at': _t.time() - 20 * 60, 'used': 99.0, 'binding': 'Fable', 'resetsAt': _iso_in(days=6), 'why': '已满'}, 'account-0025@example.com': {'at': _t.time() - 20 * 60, 'used': 99.0, 'binding': '7d', 'resetsAt': _iso_in(days=6), 'why': '已满'}}), encoding='utf-8')
    with _Patch(LEDGER=ledger):
        with _Env(CCSWITCH_MODELS=None):
            off = decide.is_vacated('account-0024@example.com')
            seven = decide.is_vacated('account-0025@example.com')
        with _Env(CCSWITCH_MODELS='Fable'):
            on = decide.is_vacated('account-0024@example.com')
    assert off is False, 'Fable 不算数了, 20 分钟前的记录早该过了 10 分钟短时'
    assert seven is True
    assert on is True
    return 3

def check_helper_loads_sibling_usage(tmp: str) -> int:
    import shutil as _sh
    import sys as _sys
    import time as _t
    home = Path(tmp) / 'helperhome'
    (home / 'bin').mkdir(parents=True)
    for f in ('claude-autoswitch-helper.py',):
        _sh.copy(_HERE / f, home / 'bin' / f)
    _sh.copy(_HERE.parent / 'ccpick_usage.py', home / 'bin' / 'ccpick_usage.py')
    tools = _fake_tools_dir(home)
    (tools / 'ccpick_usage.py').write_text('def collect(now=None):\n    return []\n', encoding='utf-8')
    cache_dir = home / '.claude-swap-backup' / 'cache'
    cache_dir.mkdir(parents=True)
    now = _t.time()
    (cache_dir / 'usage.json').write_text(json.dumps({'accounts': {'1': {'email': 'account-0024@example.com', 'fetchedAt': now, 'lastGood': {'five_hour': {'pct': 10.0, 'resets_at': _iso_in(hours=2)}, 'seven_day': {'pct': 20.0, 'resets_at': _iso_in(days=3)}, 'scoped': [{'name': 'Fable', 'pct': 99.0, 'resets_at': _iso_in(days=2)}]}}}}), encoding='utf-8')
    (home / '.claude-swap-backup' / 'sequence.json').write_text(json.dumps({'accounts': {'1': {'email': 'account-0024@example.com'}}}), encoding='utf-8')
    (home / 'Library' / 'Logs').mkdir(parents=True)
    (home / 'Library' / 'Logs' / 'claude-autoswitch-status.json').write_text(json.dumps({'ts': now, 'activeEmail': 'account-0024@example.com'}), encoding='utf-8')
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1')
    env.pop('CCSWITCH_MODELS', None)
    env.pop('LOCALAPPDATA', None)
    r = subprocess.run([_sys.executable, str(home / 'bin' / 'claude-autoswitch-helper.py'), 'accounts'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stdout[-300:], r.stderr[-300:])
    out = json.loads(r.stdout)
    acct, = out['accounts']
    assert 'usable' in acct and 'blocked' in acct, acct
    assert acct['usable'] is True and acct['headroom'] == 80.0, acct
    assert any((w['name'] == 'Fable' and w['used'] == 99.0 for w in acct['windows'])), acct
    env['CCSWITCH_MODELS'] = 'all'
    r = subprocess.run([_sys.executable, str(home / 'bin' / 'claude-autoswitch-helper.py'), 'accounts'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
    acct, = json.loads(r.stdout)['accounts']
    assert acct['usable'] is False and acct['headroom'] == 1.0, acct
    return 6

def check_next_check_clamped(tmp: str) -> int:
    assert decide.next_check_s(None) is None
    assert decide.next_check_s(4.0) == decide.NEXT_CHECK_MIN_S
    assert decide.next_check_s(100000.0) == decide.NEXT_CHECK_MAX_S
    assert abs(decide.next_check_s(240.0) - 60.0) < 0.01
    return 4

def check_slow_burn_still_used_to_the_end(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0005@example.com', {'5h': 88.8}, 6000.0)
    decide.record_sample('account-0005@example.com', {'5h': 89.0}, 6060.0)
    pace = decide.pace_of('account-0005@example.com', {'5h': 89.0}, 6060.0)
    assert pace['urgent'] is False, pace
    assert abs(pace['nextCheckS'] - 75) < 2, pace
    _fresh(tmp)
    decide.record_sample('account-0006@example.com', {'5h': 49.8}, 6000.0)
    decide.record_sample('account-0006@example.com', {'5h': 50.0}, 6060.0)
    pace = decide.pace_of('account-0006@example.com', {'5h': 50.0}, 6060.0)
    assert pace['urgent'] is False, pace
    assert pace['nextCheckS'] == decide.NEXT_CHECK_MAX_S, pace
    return 4

def _replay(start_used: float, rate: float) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        t = 0.0
        interval = decide.POST_SWITCH_CHECK_S
        for _ in range(200):
            used = min(100.0, start_used + rate * (t / 60.0))
            decide.record_sample('account-0026@example.com', {'5h': used}, t)
            pace = decide.pace_of('account-0026@example.com', {'5h': used}, t)
            if used >= decide.CONSIDER_AT or pace['urgent']:
                return {'t': t, 'used': used, 'pace': pace, 'headroomS': (100.0 - used) / rate * 60.0}
            interval = pace['nextCheckS'] or decide.POST_SWITCH_CHECK_S
            t += interval
        raise AssertionError('跑了 200 轮还没决定切 —— 判据坏了')

def _tray_fallback_interval(used):
    if used is None:
        return 60.0
    eta = max(0.0, 100.0 - used) / 20.0 * 60.0
    return min(max(eta / 4.0, 20.0), 300.0)

def _replay_with_outage(start_used: float, rate: float, outage_at_round: int, keep_level: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        t = 0.0
        interval = decide.POST_SWITCH_CHECK_S
        for i in range(200):
            used = min(100.0, start_used + rate * (t / 60.0))
            if i == outage_at_round:
                t += _tray_fallback_interval(used if keep_level else None)
                continue
            decide.record_sample('account-0026@example.com', {'5h': used}, t)
            pace = decide.pace_of('account-0026@example.com', {'5h': used}, t)
            if used >= decide.CONSIDER_AT or pace['urgent']:
                return {'t': t, 'used': used}
            t += pace['nextCheckS'] or _tray_fallback_interval(used)
        raise AssertionError('跑了 200 轮还没决定切')

def check_replay_2026_09_20_outage(tmp: str) -> int:
    kept = _replay_with_outage(85.0, 2.8, outage_at_round=2, keep_level=True)
    lost = _replay_with_outage(85.0, 2.8, outage_at_round=2, keep_level=False)
    assert kept['used'] < decide.EXHAUSTED_AT, kept
    assert lost['used'] < decide.EXHAUSTED_AT, lost
    assert kept['used'] < 93.0, kept
    assert kept['t'] <= lost['t'], (kept, lost)
    fast = _replay_with_outage(85.0, 20.0, outage_at_round=1, keep_level=True)
    assert fast['used'] < 100.0, fast
    lost_fast = _replay_with_outage(85.0, 20.0, outage_at_round=1, keep_level=False)
    assert lost_fast['used'] > fast['used'], (fast, lost_fast)
    return 6

def check_replay_wall_2(tmp: str) -> int:
    r = _replay(start_used=10.0, rate=17.0)
    assert r['used'] < 80.0, r
    assert r['headroomS'] >= decide.SWITCH_COST_S, r
    level_only_headroom = (100.0 - decide.CONSIDER_AT) / 17.0 * 60.0
    assert level_only_headroom < decide.SWITCH_COST_S, level_only_headroom
    assert r['headroomS'] > level_only_headroom * 2, (r, level_only_headroom)
    return 4

def check_replay_wall_1(tmp: str) -> int:
    r = _replay(start_used=31.0, rate=20.0)
    assert r['used'] < 80.0, r
    assert r['headroomS'] >= decide.SWITCH_COST_S, r
    assert r['t'] < 3.5 * 60, r
    return 3

def check_replay_normal_burn_not_twitchy(tmp: str) -> int:
    r = _replay(start_used=10.0, rate=2.4)
    assert r['used'] >= decide.CONSIDER_AT, r
    return 1

def check_rate_at_real_cache_cadence(tmp: str) -> int:
    _fresh(tmp)
    t0, step = (10000.0, 195.0)
    for i, used in enumerate([42.0, 45.0, 46.0, 52.0, 57.0]):
        decide.record_sample('account-0006@example.com', {'5h': used}, t0 + i * step)
    now = t0 + 4 * step
    rate, eta = decide.burn_eta('account-0006@example.com', {'5h': 57.0}, now)
    assert rate is not None, '真实缓存节奏下算不出速率 —— RATE_WINDOW_S 又太小了'
    assert abs(rate - 6.0 / (step / 60)) < 0.05, rate
    assert decide.pace_of('account-0006@example.com', {'5h': 57.0}, now)['nextCheckS'] is not None
    return 3

def check_identity_lookup_is_bounded(tmp: str) -> int:
    import inspect
    spec = importlib.util.spec_from_file_location('ccpick_usage', _HERE.parent / 'ccpick_usage.py')
    usage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usage)
    default = inspect.signature(usage.live_identity).parameters['timeout'].default
    assert default <= 5.0, default
    helper = (_HERE / 'claude-autoswitch-helper.py').read_text(encoding='utf-8')
    assert '_active_email_from_status' in helper
    assert 'mod.live_identity = lambda' in helper
    return 3

def check_panel_never_forks_on_open(tmp: str) -> int:
    sw = (_HERE / 'menubar-main.swift').read_text(encoding='utf-8')
    calls = [ln.strip() for ln in sw.splitlines() if 'loadAccounts()' in ln and (not ln.lstrip().startswith('//')) and ('func loadAccounts()' not in ln)]
    assert len(calls) == 1, calls
    fetch_pos = sw.index('private func fetchAccounts')
    call_pos = sw.index(calls[0])
    assert fetch_pos < call_pos, 'loadAccounts() 不在 fetchAccounts 里'
    assert 'DispatchQueue.global' in sw[fetch_pos:call_pos], '它没被挪到后台队列上'
    assert 'accountCache ?? ([], nil)' in sw
    return 4

def check_no_byte_slicing_of_chinese(tmp: str) -> int:
    sh = (_HERE / 'claude-account-autoswitch.sh').read_text(encoding='utf-8')
    offenders = [ln.strip() for ln in sh.splitlines() if 'cut -c' in ln and (not ln.lstrip().startswith('#'))]
    assert not offenders, offenders
    msg = '活跃账号 ' + '汉' * 60
    try:
        got = subprocess.run(['cut', '-c1-120'], input=msg.encode('utf-8'), capture_output=True, env={'LC_ALL': 'C', 'LANG': 'C', 'PATH': '/usr/bin:/bin'}).stdout
    except OSError as e:
        _SKIPPED.append('check_no_byte_slicing_of_chinese 的病因复现半段（本机没有 POSIX 的 cut: %s）—— 防腐闸本身已跑过' % type(e).__name__)
        return 1
    try:
        got.decode('utf-8')
        raise AssertionError('C locale 下 cut -c 竟然是按字符切的? 这条前提变了, 重写本用例')
    except UnicodeDecodeError:
        pass
    return 2

def check_status_write_survives_bad_bytes(tmp: str) -> int:
    import sys as _sys
    '★状态文件是显示用的, 不许因为一句话里有坏字节就停止更新★ (2026-09-05)\n\n    当时的症状: helper 的 json 编码写到一半抛异常 ⇒ 状态文件停在旧值 +\n    Logs 里堆半截 `.tmp.<pid>` ⇒ 而 stderr 被 `2>/dev/null` 吞掉, 整件事无声。\n    '
    if os.name != 'posix':
        _SKIPPED.append('check_status_write_survives_bad_bytes（需要 POSIX: argv 塞裸 bytes 在 Windows 上无法表达）')
        return 0
    dest = Path(tmp) / 'status-badbytes.json'
    for p in Path(tmp).glob('status-badbytes.json.tmp.*'):
        p.unlink()
    r = subprocess.run([_sys.executable, str(_HERE / 'claude-autoswitch-helper.py'), 'write', str(dest), 'error', '决策出错', b'\xe6\x88', '90', '', ''], capture_output=True)
    assert r.returncode == 0, (r.returncode, r.stderr[:200])
    obj = json.loads(dest.read_text(encoding='utf-8'))
    assert obj['state'] == 'error' and obj['message'] == '决策出错', obj
    assert obj['threshold'] == 90.0, obj
    leftovers = list(Path(tmp).glob('status-badbytes.json.tmp.*'))
    assert not leftovers, leftovers
    return 4

def _row(email: str, slot: str, used: float) -> dict:
    return {'slot': slot, 'email': email, 'used': {'5h': used, '7d': used}, 'worstUsed': used, 'binding': '5h', 'resetsAt': '', 'age': 5.0, 'error': None}

class _Fake:

    def __init__(self, add_rc: int=0, add_out: str='Added Account 8: account-0023@example.com'):
        self.calls: list[list[str]] = []
        self.added = False
        self._add_rc = add_rc
        self._add_out = add_out

    def cswap(self, args, timeout=90):
        self.calls.append(list(args))
        if args == ['add']:
            if self._add_rc == 0:
                self.added = True
            return subprocess.CompletedProcess(args, self._add_rc, stdout=self._add_out, stderr='')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    def rows(self, visible_after_add: bool=True):
        out = [_row('account-0027@example.com', '3', 50.0)]
        if self.added and visible_after_add:
            out.append(_row('account-0023@example.com', '8', 40.0))
        return out

def _wire(fake: _Fake, tmp: str, visible_after_add: bool=True) -> None:
    _fresh(tmp)
    decide._cswap = fake.cswap
    decide.rows_from_cache = lambda: fake.rows(visible_after_add)
    decide.active_email = lambda: 'account-0023@example.com'
    decide.refresh = lambda slots, budget: True
    decide.is_vacated = lambda email: False

def check_unenrolled_account_is_auto_enrolled(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp)
    res = decide.decide(dry_run=False)
    assert ['add'] in fake.calls, fake.calls
    assert res['action'] != 'error', res
    assert res.get('active') == 'account-0023@example.com', res
    return 3

def check_enroll_records_profile_mapping(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp)
    seen = []
    decide.claim_login_profile = lambda: 'Profile 2'
    decide.record_profile_account = lambda prof, email: seen.append((prof, email))
    try:
        res = decide.decide(dry_run=False)
    finally:
        decide.claim_login_profile = lambda: None
        decide.record_profile_account = lambda *a: None
    assert res['action'] != 'error', res
    assert seen == [('Profile 2', 'account-0023@example.com')], seen
    return 2

def check_mapping_failure_never_blocks_enroll(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp)

    def _boom(*a, **k):
        raise RuntimeError('磁盘满了')
    decide.claim_login_profile = _boom
    try:
        res = decide.decide(dry_run=False)
    finally:
        decide.claim_login_profile = lambda: None
    assert ['add'] in fake.calls, fake.calls
    assert res['action'] != 'error', res
    return 2

def check_dry_run_never_enrolls(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp)
    res = decide.decide(dry_run=True)
    assert ['add'] not in fake.calls, fake.calls
    assert res['action'] == 'error', res
    assert 'account-0023@example.com' in res['why'], res
    return 3

def check_failed_enroll_says_why(tmp: str) -> int:
    fake = _Fake(add_rc=1, add_out='not logged in')
    _wire(fake, tmp)
    res = decide.decide(dry_run=False)
    assert ['add'] in fake.calls, fake.calls
    assert res['action'] == 'error', res
    assert 'not logged in' in res['why'], res
    return 3

def check_enrolled_but_cache_lags_is_not_an_error(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp, visible_after_add=False)
    res = decide.decide(dry_run=False)
    assert ['add'] in fake.calls, fake.calls
    assert res['action'] != 'error', res
    assert '入库' in res['why'], res
    return 3

def _run_plan_pool(tmp: str, name: str, accounts, active: str, ledger=None, samples=None, dry_run=True, switch_rc=0, allow_switch=True):
    import time as _t
    cpath, spath = _write_pool(tmp, name, [(s, e, u, r, None, 5) for s, e, u, r, _o, _p in accounts])
    seq = {'activeAccountNumber': 0, 'accounts': {s: {'email': e, 'organizationUuid': o} for s, e, _u, _r, o, _p in accounts}}
    spath.write_text(json.dumps(seq), encoding='utf-8')
    cfg = Path(tmp) / ('configs-%s' % name)
    cfg.mkdir(exist_ok=True)
    for f in cfg.glob('*'):
        f.unlink()
    snap = {'20x': {'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_20x'}, '5x': {'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_5x'}, 'Team': {'organizationType': 'claude_team', 'seatTier': 'team_standard', 'organizationRateLimitTier': 'default_raven'}}
    for s, e, _u, _r, o, plan in accounts:
        if plan:
            (cfg / ('.claude-config-%s-%s.json' % (s, e))).write_text(json.dumps({'oauthAccount': dict(snap[plan], emailAddress=e, organizationUuid=o, profileFetchedAt=1790000000000)}), encoding='utf-8')
    led = Path(tmp) / ('pool-%s-ledger.json' % name)
    smp = Path(tmp) / ('pool-%s-samples.json' % name)
    for f in (led, smp):
        if f.exists():
            f.unlink()
    if ledger:
        led.write_text(json.dumps(ledger), encoding='utf-8')
    if samples:
        smp.write_text(json.dumps({'email': active, 'samples': samples}), encoding='utf-8')
    u = decide.usage
    orig = u.CONFIGS
    u.CONFIGS = cfg
    fake = _SwitchFake(active, switch_rc=switch_rc)
    global _LAST_PLAN_FAKE
    _LAST_PLAN_FAKE = fake
    try:
        with _Patch(CACHE=cpath, SEQ=spath, LEDGER=led, SAMPLES=smp, active_email=lambda: fake.current, refresh=lambda slots, budget=2: True, _cswap=(lambda *a, **k: None) if dry_run else fake.cswap, time=_NoSleep, _record_history=lambda *a: None):
            res = decide.decide(dry_run=dry_run, allow_switch=allow_switch)
            rows = {r['email']: r for r in decide.rows_from_cache()}
            book = decide.load_ledger()
    finally:
        u.CONFIGS = orig
    if dry_run:
        return (res, rows)
    return (res, rows, book)

def _pool_0925(me_windows: dict, me_email: str, later: dict | None=None) -> list:
    T, A, B, C, D = ('org-team', 'org-a', 'org-b', 'org-c', 'org-d')
    pool = [('4', 'account-0028@example.com', {'5h': 66, '7d': 90}, {'5h': _iso_in(hours=2), '7d': _iso_in(days=5)}, A, '20x'), ('5', 'account-0029@example.com', {'5h': 0, '7d': 100}, {'7d': _iso_in(days=2)}, B, '20x'), ('6', 'account-0030@example.com', {'5h': 0, '7d': 91}, {'7d': _iso_in(days=2, hours=9)}, C, '20x'), ('7', 'account-0031@example.com', {'5h': 0, '7d': 90}, {'7d': _iso_in(days=4, hours=16)}, D, '20x'), ('8', 'account-0032@example.com', {'5h': 91, '7d': 73}, {'5h': _iso_in(minutes=40), '7d': _iso_in(days=4)}, T, '20x'), ('9', 'account-0033@example.com', {'5h': 0, '7d': 96}, {'7d': _iso_in(hours=6)}, T, 'Team'), ('10', 'account-0034@example.com', {'5h': 0, '7d': 0}, {}, T, 'Team'), ('11', 'account-0035@example.com', {'5h': 0, '7d': 80}, {'7d': _iso_in(days=1, hours=16)}, T, 'Team'), ('12', 'account-0036@example.com', {'5h': 0, '7d': 90}, {'7d': _iso_in(hours=18)}, T, 'Team')]
    later = dict(later or {}, **{me_email: me_windows})
    return [(s, e, dict(later.get(e, u)), r, o, p) for s, e, u, r, o, p in pool]

def check_plan_info_reads_cswap_data(tmp: str) -> int:
    u = decide.usage
    d = Path(tmp) / 'plan-info'
    cfg = d / 'configs'
    cfg.mkdir(parents=True, exist_ok=True)
    seq = {'accounts': {'4': {'email': 'account-0037@example.com', 'organizationUuid': 'A'}, '8': {'email': 'account-0038@example.com', 'organizationUuid': 'T'}, '10': {'email': 'account-0039@example.com', 'organizationUuid': 'T'}, '13': {'email': 'account-0040@example.com', 'organizationUuid': 'S'}, '14': {'email': 'account-0041@example.com', 'organizationUuid': 'N'}, '15': {'email': 'account-0042@example.com', 'organizationUuid': 'W'}, '16': {'email': 'account-0043@example.com', 'organizationUuid': 'F'}, '17': {'email': 'account-0044@example.com', 'organizationUuid': 'P'}, '18': {'email': 'account-0045@example.com', 'organizationUuid': 'L'}}}
    (d / 'sequence.json').write_text(json.dumps(seq), encoding='utf-8')
    snaps = {('4', 'account-0037@example.com'): {'emailAddress': 'account-0037@example.com', 'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_20x'}, ('18', 'account-0045@example.com'): {'emailAddress': 'account-0045@example.com', 'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_20x'}, ('8', 'account-0038@example.com'): {'emailAddress': 'account-0038@example.com', 'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_20x'}, ('13', 'account-0040@example.com'): {'emailAddress': 'account-0040@example.com', 'organizationType': 'claude_team', 'seatTier': 'team_tier_1'}, ('15', 'account-0042@example.com'): {'emailAddress': 'account-0046@example.com', 'organizationRateLimitTier': 'default_claude_max_20x'}, ('16', 'account-0043@example.com'): {'emailAddress': 'account-0043@example.com', 'userRateLimitTier': 'default_claude_max_5x'}, ('17', 'account-0044@example.com'): {'emailAddress': 'account-0044@example.com', 'organizationType': 'claude_pro'}}
    for (s, e), oa in snaps.items():
        if e != 'account-0045@example.com':
            oa = dict(oa, profileFetchedAt=1790000000000)
        (cfg / ('.claude-config-%s-%s.json' % (s, e))).write_text(json.dumps({'oauthAccount': oa}), encoding='utf-8')
    info = u.plan_info(d / 'sequence.json', cfg)
    want = {'account-0037@example.com': (20.0, '20x'), 'account-0038@example.com': (5.0, 'Team'), 'account-0039@example.com': (5.0, 'Team'), 'account-0040@example.com': (5.0, 'Team'), 'account-0041@example.com': (u.DEFAULT_SCALE, '?'), 'account-0042@example.com': (u.DEFAULT_SCALE, '?'), 'account-0043@example.com': (5.0, '5x'), 'account-0044@example.com': (1.0, 'Pro'), 'account-0045@example.com': (u.DEFAULT_SCALE, '?')}
    n = 0
    for e, (scale, plan) in want.items():
        assert (info[e]['scale'], info[e]['plan']) == (scale, plan), (e, info[e])
        n += 1
    assert info['account-0038@example.com']['org'] == 'T', info['account-0038@example.com']
    assert u.plan_info(d / 'absent.json', cfg) == {}
    assert u.capacity({'5h': 66, '7d': 90}, 20) == 136.0
    assert u.capacity({'5h': 0, '7d': 0}, 5) == 100.0
    assert u.capacity({'5h': 87, '7d': 15}, 5) == 13.0
    assert u.capacity({'5h': 0, '7d': 80}, 5) == 80.0
    assert u.capacity({}, 5) is None
    assert u.capacity({'7d': 19}, 5) == 100.0 and u.capacity({'7d': 90}, 20) == 160.0
    return n + 9

def check_replay_2026_09_25_1340_keeps_big_plan(tmp: str) -> int:
    me = 'account-0028@example.com'
    res, rows = _run_plan_pool(tmp, '0925-1340', _pool_0925({'5h': 66, '7d': 90}, me), me)
    assert res['action'] == 'stay', res
    assert rows[me]['plan'] == '20x' and rows[me]['cap'] == 136.0, rows[me]
    assert rows['account-0032@example.com']['plan'] == 'Team', rows['account-0032@example.com']
    return 3

def check_replay_2026_09_25_1344_urgent_leaves_by_capacity(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0034@example.com'
    res, _ = _run_plan_pool(tmp, '0925-1344', _pool_0925({'5h': 87, '7d': 15}, me), me, ledger={'account-0028@example.com': {'at': now - 200, 'used': 90.0, 'binding': '7d', 'resetsAt': '', 'why': '超阈值, 只压短时'}}, samples=[[now - 180, {'5h': 30.0, '7d': 5.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'would-switch', res
    assert res['to'] == 'account-0030@example.com', res
    return 3

def check_replay_2026_09_25_1348_does_not_wait_for_97(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0035@example.com'
    res, _ = _run_plan_pool(tmp, '0925-1348', _pool_0925({'5h': 62, '7d': 90}, me, later={'account-0034@example.com': {'5h': 100, '7d': 19}}), me, ledger={'account-0028@example.com': {'at': now - 480, 'used': 90.0, 'binding': '7d', 'resetsAt': '', 'why': '超阈值, 只压短时'}, 'account-0034@example.com': {'at': now - 200, 'used': 100.0, 'binding': '5h', 'resetsAt': _iso_in(hours=4, minutes=50), 'why': '已满'}}, samples=[[now - 80, {'5h': 26.0, '7d': 84.0}]])
    assert res['action'] == 'would-switch', res
    assert res['to'] in ('account-0030@example.com', 'account-0031@example.com'), res
    return 2

def check_switch_cost_prefers_same_org(tmp: str) -> int:
    me = 'account-0047@example.com'
    base = [('1', me, {'5h': 92, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0048@example.com', {'5h': 40, '7d': 30}, {}, 'T', 'Team'), ('3', 'account-0049@example.com', {'5h': 20, '7d': 30}, {}, 'O', '5x')]
    res, _ = _run_plan_pool(tmp, 'cost-a', base, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0048@example.com', res
    res, _ = _run_plan_pool(tmp, 'cost-b', [base[0], base[2]], me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0049@example.com', res
    near = [base[0], ('3', 'account-0049@example.com', {'5h': 70, '7d': 30}, {}, 'O', '5x')]
    res, _ = _run_plan_pool(tmp, 'cost-c', near, me)
    assert res['action'] == 'stay' and '跨组织' in (res.get('why') or ''), res
    same = [base[0], ('2', 'account-0048@example.com', {'5h': 70, '7d': 30}, {}, 'T', 'Team')]
    res, _ = _run_plan_pool(tmp, 'cost-d', same, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0048@example.com', res
    return 5

def check_expiring_capacity_first(tmp: str) -> int:
    me = 'account-0047@example.com'
    mk = lambda a7: [('1', me, {'5h': 99, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0002@example.com', {'5h': 0, '7d': a7}, {'7d': _iso_in(days=1)}, 'A', '20x'), ('3', 'account-0005@example.com', {'5h': 0, '7d': 88}, {'7d': _iso_in(days=6)}, 'B', '20x')]
    res, _ = _run_plan_pool(tmp, 'expiry-a', mk(90), me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0002@example.com', res
    res, _ = _run_plan_pool(tmp, 'expiry-b', mk(97), me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0005@example.com', res
    return 2

def check_big_plan_percent_lines(tmp: str) -> int:
    big = _drow('account-0037@example.com', '1', {'5h': 0.0, '7d': 96.0})
    big.update(scale=20.0, plan='20x', org='B', cap=None)
    small = _drow('account-0050@example.com', '2', {'5h': 0.0, '7d': 96.0})
    with _Patch(is_vacated=lambda e: False):
        assert decide.pick([big], set())[0] is big
        assert decide.pick([small], set())[0] is None
    full = _drow('account-0051@example.com', '3', {'5h': 0.0, '7d': 98.0})
    full.update(scale=20.0)
    assert decide.past(full, decide.BURNED_AT) is False
    assert decide.past(_drow('account-0052@example.com', '4', {'5h': 0.0, '7d': 98.0}), decide.BURNED_AT)
    me = 'account-0007@example.com'
    pool = [('1', me, {'5h': 10, '7d': 97}, {}, 'M', '5x'), ('2', 'account-0012@example.com', {'5h': 90, '7d': 10}, {}, 'T2', '5x')]
    res, _ = _run_plan_pool(tmp, 'exhausted-bigger', pool, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0012@example.com', res
    smaller = [pool[0], ('2', 'account-0012@example.com', {'5h': 94, '7d': 10}, {}, 'T2', '5x')]
    res, _ = _run_plan_pool(tmp, 'exhausted-smaller', smaller, me)
    assert res['action'] == 'stay' and '不比它多' in (res.get('why') or ''), res
    nowhere = [pool[0], ('2', 'account-0012@example.com', {'5h': 99, '7d': 10}, {}, 'T2', '5x')]
    res, _ = _run_plan_pool(tmp, 'exhausted-nowhere', nowhere, me)
    assert res['action'] == 'blocked', res
    big99 = [('1', me, {'5h': 10, '7d': 99}, {}, 'M', '20x'), ('2', 'account-0053@example.com', {'5h': 88, '7d': 10}, {}, 'T', 'Team'), ('3', 'account-0054@example.com', {'5h': 99, '7d': 10}, {}, 'T', 'Team')]
    res, _ = _run_plan_pool(tmp, 'exhausted-big99', big99, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0053@example.com', res
    w99 = _drow('account-0055@example.com', '5', {'5h': 20.0, '7d': 99.0})
    w99.update(scale=20.0)
    assert decide.past(w99, decide.EXHAUSTED_AT) and decide.past(w99, decide.BURNED_AT)
    w97 = _drow('account-0056@example.com', '6', {'5h': 20.0, '7d': 97.0})
    w97.update(scale=20.0)
    assert not decide.past(w97, decide.EXHAUSTED_AT)
    return 11

def check_uniform_plans_keep_percent_behaviour(tmp: str) -> int:
    import random
    rng = random.Random(20260925)
    n = 0
    with _Patch(is_vacated=lambda e: False):
        for _ in range(300):
            rows = []
            for i in range(rng.randint(1, 6)):
                p5 = float(rng.randint(0, 99))
                rows.append(_drow('r%account-0018@example.com' % i, str(i), {'5h': p5, '7d': float(rng.randint(0, int(p5)))}, age=float(rng.randint(0, 600))))
            best, _ = decide.pick(rows, set())
            ok = [r for r in rows if r['worstUsed'] < decide.USABLE_CEILING]
            old = min(ok, key=lambda r: (r['worstUsed'], r['age'])) if ok else None
            if old is None:
                assert best is None, rows
            else:
                assert (best['worstUsed'], best['age']) == (old['worstUsed'], old['age']), (best, old)
            n += 1
    return n

def check_gate_filters_before_ranking(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0047@example.com'
    pool = [('1', me, {'5h': 91, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team'), ('2', 'account-0048@example.com', {'5h': 82, '7d': 30}, {'7d': _iso_in(days=1)}, 'T', 'Team'), ('3', 'account-0057@example.com', {'5h': 80, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team')]
    res, _ = _run_plan_pool(tmp, 'gate-a', pool, me, samples=[[now - 65, {'5h': 71.0, '7d': 29.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'would-switch' and res['to'] == 'account-0057@example.com', res
    res, _ = _run_plan_pool(tmp, 'gate-a2', pool, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0057@example.com', res
    pool = [('1', me, {'5h': 90, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team'), ('2', 'account-0048@example.com', {'5h': 81, '7d': 30}, {'7d': _iso_in(days=1)}, 'T', 'Team'), ('4', 'account-0037@example.com', {'5h': 0, '7d': 97}, {'7d': _iso_in(days=3)}, 'B', '20x')]
    res, _ = _run_plan_pool(tmp, 'gate-b', pool, me, samples=[[now - 65, {'5h': 70.0, '7d': 29.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'would-switch' and res['to'] == 'account-0037@example.com', res
    return 6

def check_forced_pick_still_counts_switch_cost(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0047@example.com'
    pool = [('1', me, {'5h': 98, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team'), ('2', 'account-0048@example.com', {'5h': 30, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team'), ('3', 'account-0049@example.com', {'5h': 45, '7d': 30}, {'7d': _iso_in(days=1)}, 'O', '5x')]
    res, _ = _run_plan_pool(tmp, 'forced-cost', pool, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0048@example.com', res
    urgent = [(s_, e, {'5h': 92, '7d': 30} if e == me else u, r, o, p_) for s_, e, u, r, o, p_ in pool]
    res, _ = _run_plan_pool(tmp, 'forced-cost-u', urgent, me, samples=[[now - 65, {'5h': 72.0, '7d': 29.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'would-switch' and res['to'] == 'account-0048@example.com', res
    return 3

def check_stay_wording_and_exhausted_switch_failures(tmp: str) -> int:
    me = 'account-0047@example.com'
    res, _ = _run_plan_pool(tmp, 'neg-gain', [('1', me, {'5h': 92, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0058@example.com', {'5h': 85, '7d': 30}, {}, 'O', '5x')], me)
    why = res.get('why') or ''
    assert res['action'] == 'stay' and '反而少' in why and ('-' not in why.split('反而少')[1][:4]), res
    pool = [('1', 'account-0007@example.com', {'5h': 10, '7d': 97.5}, {}, None, 5), ('2', 'account-0002@example.com', {'5h': 50, '7d': 10}, {}, None, 5), ('3', 'account-0006@example.com', {'5h': 60, '7d': 10}, {}, None, 5), ('4', 'account-0059@example.com', {'5h': 70, '7d': 10}, {}, None, 5)]
    res, _, fake = _run_pool(tmp, 'exh-fail3', pool, 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert len([c for c in fake.calls if c[:1] == ['switch']]) == decide.MAX_SWITCH_TRIES
    assert res['action'] == 'blocked' and res.get('switchFailed') is True, res
    assert '切换没成功' in (res.get('why') or ''), res
    return 5

def check_landing_hold_uses_the_window_that_ran_out(tmp: str) -> int:
    me = 'account-0047@example.com'
    r5h, r7d = (_iso_in(hours=2), _iso_in(days=5))
    pool = [('1', me, {'5h': 92, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0037@example.com', {'5h': 10, '7d': 50}, {'5h': r5h, '7d': r7d}, 'B', '20x')]
    cpath_holder = {}

    def run():
        import time as _t
        res, rows, book = _run_plan_pool(tmp, 'land-hold', pool, me, dry_run=False)
        return (res, book)
    orig_refresh = None
    cache = Path(tmp) / 'pool-land-hold-usage.json'

    def burn_it(email):
        d = json.loads(cache.read_text(encoding='utf-8'))
        for a in d['accounts'].values():
            if a['email'] == 'account-0037@example.com':
                a['lastGood']['five_hour']['pct'] = 98.2
                a['lastGood']['seven_day']['pct'] = 98.6
        cache.write_text(json.dumps(d), encoding='utf-8')
    real_fake = _SwitchFake.__init__

    def patched_init(self, me_, switch_rc=0, land_on=_MOVE, after_switch=None):
        real_fake(self, me_, switch_rc=switch_rc, land_on=land_on, after_switch=burn_it)
    _SwitchFake.__init__ = patched_init
    try:
        res, book = run()
    finally:
        _SwitchFake.__init__ = real_fake
    rec = book.get('account-0037@example.com') or {}
    assert rec.get('why') == '已满' and rec.get('binding') == '5h' and (rec.get('resetsAt') == r5h), (res, book)
    return 1

def check_panel_and_pickers_sort_by_points(tmp: str) -> int:
    import importlib.util as _ilu
    u = decide.usage
    d = Path(tmp) / 'panel-sort'
    cfg = d / 'configs'
    cfg.mkdir(parents=True, exist_ok=True)
    future = '2099-01-01T00:00:00+00:00'
    import time as _t
    now = _t.time()
    accts = {'1': ('account-0047@example.com', 98, 30, 'T'), '2': ('account-0048@example.com', 0, 90, 'T'), '3': ('account-0060@example.com', 85, 30, 'T'), '4': ('account-0037@example.com', 0, 95, 'B')}
    cache = {'accounts': {s_: {'email': e, 'lastError': None, 'fetchedAt': now, 'lastGood': {'five_hour': {'pct': p5, 'resets_at': future}, 'seven_day': {'pct': p7, 'resets_at': future}}} for s_, (e, p5, p7, o) in accts.items()}}
    seq = {'accounts': {s_: {'email': e, 'organizationUuid': o} for s_, (e, p5, p7, o) in accts.items()}}
    (d / 'usage.json').write_text(json.dumps(cache), encoding='utf-8')
    (d / 'sequence.json').write_text(json.dumps(seq), encoding='utf-8')
    (cfg / ('.claude-config-%s-%s.json' % ('4', 'account-0037@example.com'))).write_text(json.dumps({'oauthAccount': {'emailAddress': 'account-0037@example.com', 'organizationType': 'claude_max', 'profileFetchedAt': 1, 'organizationRateLimitTier': 'default_claude_max_20x'}}), encoding='utf-8')
    orig = (u.CACHE, u.SEQ, u.CONFIGS, u.known_status)
    u.CACHE, u.SEQ, u.CONFIGS, u.known_status = (d / 'usage.json', d / 'sequence.json', cfg, lambda: {})
    import contextlib as _cl
    import io as _io
    buf = _io.StringIO()
    try:
        helper._load_usage = lambda: (u, '')
        helper._active_email_from_status = lambda: 'account-0047@example.com'
        with _cl.redirect_stdout(buf):
            helper.cmd_accounts([])
    finally:
        u.CACHE, u.SEQ, u.CONFIGS, u.known_status = orig
    order = [a['email'] for a in json.loads(buf.getvalue())['accounts']]
    assert order[0] == 'account-0037@example.com', order
    assert order.index('account-0048@example.com') < order.index('account-0060@example.com'), order
    assert all(('_err' not in a and '_disabled' not in a for a in json.loads(buf.getvalue())['accounts']))
    real_collect = u.collect

    def with_disabled(*a, **k):
        rows = real_collect(*a, **k)
        for r in rows:
            if r['email'] == 'account-0037@example.com':
                r['disabled'] = True
        return rows
    u.CACHE, u.SEQ, u.CONFIGS, u.known_status = (d / 'usage.json', d / 'sequence.json', cfg, lambda: {})
    u.collect = with_disabled
    buf = _io.StringIO()
    try:
        with _cl.redirect_stdout(buf):
            helper.cmd_accounts([])
    finally:
        u.CACHE, u.SEQ, u.CONFIGS, u.known_status = orig
        u.collect = real_collect
    order = [a['email'] for a in json.loads(buf.getvalue())['accounts']]
    assert order[-1] == 'account-0037@example.com', order
    return 4

def _with_landing_burn(cache: Path, email: str, windows: dict):
    real_init = _SwitchFake.__init__

    def burn(landed):
        if landed != email:
            return
        d = json.loads(cache.read_text(encoding='utf-8'))
        for a in d['accounts'].values():
            if a['email'] == email:
                for key, win in (('five_hour', '5h'), ('seven_day', '7d')):
                    if win in windows:
                        a['lastGood'].setdefault(key, {})['pct'] = float(windows[win])
        cache.write_text(json.dumps(d), encoding='utf-8')

    def patched(self, me_, switch_rc=0, land_on=_MOVE, after_switch=None):
        real_init(self, me_, switch_rc=switch_rc, land_on=land_on, after_switch=burn)
    return (real_init, patched)

def check_round3_edges(tmp: str) -> int:
    me = 'account-0047@example.com'
    pool = [('1', me, {'5h': 98, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0061@example.com', {'5h': 0, '7d': 50}, {}, 'B', '20x'), ('3', 'account-0062@example.com', {'5h': 90, '7d': 10}, {}, 'O', '5x')]
    real_init, patched = _with_landing_burn(Path(tmp) / 'pool-r3-burned-usage.json', 'account-0061@example.com', {'7d': 99})
    _SwitchFake.__init__ = patched
    try:
        res, _, book = _run_plan_pool(tmp, 'r3-burned', pool, me, dry_run=False)
    finally:
        _SwitchFake.__init__ = real_init
    assert res['action'] == 'switched' and res['to'] == 'account-0062@example.com', res
    assert (book.get('account-0061@example.com') or {}).get('why') == '已满', book
    fail = [('1', 'account-0007@example.com', {'5h': 10, '7d': 97.5}, {}, None, 5), ('2', 'account-0002@example.com', {'5h': 50, '7d': 10}, {}, None, 5), ('3', 'account-0005@example.com', {'5h': 94, '7d': 10}, {}, None, 5)]
    res, _, _ = _run_pool(tmp, 'r3-fail-smaller', fail, 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'blocked' and res.get('switchFailed') is True, res
    band = [('1', me, {'5h': 92, '7d': 30}, {'7d': _iso_in(days=5)}, 'T', 'Team'), ('2', 'account-0053@example.com', {'5h': 0, '7d': 90}, {'7d': _iso_in(days=1)}, 'T', 'Team'), ('3', 'account-0063@example.com', {'5h': 47, '7d': 50}, {'7d': _iso_in(days=5)}, 'T', 'Team')]
    res, _ = _run_plan_pool(tmp, 'r3-band', band, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0063@example.com', res
    same = [('1', me, {'5h': 92, '7d': 30}, {}, 'T', 'Team'), ('2', 'account-0064@example.com', {'5h': 92, '7d': 30}, {}, 'T', 'Team')]
    res, _ = _run_plan_pool(tmp, 'r3-zero', same, me)
    assert res['action'] == 'stay' and '一样多' in (res.get('why') or ''), res
    assert '-0 点' not in (res.get('why') or ''), res
    return 7

def check_same_plan_switch_decision_unchanged(tmp: str) -> int:
    import random
    rng = random.Random(925)
    n = 0
    for i in range(160):
        me_pct = float(rng.choice([90, 91, 92, 93, 94, 95, 96, 97, 98, 99]))
        pool = [('1', 'account-0007@example.com', {'5h': me_pct, '7d': float(rng.randint(0, int(me_pct)))}, {'7d': _iso_in(days=rng.randint(1, 6))}, None, 5)]
        for j in range(rng.randint(1, 5)):
            p5 = float(rng.randint(0, 99))
            pool.append((str(j + 2), 't%account-0018@example.com' % j, {'5h': p5, '7d': float(rng.randint(0, int(p5)))}, {'7d': _iso_in(days=rng.randint(1, 6), hours=rng.randint(0, 23))}, None, 5))
        res, _, _ = _run_pool(tmp, 'same-plan', pool, 'account-0007@example.com')
        ok = [u['5h'] for _s, e, u, *_ in pool[1:] if u['5h'] < decide.USABLE_CEILING]
        if me_pct >= decide.EXHAUSTED_AT:
            old_switch = bool(ok)
        else:
            old_switch = bool(ok) and me_pct - min(ok) >= decide.MIN_GAIN
        assert (res['action'] == 'would-switch') is old_switch, (pool, res)
        n += 1
    return n

def check_exhausted_failed_switch_is_not_silent(tmp: str) -> int:
    pool = [('1', 'account-0007@example.com', {'5h': 10, '7d': 97.5}, {}, None, 5), ('2', 'account-0002@example.com', {'5h': 50, '7d': 10}, {}, None, 5), ('3', 'account-0005@example.com', {'5h': 92, '7d': 10}, {}, None, 5)]
    res, led, fake = _run_pool(tmp, 'exh-fail', pool, 'account-0007@example.com', dry_run=False, switch_rc=1)
    assert [c[1] for c in fake.calls if c[:1] == ['switch']] == ['account-0002@example.com', 'account-0005@example.com'], fake.calls
    assert res['action'] == 'blocked' and res.get('switchFailed') is True, res
    assert 'account-0002@example.com' in (res.get('why') or ''), res
    assert 'account-0007@example.com' not in led, led
    return 4

def check_full_hold_uses_the_window_that_ran_out(tmp: str) -> int:
    me = 'account-0037@example.com'
    r5h, r7d = (_iso_in(hours=2), _iso_in(days=5))
    pool = [('1', me, {'5h': 98.2, '7d': 98.6}, {'5h': r5h, '7d': r7d}, 'B', '20x'), ('2', 'account-0065@example.com', {'5h': 0, '7d': 0}, {}, 'T', 'Team')]
    res, _, book = _run_plan_pool(tmp, 'hold-window', pool, me, dry_run=False)
    assert res['action'] == 'switched' and res['to'] == 'account-0065@example.com', res
    rec = book.get(me) or {}
    assert rec.get('why') == '已满' and rec.get('binding') == '5h', rec
    assert rec.get('resetsAt') == r5h, rec
    return 3

def check_watch_only_never_switches(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0034@example.com'
    pre = {'account-0028@example.com': {'at': now - 200, 'used': 90.0, 'binding': '7d', 'resetsAt': '', 'why': '超阈值, 只压短时'}}
    res, rows, book = _run_plan_pool(tmp, 'watch-only', _pool_0925({'5h': 87, '7d': 15}, me), me, ledger=pre, samples=[[now - 180, {'5h': 30.0, '7d': 5.0}]], dry_run=False, allow_switch=False)
    calls = _LAST_PLAN_FAKE.calls
    assert not any((c[:1] == ['switch'] for c in calls)), calls
    assert _LAST_PLAN_FAKE.current == me, _LAST_PLAN_FAKE.current
    assert res['action'] == 'stay' and res.get('watchOnly') is True, res
    assert res['wouldSwitchTo'] == 'account-0030@example.com', res
    assert res['active'] == me and 'to' not in res, res
    assert set(book) == set(pre), book
    res2, _rows2 = _run_plan_pool(tmp, 'watch-only-dry', _pool_0925({'5h': 87, '7d': 15}, me), me, ledger=pre, samples=[[now - 180, {'5h': 30.0, '7d': 5.0}]], dry_run=True, allow_switch=False)
    assert res2['action'] == 'stay' and res2.get('wouldSwitchTo') == 'account-0030@example.com', res2
    return 7

def check_watch_only_flag(tmp: str) -> int:
    flag = Path(tmp) / 'watch-only-flag'
    if flag.exists():
        flag.unlink()
    old_env = os.environ.pop('CCSWITCH_WATCH_ONLY', None)
    try:
        with _Patch(WATCH_ONLY_FLAG=flag):
            assert decide.watch_only() is False
            flag.write_text('', encoding='utf-8')
            assert decide.watch_only() is True
            flag.unlink()
            os.environ['CCSWITCH_WATCH_ONLY'] = '1'
            assert decide.watch_only() is True
            os.environ['CCSWITCH_WATCH_ONLY'] = '0'
            assert decide.watch_only() is False
    finally:
        os.environ.pop('CCSWITCH_WATCH_ONLY', None)
        if old_env is not None:
            os.environ['CCSWITCH_WATCH_ONLY'] = old_env
    return 4

def check_watch_only_flag_is_shared(tmp: str) -> int:
    assert decide._state_dir() / 'claude-autoswitch.watch-only' == decide.usage._watch_only_flag(), (decide._state_dir(), decide.usage._watch_only_flag())
    return 1

def check_cswap_guard_blocks_switch(tmp: str) -> int:
    guard_sequence = Path(tmp) / 'guard-sequence.json'
    guard_sequence.write_text(json.dumps({'accounts': {'1': {'email': 'account-0066@example.com'}}}), encoding='utf-8')
    flag = Path(tmp) / 'guard-flag'
    flag.write_text('', encoding='utf-8')
    if os.name == 'nt':
        echo = Path(tmp) / 'echo-args.cmd'
        echo.write_text('@echo %*\r\n', encoding='ascii')
        echo_bin = str(echo)
    else:
        echo_bin = '/bin/echo'
    stub = {}
    if hasattr(decide, '_backend'):

        class _EchoBackend:

            @staticmethod
            def run(args, **_kw):
                return subprocess.CompletedProcess(args, 0, stdout=' '.join(args) + '\n', stderr='')
        stub = {'_backend': _EchoBackend}
    try:
        with _Patch(WATCH_ONLY_FLAG=flag, CSWAP=echo_bin, **stub, SEQ=guard_sequence), _Env(CCSWITCH_WATCH_ONLY=None):
            assert decide._cswap(['switch', 'account-0066@example.com']) is None
            r = decide._cswap(['status'])
            assert r is not None and r.returncode == 0, r
        with _Patch(WATCH_ONLY_FLAG=Path(tmp) / 'no-guard-flag', CSWAP=echo_bin, **stub, SEQ=guard_sequence), _Env(CCSWITCH_WATCH_ONLY=None):
            r = decide._cswap(['switch', 'account-0066@example.com'])
            assert r is not None and 'switch account-0066@example.com' in r.stdout, r
    finally:
        flag.unlink()
    return 3

class _SwapLog:

    def __init__(self, tmp: str, name: str, entries=(), rotated=(), switches=()):
        self.path = Path(tmp) / ('swap-%s.log' % name)
        self.entries, self.rotated = (list(entries), list(rotated))
        self.switches = list(switches)

    @staticmethod
    def _lines(entries) -> str:
        import time as _t
        return ''.join(('%s,%03d - WARNING - Usage fetch failed for account %s: %s\n' % (_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(ts)), 123, slot, err) for ts, slot, err in entries))

    @staticmethod
    def _switch_lines(switches) -> str:
        import time as _t
        return ''.join(('%s,456 - INFO - Switched from account %s to %s\n' % (_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(ts)), frm, to) for ts, frm, to in switches))

    def __enter__(self):
        u = decide.usage
        self._orig = u.SWAP_LOG
        self.path.write_text('2026-09-26 03:31:25,000 - INFO - Backed up account 5\n' + self._lines(self.entries) + self._switch_lines(self.switches), encoding='utf-8')
        rot = self.path.with_name(self.path.name + '.1')
        if self.rotated:
            rot.write_text(self._lines(self.rotated), encoding='utf-8')
        elif rot.exists():
            rot.unlink()
        u.SWAP_LOG = self.path
        u._DENIED_LOG_MEMO.clear()
        return self

    def __exit__(self, *exc):
        decide.usage.SWAP_LOG = self._orig
        decide.usage._DENIED_LOG_MEMO.clear()
        return False

def _set_fetch(cache: Path, email: str, **fields) -> None:
    d = json.loads(cache.read_text(encoding='utf-8'))
    for a in d['accounts'].values():
        if a['email'] == email:
            for k, v in fields.items():
                if v is None:
                    a.pop(k, None)
                else:
                    a[k] = v
    cache.write_text(json.dumps(d), encoding='utf-8')

def _run_denied_pool(tmp: str, name: str, accounts, active: str, fetch=None, log=(), dry_run=True, switch_rc=0, after_switch=None, plans=None, orgs=None, ledger_init=None, switches=(), allow_switch=True):
    cpath, spath = _write_pool(tmp, name, accounts)
    for email, fields in (fetch or {}).items():
        _set_fetch(cpath, email, **fields)
    if orgs:
        seq = json.loads(spath.read_text(encoding='utf-8'))
        for a in seq['accounts'].values():
            if a['email'] in orgs:
                a['organizationUuid'] = orgs[a['email']]
        spath.write_text(json.dumps(seq), encoding='utf-8')
    ledger = Path(tmp) / ('pool-%s-ledger.json' % name)
    sample_file = Path(tmp) / ('pool-%s-samples.json' % name)
    for f in (ledger, sample_file):
        if f.exists():
            f.unlink()
    if ledger_init:
        ledger.write_text(json.dumps(ledger_init), encoding='utf-8')
    fake = _SwitchFake(active, switch_rc=switch_rc, after_switch=(lambda e: after_switch(cpath, e)) if after_switch else None)
    cfg = Path(tmp) / ('configs-denied-%s' % name)
    cfg.mkdir(exist_ok=True)
    for f in cfg.glob('*'):
        f.unlink()
    snap = {'20x': {'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_20x'}, '5x': {'organizationType': 'claude_max', 'organizationRateLimitTier': 'default_claude_max_5x'}, 'Team': {'organizationType': 'claude_team', 'seatTier': 'team_standard', 'organizationRateLimitTier': 'default_raven'}}
    for slot, email, *_ in accounts:
        plan = (plans or {}).get(email)
        if plan:
            (cfg / ('.claude-config-%s-%s.json' % (slot, email))).write_text(json.dumps({'oauthAccount': dict(snap[plan], emailAddress=email, profileFetchedAt=1790000000000)}), encoding='utf-8')
    orig_cfg = decide.usage.CONFIGS
    decide.usage.CONFIGS = cfg
    try:
        with _SwapLog(tmp, name, log, switches=switches), _Patch(CACHE=cpath, SEQ=spath, LEDGER=ledger, SAMPLES=sample_file, active_email=lambda: fake.current, refresh=lambda slots, budget=2: True, _cswap=(lambda *a, **k: None) if dry_run else fake.cswap, time=_NoSleep, _record_history=lambda *a: None):
            res = decide.decide(dry_run=dry_run) if allow_switch else decide.decide(dry_run=dry_run, allow_switch=False)
            led = decide.load_ledger()
    finally:
        decide.usage.CONFIGS = orig_cfg
    return (res, led, fake)

def check_denied_slots_rules(tmp: str) -> int:
    import time as _t
    now = _t.time()
    u = decide.usage
    accts = {'1': {'fetchedAt': now - 3600, 'lastError': 'http-403', 'lastAttemptAt': now - 60}, '2': {'lastError': 'http-403', 'lastAttemptAt': now - 60}, '3': {'fetchedAt': now - 7200, 'lastError': 'http-429'}, '4': {'fetchedAt': now - 100, 'lastError': 'timeout'}, '5': {'fetchedAt': now - 7200, 'lastError': None}, '6': {'fetchedAt': now - 7200, 'lastError': 'http-429'}, '7': {'fetchedAt': now - 7200, 'lastError': 'timeout'}, '8': {'fetchedAt': now - 7200, 'lastError': 'timeout'}, '9': {'fetchedAt': now - 7200, 'lastError': 'http-429'}, '10': {'fetchedAt': now - 7200, 'lastError': 'http-403', 'lastAttemptAt': now - 30}}
    log = [(now - 3000, '3', 'http-403'), (now - 2000, '3', 'http-429, retry-after 3600s'), (now - 1000, '3', 'http-403'), (now - 3000, '4', 'http-403'), (now - 3000, '5', 'http-403'), (now - 3000, '6', 'http-429'), (now - 3000, '18', 'http-403'), (now - 3000, '1', 'timeout'), (now - 2500, '9 after refresh', 'http-403'), (now - 9000, '10', 'http-403'), (now - 4000, '10', 'http-403')]
    with _SwapLog(tmp, 'rules', log, rotated=[(now - 5000, '7', 'http-403')]):
        got = u.denied_slots(accts)
    assert set(got) == {'1', '3', '7', '9', '10'}, got
    assert abs(got['3'] - (now - 3000)) < 2, got
    assert abs(got['10'] - (now - 4000)) < 2, got
    assert abs(got['1'] - (now - 60)) < 2, got
    with _SwapLog(tmp, 'rules-nolog'):
        decide.usage.SWAP_LOG = Path(tmp) / 'no-such-swap.log'
        assert set(u.denied_slots(accts)) == {'1', '10'}
    return 5

def check_replay_2026_09_26_denied_active_leaves(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0067@example.com'
    pool = [('5', me, {'5h': 28, '7d': 32}, {'7d': _iso_in(days=1)}, 'http-429', 21600), ('7', 'account-0068@example.com', {'5h': 20, '7d': 29}, {'7d': _iso_in(days=3)}, None, 30), ('4', 'account-0069@example.com', {'5h': 0, '7d': 99}, {'7d': _iso_in(days=4)}, None, 30)]
    log = [(now - 21500, '5', 'http-403'), (now - 21400, '5', 'http-429')]
    res, _, _ = _run_denied_pool(tmp, '0926-dry', pool, me, log=log)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0068@example.com', res
    assert res.get('deniedFrom') == me, res
    res, led, fake = _run_denied_pool(tmp, '0926', pool, me, log=log, dry_run=False)
    assert res['action'] == 'switched' and res['to'] == 'account-0068@example.com', res
    assert res.get('deniedFrom') == me and '疑似' in (res.get('why') or ''), res
    assert not res.get('activeDenied'), res
    assert [c for c in fake.calls if c[:1] == ['switch']] == [['switch', 'account-0068@example.com']], fake.calls
    pool403 = [('5', me, {'5h': 28, '7d': 32}, {}, 'http-403', 600)] + pool[1:]
    res, _, _ = _run_denied_pool(tmp, '0926-403', pool403, me)
    assert res['action'] == 'would-switch' and res.get('deniedFrom') == me, res
    return 7

def check_denied_active_with_nowhere_to_go_is_blocked(tmp: str) -> int:
    me = 'account-0067@example.com'
    pool = [('5', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('6', 'account-0070@example.com', {'5h': 100, '7d': 60}, {'5h': _iso_in(hours=2)}, None, 30), ('7', 'account-0069@example.com', {'5h': 5, '7d': 100}, {'7d': _iso_in(days=2)}, None, 30)]
    res, led, _ = _run_denied_pool(tmp, 'dead-nowhere', pool, me, dry_run=False)
    assert res['action'] == 'blocked' and res.get('activeDenied') is True, res
    assert '疑似' in (res.get('why') or ''), res
    assert res.get('soonest') == 'account-0070@example.com', res
    pool2 = [pool[0], ('6', 'account-0068@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]
    res, _, fake = _run_denied_pool(tmp, 'dead-switchfail', pool2, me, dry_run=False, switch_rc=1)
    assert res['action'] == 'blocked' and res.get('activeDenied') is True, res
    assert res.get('switchFailed') is True, res
    return 5

def check_denied_account_is_never_a_target(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0007@example.com'
    pool = [('1', me, {'5h': 98, '7d': 40}, {}, None, 5), ('2', 'account-0067@example.com', {'5h': 0, '7d': 10}, {}, 'http-429', 7200), ('3', 'account-0068@example.com', {'5h': 30, '7d': 50}, {}, None, 30)]
    log = [(now - 7000, '2', 'http-403')]
    res, _, _ = _run_denied_pool(tmp, 'not-target', pool, me, log=log)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0068@example.com', res
    assert not res.get('deniedFrom'), res
    res, _, _ = _run_denied_pool(tmp, 'not-target-only', pool[:2], me, log=log)
    assert res['action'] == 'blocked' and res.get('soonest') != 'account-0067@example.com', res
    return 3

def check_denied_needs_confirmation_before_switching(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0067@example.com'
    solo = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600)]
    res, _, fake = _run_denied_pool(tmp, 'unsure-solo', solo, me, dry_run=False)
    assert res['action'] == 'blocked' and res.get('activeDenied') and res.get('denyUnsure'), res
    assert '没有别的号可对照' in res['why'] and (not fake.calls), (res, fake.calls)
    pool = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 7200), ('2', 'account-0068@example.com', {'5h': 10, '7d': 20}, {}, None, 7200)]
    early = {me: {'lastAttemptAt': now - 60}, 'account-0068@example.com': {'fetchedAt': now - 300}}
    res, _, fake = _run_denied_pool(tmp, 'unsure-early', pool, me, dry_run=False, fetch=early, log=[(now - 120, '1', 'http-403')])
    assert res['action'] == 'blocked' and res.get('denyUnsure') and (res.get('quiet') is True), res
    assert not [c for c in fake.calls if c[:1] == ['switch']], fake.calls
    assert res.get('nextCheckS') == decide.UNSURE_CHECK_S, res
    stale = {me: {'lastAttemptAt': now - 60}, 'account-0068@example.com': {'fetchedAt': now - 2000}}
    res, _, _ = _run_denied_pool(tmp, 'unsure-late', pool, me, fetch=stale, log=[(now - 1200, '1', 'http-403')])
    assert res.get('denyUnsure') and res.get('quiet') is False and res.get('notice'), res
    later = {me: {'lastAttemptAt': now - 60}, 'account-0068@example.com': {'fetchedAt': now - 30}}
    res, _, _ = _run_denied_pool(tmp, 'confirmed', pool, me, fetch=later, log=[(now - 120, '1', 'http-403')])
    assert res['action'] == 'would-switch' and res.get('deniedFrom') == me, res
    allp = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('2', 'account-0071@example.com', {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('3', 'account-0072@example.com', {}, {}, 'http-403', 600)]
    res, _, fake = _run_denied_pool(tmp, 'unsure-all', allp, me, dry_run=False, fetch={'account-0072@example.com': {'fetchedAt': None}})
    assert res['action'] == 'blocked' and res.get('denyUnsure'), res
    assert not [c for c in fake.calls if c[:1] == ['switch']], fake.calls
    return 11

def check_escape_from_denied_takes_any_capacity(tmp: str) -> int:
    me = 'account-0067@example.com'
    pool = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('2', 'account-0073@example.com', {'5h': 10, '7d': 96}, {}, None, 30)]
    res, _, _ = _run_denied_pool(tmp, 'escape-96', pool, me)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0073@example.com', res
    import time as _t
    hold = {'account-0073@example.com': {'at': _t.time() - 120, 'used': 91.0, 'binding': '5h', 'resetsAt': '', 'why': '超阈值, 只压短时'}}
    pool2 = [pool[0], ('2', 'account-0073@example.com', {'5h': 60, '7d': 30}, {}, None, 30)]
    res, _, _ = _run_denied_pool(tmp, 'escape-hold', pool2, me, ledger_init=hold)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0073@example.com', res
    full = {'account-0073@example.com': dict(hold['account-0073@example.com'], why='已满', resetsAt=_iso_in(hours=3))}
    res, _, _ = _run_denied_pool(tmp, 'escape-full', pool2, me, ledger_init=full)
    assert res['action'] == 'blocked' and res.get('activeDenied'), res
    team = [('1', 'account-0074@example.com', {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('2', 'account-0075@example.com', {'5h': 0, '7d': 0}, {}, None, 30), ('3', 'account-0076@example.com', {'5h': 0, '7d': 0}, {}, None, 30), ('4', 'account-0077@example.com', {'5h': 30, '7d': 40}, {}, None, 30)]
    orgs = {'account-0074@example.com': 'T', 'account-0075@example.com': 'T', 'account-0076@example.com': 'T', 'account-0077@example.com': 'S'}
    seats = {'account-0074@example.com', 'account-0075@example.com', 'account-0076@example.com'}

    def team_dies(cache, email):
        if email in seats:
            _set_fetch(cache, email, lastError='http-403', lastAttemptAt=_t.time())
    res, _, fake = _run_denied_pool(tmp, 'escape-org', team, 'account-0074@example.com', dry_run=False, orgs=orgs, after_switch=team_dies)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert switches == ['account-0077@example.com'], switches
    assert res['action'] == 'switched' and res.get('deniedFrom') == 'account-0074@example.com', res
    res, _, _ = _run_denied_pool(tmp, 'escape-org-only', team[:3], 'account-0074@example.com', orgs=orgs)
    assert res['action'] == 'would-switch' and res['to'] in seats - {'account-0074@example.com'}, res
    return 7

def check_replay_2026_09_26_with_real_plan_shape(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0067@example.com'
    pool = [('5', me, {'5h': 28, '7d': 32}, {'7d': _iso_in(days=1)}, 'http-429', 21600), ('8', 'account-0075@example.com', {'5h': 70, '7d': 50}, {}, None, 30), ('9', 'account-0076@example.com', {'5h': 95, '7d': 60}, {}, None, 30)]
    plans = {me: '20x', 'account-0075@example.com': 'Team', 'account-0076@example.com': 'Team'}
    orgs = {me: 'P', 'account-0075@example.com': 'T', 'account-0076@example.com': 'T'}
    log = [(now - 21500, '5', 'http-403'), (now - 21400, '5', 'http-429')]
    res, _, _ = _run_denied_pool(tmp, '0926-20x', pool, me, log=log, plans=plans, orgs=orgs)
    assert res['action'] == 'would-switch' and res['to'] == 'account-0075@example.com', res
    assert res.get('fromCap', 0) > 200, res
    res, _, fake = _run_denied_pool(tmp, '0926-20x-run', pool, me, log=log, plans=plans, orgs=orgs, dry_run=False)
    assert res['action'] == 'switched' and res['to'] == 'account-0075@example.com', res
    assert '已换到' in (res.get('notice') or '') and me in res['notice'], res
    return 5

def check_denied_notices_and_hard_edges(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0067@example.com'
    pool = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('2', 'account-0068@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]
    res, _, _ = _run_denied_pool(tmp, 'notice-sf', pool, me, dry_run=False, switch_rc=1)
    assert res['action'] == 'blocked' and res.get('switchFailed'), res
    assert '没成功' in res.get('notice', '') and '没有余量' not in res.get('notice', ''), res
    pool2 = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-429', 7200), ('2', 'account-0068@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]

    def burn_b(cache, email):
        if email == 'account-0068@example.com':
            d = json.loads(cache.read_text(encoding='utf-8'))
            for a in d['accounts'].values():
                if a['email'] == email:
                    a['lastGood']['five_hour']['pct'] = 100.0
            cache.write_text(json.dumps(d), encoding='utf-8')
    res, _, fake = _run_denied_pool(tmp, 'notice-burn', pool2, me, dry_run=False, log=[(now - 7000, '1', 'http-403')], after_switch=burn_b)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert switches == ['account-0068@example.com'], switches
    assert res['action'] == 'blocked' and res.get('deniedFrom') == me, res
    assert '也满了' in res.get('notice', ''), res
    good = 'account-0007@example.com'
    pool3 = [('1', good, {'5h': 99, '7d': 40}, {}, None, 5), ('2', 'account-0071@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]

    def deny_b(cache, email):
        if email == 'account-0071@example.com':
            _set_fetch(cache, email, lastError='http-403', lastAttemptAt=_t.time())
    res, led, _ = _run_denied_pool(tmp, 'land-denied-stuck', pool3, good, dry_run=False, after_switch=deny_b)
    assert res['action'] == 'blocked' and res.get('activeDenied') is True, res
    assert res.get('to') == 'account-0071@example.com' and (not res.get('deniedFrom')), res
    assert good not in led, led
    rows = [dict(_drow('account-0067@example.com', '1', {'5h': 100.0}, resets=_iso_in(hours=1), error='http-429'), denied=True, resets={'5h': _iso_in(hours=1)}), dict(_drow('account-0070@example.com', '2', {'5h': 100.0}, resets=_iso_in(hours=3)), resets={'5h': _iso_in(hours=3)})]
    soon = decide.soonest_recovery(rows, now)
    assert soon and soon['email'] == 'account-0070@example.com', soon
    return 13

def check_landing_on_denied_moves_on(tmp: str) -> int:
    import time as _t
    me = 'account-0067@example.com'
    pool = [('5', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('6', 'account-0071@example.com', {'5h': 0, '7d': 5}, {}, None, 30), ('7', 'account-0078@example.com', {'5h': 20, '7d': 40}, {}, None, 30)]

    def deny_on_landing(cache, email):
        if email == 'account-0071@example.com':
            _set_fetch(cache, email, lastError='http-403', lastAttemptAt=_t.time())
    res, _, fake = _run_denied_pool(tmp, 'land-denied', pool, me, dry_run=False, after_switch=deny_on_landing)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert switches == ['account-0071@example.com', 'account-0078@example.com'], switches
    assert res['action'] == 'switched' and res['to'] == 'account-0078@example.com', res
    assert res.get('deniedFrom') == me and (not res.get('activeDenied')), res
    pool2 = pool[:2]
    res, _, _ = _run_denied_pool(tmp, 'land-denied-all', pool2, me, dry_run=False, after_switch=deny_on_landing)
    assert res['action'] == 'blocked' and res.get('activeDenied') is True, res
    assert res.get('to') == 'account-0071@example.com', res
    return 6

def check_round2_egress_recovery_and_switch_in(tmp: str) -> int:
    import time as _t
    now = _t.time()
    me = 'account-0079@example.com'
    pool = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 5000), ('2', 'account-0080@example.com', {'5h': 10, '7d': 20}, {}, None, 60), ('3', 'account-0081@example.com', {'5h': 10, '7d': 20}, {}, None, 100)]
    log = [(now - 1200, '1', 'http-403'), (now - 1190, '3', 'http-403'), (now - 900, '1', 'http-403')]
    res, _, fake = _run_denied_pool(tmp, 'egress-recover', pool, me, dry_run=False, log=log, fetch={me: {'lastAttemptAt': now - 400}})
    assert res['action'] == 'blocked' and res.get('denyUnsure'), res
    assert '网络出口' in res['why'] and (not fake.calls), (res, fake.calls)
    res, _, _ = _run_denied_pool(tmp, 'egress-recover-retry', pool, me, log=log, fetch={me: {'lastAttemptAt': now - 20, 'lastError': 'http-429'}})
    assert res['action'] == 'would-switch' and res.get('deniedFrom') == me, res
    good = 'account-0082@example.com'
    pool2 = [('1', good, {'5h': 99, '7d': 40}, {}, None, 5), ('2', 'account-0071@example.com', {'5h': 0, '7d': 5}, {}, None, 30), ('3', 'account-0078@example.com', {'5h': 20, '7d': 40}, {}, None, 30)]

    def deny_b(cache, email):
        if email == 'account-0071@example.com':
            _set_fetch(cache, email, lastError='http-403', lastAttemptAt=_t.time())
    res, led, fake = _run_denied_pool(tmp, 'land-unsure', pool2, good, dry_run=False, after_switch=deny_b)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert switches == ['account-0071@example.com'], switches
    assert res['action'] == 'blocked' and res.get('denyUnsure') and res.get('activeDenied'), res
    assert res.get('quiet') is True and '分不清' in res.get('notice', ''), res
    assert good not in led, led
    pool3 = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-429', 7200), ('2', 'account-0080@example.com', {'5h': 10, '7d': 20}, {}, None, 30)]
    log3 = [(now - 7000, '1', 'http-403'), (now - 6900, '1', 'http-429')]
    res, _, fake = _run_denied_pool(tmp, 'switched-in', pool3, me, log=log3, dry_run=False, switches=[(now - 600, '2', '1')])
    assert res['action'] == 'stay' and '切过来之前' in res['why'], res
    assert not fake.calls, fake.calls
    res, _, _ = _run_denied_pool(tmp, 'switched-in-then-403', pool3, me, log=log3 + [(now - 300, '1', 'http-403')], switches=[(now - 600, '2', '1')])
    assert res['action'] == 'would-switch' and res.get('deniedFrom') == me, res
    return 13

def check_watch_only_with_denied_current(tmp: str) -> int:
    me = 'account-0067@example.com'
    pool = [('1', me, {'5h': 10, '7d': 20}, {}, 'http-403', 600), ('2', 'account-0068@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]
    res, led, fake = _run_denied_pool(tmp, 'wo-denied', pool, me, dry_run=False, allow_switch=False)
    assert res['action'] == 'stay' and res.get('watchOnly'), res
    assert res.get('wouldSwitchTo') == 'account-0068@example.com' and res.get('currentDenied'), res
    assert '疑似被封' in res.get('notice', '') and 'account-0068@example.com' in res['notice'], res
    assert not fake.calls and me not in led, (fake.calls, led)
    res, _, fake = _run_denied_pool(tmp, 'wo-denied-unsure', pool[:1], me, dry_run=False, allow_switch=False)
    assert res['action'] == 'blocked' and res.get('denyUnsure') and (not fake.calls), res
    good = [('1', 'account-0007@example.com', {'5h': 99, '7d': 40}, {}, None, 5), ('2', 'account-0068@example.com', {'5h': 10, '7d': 10}, {}, None, 30)]
    res, _, fake = _run_denied_pool(tmp, 'wo-good', good, 'account-0007@example.com', dry_run=False, allow_switch=False)
    assert res.get('wouldSwitchTo') == 'account-0068@example.com' and (not res.get('currentDenied')), res
    assert not fake.calls, fake.calls
    return 8

def check_denied_is_blocked_in_panel_and_ccpick_auto(tmp: str) -> int:
    import sys as _sys
    import time as _t
    now = _t.time()
    u = decide.usage
    d = Path(tmp) / 'denied-panel'
    d.mkdir(exist_ok=True)
    future = '2099-01-01T00:00:00+00:00'
    lg = {'five_hour': {'pct': 5.0, 'resets_at': future}, 'seven_day': {'pct': 10.0, 'resets_at': future}}
    cache = {'accounts': {'1': {'email': 'account-0067@example.com', 'lastError': 'http-429', 'fetchedAt': now - 7200, 'lastGood': lg}, '2': {'email': 'account-0068@example.com', 'lastError': None, 'fetchedAt': now, 'lastGood': lg}, '3': {'email': 'account-0072@example.com', 'lastError': 'http-403', 'lastGood': {}}}}
    seq = {'accounts': {s: {'email': a['email']} for s, a in cache['accounts'].items()}}
    (d / 'usage.json').write_text(json.dumps(cache), encoding='utf-8')
    (d / 'sequence.json').write_text(json.dumps(seq), encoding='utf-8')
    orig = (u.CACHE, u.SEQ, u.known_status, u.live_identity, u.CONFIGS)
    (d / 'configs').mkdir(exist_ok=True)
    u.CACHE, u.SEQ, u.known_status, u.live_identity, u.CONFIGS = (d / 'usage.json', d / 'sequence.json', lambda: {}, lambda *a, **k: '', d / 'configs')
    try:
        with _SwapLog(tmp, 'panel', [(now - 7000, '1', 'http-403')]):
            rows = {r['email']: r for r in u.collect(identity='account-0068@example.com')}
            text = u.render(list(rows.values()))
            saved_mod = _sys.modules.get('ccpick_usage')
            _sys.modules['ccpick_usage'] = u
            _sys.path.insert(0, str(_HERE.parent))
            try:
                import ccpick_auto as _ca
                meta = {str(r['slot']): {'email': r['email'], 'active': r['active'], 'windows': r['windows'], 'error': r['error'], 'denied': r['denied']} for r in rows.values()}
                probe = {'1': {'headroom': 90.0, 'windows': {'5h': 5.0, '7d': 10.0}}, '2': {'headroom': 90.0, 'windows': {'5h': 5.0, '7d': 10.0}}}
                ranked = {r['email']: r for r in _ca.rank(probe, meta)}
                assert _ca.slot_meta()['1']['denied'], _ca.slot_meta()
            finally:
                _sys.path.pop(0)
                if saved_mod is None:
                    _sys.modules.pop('ccpick_usage', None)
                else:
                    _sys.modules['ccpick_usage'] = saved_mod
    finally:
        u.CACHE, u.SEQ, u.known_status, u.live_identity, u.CONFIGS = orig
    dead, live, token = (rows['account-0067@example.com'], rows['account-0068@example.com'], rows['account-0072@example.com'])
    assert dead['denied'] and (not live['denied']) and (not token['denied']), rows
    blocked, why = u.is_blocked(dead)
    assert blocked and '403' in why and ('停用' in why), why
    assert not u.is_usable(dead)[0] and u.is_usable(live)[0]
    assert not u.is_blocked(token)[0]
    assert '疑似被 Anthropic 停用' in text and 'setup-token' in text, text
    assert not ranked['account-0067@example.com']['usable'] and '停用' in ranked['account-0067@example.com']['why_bad']
    assert ranked['account-0068@example.com']['usable']
    return 9

def check_manual_only_automatic_targets(tmp: str) -> int:
    manual = dict(_drow('account-0083@example.com', '1', {'5h': 0.0}), autoSwitchEnabled=False)
    normal = _drow('account-0084@example.com', '2', {'5h': 20.0})
    with _Patch(is_vacated=lambda *a, **k: False):
        for escape in (False, True):
            assert decide.pick([manual, normal], set(), escape=escape)[0] is normal
            assert decide.pick([manual], set(), escape=escape, avoid_orgs={'unavailable-org'})[0] is None
        assert decide.auto_switch_enabled({})
        assert decide.auto_switch_enabled({'autoSwitchEnabled': True})
    cache, seq = _write_pool(tmp, 'manual-only-roster', [('1', 'account-0083@example.com', {'5h': 0.0}, {}, None, 1), ('2', 'account-0084@example.com', {'5h': 20.0}, {}, None, 1)])
    settings = json.loads(seq.read_text(encoding='utf-8'))
    settings['accounts']['1']['autoSwitchEnabled'] = False
    seq.write_text(json.dumps(settings), encoding='utf-8')
    empty_cfg = Path(tmp) / 'manual-only-empty-configs'
    empty_cfg.mkdir()
    old_cfg = decide.usage.CONFIGS
    decide.usage.CONFIGS = empty_cfg
    try:
        with _Patch(CACHE=cache, SEQ=seq):
            rows = decide.rows_from_cache()
        assert len(rows) == 2 and rows[0]['autoSwitchEnabled'] is False
        assert rows[1]['autoSwitchEnabled'] is True
    finally:
        decide.usage.CONFIGS = old_cfg
    future = _iso_in(hours=2)
    held = dict(_drow('account-0083@example.com', '1', {'5h': 100.0}, resets=future), autoSwitchEnabled=False)
    allowed = _drow('account-0084@example.com', '2', {'5h': 100.0}, resets=_iso_in(hours=3))
    assert decide.soonest_recovery([held, allowed], _NoSleep.time()) is not None
    assert decide.soonest_recovery([held, allowed], _NoSleep.time())['email'] == 'account-0084@example.com'
    assert decide.soonest_recovery([held], _NoSleep.time()) is None

    def run(name, rows, active, *, fail=False, burn_landing=False):

        def after_switch(email):
            if burn_landing:
                target = next((r for r in rows if r['email'] == email))
                target.update(worstUsed=100.0, used={'5h': 100.0}, counted={'5h': 100.0})
        fake = _SwitchFake(active, switch_rc=1 if fail else 0, after_switch=after_switch)
        with _Patch(rows_from_cache=lambda: rows, active_email=lambda: fake.current, refresh=lambda *a, **k: True, _cswap=fake.cswap, LEDGER=Path(tmp) / (name + '-ledger.json'), SAMPLES=Path(tmp) / (name + '-samples.json'), _record_history=lambda *a: None, time=_NoSleep):
            result = decide.decide_predictive(False)
        return (result, [c for c in fake.calls if c[:1] == ['switch']])
    result, calls = run('manual-only-no-target', [_drow('account-0085@example.com', '3', {'5h': 100.0}), manual], 'account-0085@example.com')
    assert result['action'] == 'blocked' and calls == [], (result, calls)
    origin = dict(_drow('account-0083@example.com', '1', {'5h': 96.0}), autoSwitchEnabled=False)
    result, calls = run('manual-only-leave', [origin, normal], 'account-0083@example.com')
    assert result['action'] == 'switched' and calls == [['switch', 'account-0084@example.com']]
    origin = dict(_drow('account-0083@example.com', '1', {'5h': 96.0}), autoSwitchEnabled=False)
    target = _drow('account-0084@example.com', '2', {'5h': 20.0})
    result, calls = run('manual-only-no-return', [origin, target], 'account-0083@example.com', burn_landing=True)
    assert calls == [['switch', 'account-0084@example.com']], (result, calls)
    assert result['action'] == 'blocked', result
    result, calls = run('manual-only-retry', [_drow('account-0085@example.com', '3', {'5h': 100.0}), manual, normal], 'account-0085@example.com', fail=True)
    assert calls == [['switch', 'account-0084@example.com']], (result, calls)
    return 17

def check_manual_only_panel_is_still_selectable(tmp: str) -> int:
    import contextlib
    import io
    import types
    future = _iso_in(hours=2)
    rows = [{'slot': '1', 'email': 'account-0083@example.com', 'active': False, 'autoSwitchEnabled': False, 'windows': {'5h': {'pct': 0.0}}}, {'slot': '2', 'email': 'account-0084@example.com', 'active': True, 'windows': {'5h': {'pct': 80.0}}}]
    fake = types.SimpleNamespace(collect=lambda: rows, counted_windows=lambda w: w, counts_toward_limit=lambda _name: True, is_usable=lambda _row: (True, ''), is_blocked=lambda _row: (False, ''))
    old = (helper._load_usage, helper._active_email_from_status)
    helper._load_usage = lambda: (fake, '')
    helper._active_email_from_status = lambda: ''
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            helper.cmd_accounts([])
        panel = json.loads(buf.getvalue())['accounts']
        assert [r['email'] for r in panel] == ['account-0084@example.com', 'account-0083@example.com']
        assert panel[1]['autoSwitchEnabled'] is False and panel[1]['blocked'] is False
        assert panel[0]['autoSwitchEnabled'] is True
        rows[:] = [dict(rows[0], windows={'5h': {'pct': 100.0, 'resets_at': future}})]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            helper.cmd_earliest([])
        assert not buf.getvalue()
    finally:
        helper._load_usage, helper._active_email_from_status = old
    from ccpick_app import account_context as ccpick_account_context
    from unittest.mock import patch
    profiles = [{'name': 'manual', 'account': {'email': 'account-0083@example.com'}}]
    manager = types.SimpleNamespace(current=lambda: {'name': 'manual'}, profiles=lambda: profiles, _watch_only=lambda: False, selection=lambda: {'selected': 'manual', 'selectedAt': '2026-01-01T00:00:00Z'})
    with patch.object(ccpick_account_context, 'manager', return_value=manager):
        before = helper._wait_context()
        profiles[0]['autoSwitchEnabled'] = False
        assert helper._wait_context() != before
    return 5

def main() -> int:
    from ccpick_app import account_context as ccpick_account_context
    from unittest.mock import patch
    checks = 0
    with tempfile.TemporaryDirectory() as tmp, _Env(CCSWITCH_MODELS=None, CCSWITCH_WATCH_ONLY=None), _Patch(WATCH_ONLY_FLAG=Path(tmp) / 'no-such-watch-only-flag'), patch.object(ccpick_account_context, 'manager', return_value=None), patch.object(ccpick_account_context, 'dispatch', return_value=None):
        decide.usage.SWAP_LOG = Path(tmp) / 'no-swap.log'
        decide.usage._DENIED_LOG_MEMO.clear()
        for fn in (check_rate_needs_two_samples, check_manual_only_automatic_targets, check_manual_only_panel_is_still_selectable, check_stale_cache_is_not_zero_rate, check_worst_window_wins, check_uncounted_model_window_does_not_drive_rate, check_pace_ignores_uncounted_for_gate_and_hot_ceiling, check_no_better_target_is_stay_not_blocked, check_truly_exhausted_still_blocked, check_urgent_with_nowhere_to_go_stays, check_all_switches_fail_but_current_usable_stays, check_soonest_ignores_uncounted_windows, check_soonest_cross_offset, check_roster_slot_and_email, check_model_window_display_only_by_default, check_ccswitch_models_opt_in, check_ledger_entry_on_uncounted_window_is_short_hold, check_helper_loads_sibling_usage, check_burst_not_averaged, check_window_expires, check_parse_active_email, check_next_check_clamped, check_removed_account_is_not_a_candidate, check_soonest_skips_past_and_dead, check_soonest_waits_for_every_blocking_window, check_ledger_only_after_really_leaving, check_burned_landing_is_not_a_dead_end, check_stranded_on_burned_goes_home, check_home_is_not_held_to_target_ceiling, check_third_account_landing_can_recover, check_move_resets_pace_and_reports_switch, check_history_identity_is_final_account, check_net_move_that_stays_is_a_switch, check_load_usage_rejects_stale_module, check_load_usage_from_home_bin_copy, check_failed_fetch_does_not_fake_fresh_data, check_transient_fetch_errors_keep_target, check_usable_implies_autoswitch_target, check_expired_window_not_counted, check_switch_is_recorded_in_usage_history, check_hot_zone_never_relaxes, check_gate_is_every_threshold_not_just_a_hundred, check_offline_round_keeps_water_level, check_interval_tracks_decision_gate_not_wall, check_slow_burn_still_used_to_the_end, check_replay_wall_2, check_replay_2026_09_20_outage, check_replay_wall_1, check_replay_normal_burn_not_twitchy, check_rate_at_real_cache_cadence, check_identity_lookup_is_bounded, check_panel_never_forks_on_open, check_no_byte_slicing_of_chinese, check_status_write_survives_bad_bytes, check_plan_info_reads_cswap_data, check_replay_2026_09_25_1340_keeps_big_plan, check_replay_2026_09_25_1344_urgent_leaves_by_capacity, check_replay_2026_09_25_1348_does_not_wait_for_97, check_switch_cost_prefers_same_org, check_expiring_capacity_first, check_big_plan_percent_lines, check_uniform_plans_keep_percent_behaviour, check_gate_filters_before_ranking, check_forced_pick_still_counts_switch_cost, check_stay_wording_and_exhausted_switch_failures, check_landing_hold_uses_the_window_that_ran_out, check_panel_and_pickers_sort_by_points, check_round3_edges, check_same_plan_switch_decision_unchanged, check_exhausted_failed_switch_is_not_silent, check_full_hold_uses_the_window_that_ran_out, check_watch_only_never_switches, check_watch_only_flag, check_watch_only_flag_is_shared, check_cswap_guard_blocks_switch, check_denied_slots_rules, check_replay_2026_09_26_denied_active_leaves, check_denied_active_with_nowhere_to_go_is_blocked, check_denied_account_is_never_a_target, check_denied_needs_confirmation_before_switching, check_escape_from_denied_takes_any_capacity, check_replay_2026_09_26_with_real_plan_shape, check_denied_notices_and_hard_edges, check_round2_egress_recovery_and_switch_in, check_landing_on_denied_moves_on, check_watch_only_with_denied_current, check_denied_is_blocked_in_panel_and_ccpick_auto, check_unenrolled_account_is_auto_enrolled, check_enroll_records_profile_mapping, check_mapping_failure_never_blocks_enroll, check_dry_run_never_enrolls, check_failed_enroll_says_why, check_enrolled_but_cache_lags_is_not_an_error):
            checks += fn(tmp)
    print('pace selftest: %d checks passed; 未联网、未碰真实样本文件' % checks)
    for why in _SKIPPED:
        print('  [跳过] %s' % why)
    if _SKIPPED:
        print('  ↑ %d 条因本平台限制未执行 —— 它们【没有】计入上面的 checks。' % len(_SKIPPED))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
