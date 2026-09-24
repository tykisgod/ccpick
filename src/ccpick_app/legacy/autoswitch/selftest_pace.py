from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import tempfile
import pathlib
from pathlib import Path
_SKIPPED: list[str] = []
_HERE = Path(__file__).resolve().parent
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
    decide.record_sample('account-0001@example.com', {'5h': 10.0}, 1000.0)
    assert decide.burn_eta('account-0001@example.com', {'5h': 10.0}, 1000.0) == (None, None)
    decide.record_sample('account-0001@example.com', {'5h': 27.0}, 1060.0)
    rate, eta = decide.burn_eta('account-0001@example.com', {'5h': 27.0}, 1060.0)
    assert abs(rate - 17.0) < 0.01, rate
    assert abs(eta - (100 - 27) / 17.0 * 60) < 0.5, eta
    return 3

def check_stale_cache_is_not_zero_rate(tmp: str) -> int:
    _fresh(tmp)
    for _ in range(5):
        decide.record_sample('account-0001@example.com', {'5h': 40.0}, 2000.0)
    assert len(decide._read(decide.SAMPLES)['samples']) == 1
    assert decide.burn_eta('account-0001@example.com', {'5h': 40.0}, 2100.0) == (None, None)
    return 2

def check_worst_window_wins(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0001@example.com', {'5h': 10.0, '7d': 60.0}, 3000.0)
    decide.record_sample('account-0001@example.com', {'5h': 11.0, '7d': 78.0}, 3060.0)
    rate, eta = decide.burn_eta('account-0001@example.com', {'5h': 11.0, '7d': 78.0}, 3060.0)
    assert abs(rate - 18.0) < 0.01, rate
    assert abs(eta - (100 - 78) / 18.0 * 60) < 0.5, eta
    return 2

def check_uncounted_model_window_does_not_drive_rate(tmp: str) -> int:
    samples = ((3000.0, {'5h': 10.0, '7d': 5.0, 'Fable': 60.0}), (3060.0, {'5h': 11.0, '7d': 5.0, 'Fable': 78.0}))
    with _Env(CCSWITCH_MODELS=None):
        _fresh(tmp)
        for t, u in samples:
            decide.record_sample('account-0001@example.com', u, t)
        rate, _ = decide.burn_eta('account-0001@example.com', samples[-1][1], 3060.0)
        assert abs(rate - 1.0) < 0.01, rate
        assert 'Fable' in decide._read(decide.SAMPLES)['samples'][-1][1]
    with _Env(CCSWITCH_MODELS='Fable'):
        rate, _ = decide.burn_eta('account-0001@example.com', samples[-1][1], 3060.0)
        assert abs(rate - 18.0) < 0.01, rate
    return 3

def check_pace_ignores_uncounted_for_gate_and_hot_ceiling(tmp: str) -> int:
    rate = 2.0
    with _Env(CCSWITCH_MODELS=None):
        assert abs(decide.eta_to_gate({'5h': 10.0, 'Fable': 92.0}, rate) - 2400) < 1
        _fresh(tmp)
        decide.record_sample('account-0001@example.com', {'5h': 8.0, 'Fable': 90.0}, 1000.0)
        decide.record_sample('account-0001@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        pace = decide.pace_of('account-0001@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        assert pace['nextCheckS'] == decide.NEXT_CHECK_MAX_S, pace
        assert pace['urgent'] is False, pace
    with _Env(CCSWITCH_MODELS='Fable'):
        assert abs(decide.eta_to_gate({'5h': 10.0, 'Fable': 92.0}, rate) - 150) < 1
        pace = decide.pace_of('account-0001@example.com', {'5h': 10.0, 'Fable': 92.0}, 1060.0)
        assert pace['nextCheckS'] <= decide.NEXT_CHECK_MAX_HOT_S, pace
    return 4

def check_burst_not_averaged(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0001@example.com', {'5h': 10.0}, 4000.0)
    decide.record_sample('account-0001@example.com', {'5h': 10.0}, 4030.0)
    decide.record_sample('account-0001@example.com', {'5h': 30.0}, 4090.0)
    rate, _ = decide.burn_eta('account-0001@example.com', {'5h': 30.0}, 4090.0)
    assert abs(rate - 20.0) < 0.01, rate
    return 1

def check_window_expires(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0001@example.com', {'5h': 10.0}, 5000.0)
    decide.record_sample('account-0001@example.com', {'5h': 30.0}, 5060.0)
    now = 5060.0 + decide.RATE_WINDOW_S + 10
    assert decide.burn_eta('account-0001@example.com', {'5h': 30.0}, now) == (None, None)
    return 1

def check_parse_active_email(tmp: str) -> int:
    assert decide.parse_active_email('Status: account-0002@example.com (not managed)') == 'account-0002@example.com'
    assert decide.parse_active_email('Status: #3 (account-0003@example.com) 5h 12%') == 'account-0003@example.com'
    assert decide.parse_active_email('Status: not signed in') == ''
    assert decide.parse_active_email('') == ''
    return 4

def check_interval_tracks_decision_gate_not_wall(tmp: str) -> int:
    _fresh(tmp)
    decide.record_sample('account-0001@example.com', {'5h': 84.0}, 1000.0)
    decide.record_sample('account-0001@example.com', {'5h': 85.0}, 1060.0)
    pace = decide.pace_of('account-0001@example.com', {'5h': 85.0}, 1060.0)
    assert abs(pace['etaS'] - 900) < 2, pace
    assert abs(pace['nextCheckS'] - 75) < 2, pace
    assert pace['burnRate'] == 1.0, pace
    _fresh(tmp)
    decide.record_sample('account-0004@example.com', {'5h': 94.0}, 1000.0)
    decide.record_sample('account-0004@example.com', {'5h': 95.0}, 1060.0)
    pace = decide.pace_of('account-0004@example.com', {'5h': 95.0}, 1060.0)
    assert abs(pace['etaS'] - 300) < 2, pace
    assert abs(pace['nextCheckS'] - 30) < 2, pace
    _fresh(tmp)
    decide.record_sample('account-0005@example.com', {'5h': 50.0}, 1000.0)
    pace = decide.pace_of('account-0005@example.com', {'5h': 50.0}, 1000.0)
    assert pace['nextCheckS'] is None and pace['etaS'] is None, pace
    return 7

def check_offline_round_keeps_water_level(tmp: str) -> int:
    dest = os.path.join(tmp, 'status.json')
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '85|85|40|12|0', '107|429|0.7|account-0006@example.com'])
    first = json.loads(open(dest, encoding='utf-8').read())
    assert first['usedPct'] == 85.0 and first['nextCheckS'] == 107.0, first
    helper.cmd_write([dest, 'offline', '连不上 claude.ai', '下一轮自己重试', '90', '', ''])
    off = json.loads(open(dest, encoding='utf-8').read())
    assert off['state'] == 'offline', off
    assert off['usedPct'] == 85.0, off
    assert off['win5h'] == 85.0 and off['win7d'] == 40.0, off
    assert off['activeEmail'] == 'account-0006@example.com', off
    assert off['nextCheckS'] is None and off['etaS'] is None, off
    assert off['burnRate'] is None, off
    dest2 = os.path.join(tmp, 'status-fresh.json')
    helper.cmd_write([dest2, 'offline', '连不上', '', '90', '', ''])
    fresh = json.loads(open(dest2, encoding='utf-8').read())
    assert fresh['usedPct'] is None and fresh['activeEmail'] is None, fresh
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '30|30|10|5|0', '300|1200|0.2|account-0007@example.com'])
    now = json.loads(open(dest, encoding='utf-8').read())
    assert now['usedPct'] == 30.0 and now['activeEmail'] == 'account-0007@example.com', now
    helper.cmd_write([dest, 'ok', '在盯着', '', '90', '20|10|20|99|0|7d|Fable|0', '300|1200|0.2|account-0007@example.com'])
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
        decide.CACHE.write_text(json.dumps({'accounts': {'3': _acct('account-0008@example.com', 5.0), '4': _acct('account-0009@example.com', 50.0)}}), encoding='utf-8')
        decide.SEQ.write_text(json.dumps({'accounts': {'4': {'email': 'account-0009@example.com'}}}), encoding='utf-8')
        emails = [r['email'] for r in decide.rows_from_cache()]
        assert emails == ['account-0009@example.com'], emails
        best, _ = decide.pick(decide.rows_from_cache(), set())
        assert best is not None and best['email'] == 'account-0009@example.com', best
        decide.SEQ = pathlib.Path(tmp) / 'seq-absent.json'
        emails = sorted((r['email'] for r in decide.rows_from_cache()))
        assert emails == ['account-0008@example.com', 'account-0009@example.com'], emails
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
    rows = [_drow('account-0006@example.com', '1', {'5h': 99.0}, resets='2026-09-23T15:00:00+00:00'), _drow('account-0008@example.com', '2', {'7d': 100.0}, resets=past), _drow('account-0010@example.com', '3', {'5h': 100.0}, resets=dead_soon, error='invalid_grant'), _drow('account-0009@example.com', '4', {'5h': 96.0}, resets=real)]
    fake = _SwitchFake('account-0006@example.com')
    with _Patch(rows_from_cache=lambda: rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, is_vacated=lambda e: False, _cswap=fake.cswap, SAMPLES=Path(tmp) / 'samples-soonest.json', time=type('T', (), {'time': staticmethod(lambda: now), 'sleep': staticmethod(lambda s: None)})):
        res = decide.decide(dry_run=False)
    assert res['action'] == 'blocked', res
    assert res['soonest'] == 'account-0009@example.com', res
    assert res['soonestAt'] == real, res
    return 3

def check_ledger_only_after_really_leaving(tmp: str) -> int:
    ledger = Path(tmp) / 'ledger-leave.json'
    rows = [_drow('account-0006@example.com', '1', {'5h': 98.5}, resets='2026-09-30T00:00:00+00:00'), _drow('account-0011@example.com', '2', {'5h': 20.0})]
    rows3 = rows + [_drow('account-0012@example.com', '3', {'5h': 30.0})]
    cases = ((1, _MOVE, False), (0, None, False), (0, '', False), (0, 'account-0012@example.com', True), (0, _MOVE, True))
    for rc, land, expect_in_ledger in cases:
        if ledger.exists():
            ledger.unlink()
        fake = _SwitchFake('account-0006@example.com', switch_rc=rc, land_on=land)
        with _Patch(rows_from_cache=lambda: rows3, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=ledger, SAMPLES=Path(tmp) / 'samples-leave.json', time=_NoSleep, _record_history=lambda frm, to: None):
            res = decide.decide(dry_run=False)
            in_ledger = 'account-0006@example.com' in decide.load_ledger()
        assert in_ledger is expect_in_ledger, (rc, land, res, decide._read(ledger))
    return len(cases)

def check_burned_landing_is_not_a_dead_end(tmp: str) -> int:
    ledger = Path(tmp) / 'ledger-burned.json'
    for with_t2 in (True, False):
        if ledger.exists():
            ledger.unlink()
        state = {'account-0006@example.com': 92.0, 'account-0013@example.com': 50.0, 'account-0014@example.com': 85.0}
        if not with_t2:
            del state['account-0014@example.com']

        def rows():
            return [_drow(e, str(i), {'5h': p}) for i, (e, p) in enumerate(state.items())]

        def landed(email):
            if email == 'account-0013@example.com':
                state['account-0013@example.com'] = 99.0
        fake = _SwitchFake('account-0006@example.com', after_switch=landed)
        with _Patch(rows_from_cache=rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=ledger, SAMPLES=Path(tmp) / 'samples-burned.json', time=_NoSleep, _record_history=lambda frm, to: None):
            res = decide.decide(dry_run=False)
            led = decide.load_ledger()
        res.setdefault('activeEmail', res.get('to') or res.get('active') or '')
        assert res['activeEmail'] == fake.current, (res, fake.current)
        assert fake.current != 'account-0013@example.com', (with_t2, res)
        assert 'account-0013@example.com' in led, led
        if with_t2:
            assert res['action'] == 'switched' and res['to'] == 'account-0014@example.com', res
        else:
            assert res['action'] == 'stay' and fake.current == 'account-0006@example.com', res
            assert '切回原号' in res.get('why', ''), res
            assert 'account-0006@example.com' not in led, led
    return 7

def _landing_run(tmp: str, name: str, cached: dict, landed_as: dict, land_on=_MOVE, samples=None, me='account-0006@example.com', land_once=False):
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
    res, cur, led, sw, _ = _landing_run(tmp, 'stranded', {'account-0006@example.com': 92.0, 'account-0013@example.com': 50.0, 'account-0014@example.com': 60.0, 'account-0015@example.com': 70.0}, {'account-0013@example.com': 99.5, 'account-0014@example.com': 99.5, 'account-0015@example.com': 99.5})
    assert cur == 'account-0006@example.com' and res['activeEmail'] == 'account-0006@example.com', (res, cur, sw)
    assert sw[-1] == 'account-0006@example.com' and len(sw) <= decide.MAX_SWITCH_TRIES, sw
    assert 'account-0006@example.com' not in led, led
    assert 'account-0013@example.com' in led, led
    return 4

def check_home_is_not_held_to_target_ceiling(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'ceiling', {'account-0006@example.com': 96.0, 'account-0013@example.com': 50.0}, {'account-0013@example.com': 99.0})
    assert cur == 'account-0006@example.com', (res, cur, sw)
    assert 'account-0006@example.com' not in led and 'account-0013@example.com' in led, led
    return 2

def check_third_account_landing_can_recover(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'third', {'account-0006@example.com': 92.0, 'account-0013@example.com': 80.0, 'account-0016@example.com': 96.0, 'account-0014@example.com': 88.0}, {}, land_on='account-0016@example.com', land_once=True)
    assert cur == 'account-0014@example.com' and res['action'] == 'switched' and (res['to'] == 'account-0014@example.com'), (res, sw)
    assert 'account-0006@example.com' in led, led
    res, cur, led, sw, _ = _landing_run(tmp, 'third-home', {'account-0006@example.com': 92.0, 'account-0013@example.com': 80.0, 'account-0016@example.com': 96.0}, {}, land_on='account-0016@example.com', land_once=True)
    assert cur == 'account-0006@example.com' and res['action'] == 'stay', (res, sw)
    assert 'account-0006@example.com' not in led, led
    return 5

def check_move_resets_pace_and_reports_switch(tmp: str) -> int:
    now = __import__('time').time()
    res, cur, led, sw, hist = _landing_run(tmp, 'pace', {'account-0006@example.com': 99.0, 'account-0013@example.com': 50.0}, {'account-0013@example.com': 99.5}, samples=[[now - 120, {'5h': 89.0}], [now - 60, {'5h': 94.0}], [now - 5, {'5h': 99.0}]])
    assert cur == 'account-0013@example.com', (res, cur)
    assert res['activeEmail'] == 'account-0013@example.com', res
    assert res.get('burnRate') is None and res.get('etaS') is None, res
    assert res.get('urgent') is False, res
    assert res.get('from') == 'account-0006@example.com' and res.get('to') == 'account-0013@example.com', res
    assert 'account-0006@example.com' not in led, led
    assert hist and hist[-1][-1] == 'account-0013@example.com', hist
    return 7

def check_net_move_that_stays_is_a_switch(tmp: str) -> int:
    res, cur, led, sw, _ = _landing_run(tmp, 'netmove', {'account-0006@example.com': 99.0, 'account-0013@example.com': 50.0, 'account-0016@example.com': 96.0}, {}, land_on='account-0016@example.com', land_once=True)
    assert cur == 'account-0016@example.com', (res, sw)
    assert res['action'] == 'switched' and res['to'] == 'account-0016@example.com', res
    assert res['activeEmail'] == 'account-0016@example.com', res
    assert 'account-0006@example.com' in led, led
    return 4

def check_history_identity_is_final_account(tmp: str) -> int:
    hist_file = Path(tmp) / 'history-multihop.jsonl'
    got_identity = []
    urows = [{'slot': '2', 'email': 'account-0014@example.com', 'active': False, 'error': None, 'fetched_at': 1.0, 'windows': {'5h': {'pct': 85.0, 'resets_at': ''}}}]

    def fake_collect(**kw):
        got_identity.append(kw.get('identity'))
        return [dict(r, active=r['email'] == kw.get('identity')) for r in urows]
    orig_hist, orig_collect = (decide.usage.HISTORY, decide.usage.collect)
    decide.usage.HISTORY, decide.usage.collect = (hist_file, fake_collect)
    try:
        state = {'account-0006@example.com': 92.0, 'account-0013@example.com': 50.0, 'account-0014@example.com': 85.0}

        def rows():
            return [_drow(e, str(i), {'5h': v}) for i, (e, v) in enumerate(state.items())]

        def after(email):
            if email == 'account-0013@example.com':
                state['account-0013@example.com'] = 99.0
        fake = _SwitchFake('account-0006@example.com', after_switch=after)
        with _Patch(rows_from_cache=rows, active_email=lambda: fake.current, refresh=lambda slots, budget: True, _cswap=fake.cswap, LEDGER=Path(tmp) / 'ledger-multihop.json', SAMPLES=Path(tmp) / 'samples-multihop.json', time=_NoSleep):
            res = decide.decide(dry_run=False)
    finally:
        decide.usage.HISTORY, decide.usage.collect = (orig_hist, orig_collect)
    assert res['action'] == 'switched' and res['to'] == 'account-0014@example.com', res
    assert got_identity == ['account-0014@example.com'], got_identity
    rec = json.loads(hist_file.read_text(encoding='utf-8').splitlines()[-1])
    assert 'account-0013@example.com' in rec['reason'] and 'account-0014@example.com' in rec['reason'], rec
    assert rec['accounts'][0]['active'] is True, rec
    return 4

def check_load_usage_rejects_stale_module(tmp: str) -> int:
    import shutil as _sh
    import sys as _sys
    home = Path(tmp) / 'stalehome'
    (home / 'bin').mkdir(parents=True)
    dst = home / 'bin' / 'claude-autoswitch-decide.py'
    _sh.copy(_HERE / 'claude-autoswitch-decide.py', dst)
    tools = home / '.claude' / 'tools' / 'ccpick'
    tools.mkdir(parents=True)
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
    tools = home / '.claude' / 'tools' / 'ccpick'
    tools.mkdir(parents=True)
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
        cache.write_text(json.dumps({'accounts': {'7': {'email': 'account-0006@example.com', 'fetchedAt': fetched, 'lastAttemptAt': attempted, 'lastGood': {'five_hour': {'pct': pct, 'resets_at': fut}}}}}), encoding='utf-8')
    clock = {'now': t0}
    fake_time = type('T', (), {'time': staticmethod(lambda: clock['now']), 'sleep': staticmethod(lambda s: None)})
    with _Patch(CACHE=cache, SEQ=Path(tmp) / 'seq-age-absent.json', SAMPLES=samples, time=fake_time):
        for i, pct in enumerate((64.0, 70.0, 76.0)):
            clock['now'] = t0 + 60 * i + 5
            write(t0 + 60 * i, t0 + 60 * i, pct)
            row, = decide.rows_from_cache()
            decide.record_sample('account-0006@example.com', row['used'], clock['now'] - row['age'])
        for j in (3, 4):
            clock['now'] = t0 + 60 * j + 5
            write(t0 + 120, t0 + 60 * j, 76.0)
            row, = decide.rows_from_cache()
            decide.record_sample('account-0006@example.com', row['used'], clock['now'] - row['age'])
        assert row['age'] >= 120, row
        rate, eta = decide.burn_eta('account-0006@example.com', row['used'], clock['now'])
    assert rate is not None and abs(rate - 6.0) < 0.01, (rate, eta)
    return 2

def check_soonest_waits_for_every_blocking_window(tmp: str) -> int:
    import datetime as _dt
    now = 1790000000.0
    iso = lambda s: _dt.datetime.fromtimestamp(now + s, _dt.timezone.utc).isoformat()
    cache = Path(tmp) / 'usage-soonest.json'

    def acct(email, fh, fh_at, sd, sd_at):
        return {'email': email, 'fetchedAt': now - 10, 'lastAttemptAt': now - 10, 'lastGood': {'five_hour': {'pct': fh, 'resets_at': iso(fh_at)}, 'seven_day': {'pct': sd, 'resets_at': iso(sd_at)}}}
    cache.write_text(json.dumps({'accounts': {'1': acct('account-0001@example.com', 100.0, 3600, 97.0, 3 * 86400), '2': acct('account-0004@example.com', 100.0, 7200, 50.0, 5 * 86400), '3': acct('account-0005@example.com', 100.0, 1800, 100.0, 2 * 86400)}}), encoding='utf-8')
    fake_time = type('T', (), {'time': staticmethod(lambda: now), 'sleep': staticmethod(lambda s: None)})
    (Path(tmp) / 'seq-soonest-absent.json').write_text(json.dumps({'accounts': {slot: {'email': row['email']} for slot, row in json.loads(cache.read_text(encoding='utf-8'))['accounts'].items()}}), encoding='utf-8')
    with _Patch(CACHE=cache, SEQ=Path(tmp) / 'seq-soonest-absent.json', time=fake_time):
        rows = decide.rows_from_cache()
        best = decide.soonest_recovery(rows, now)
        only_c = decide.soonest_recovery([r for r in rows if r['email'] == 'account-0005@example.com'], now)
    assert best is not None and best['email'] == 'account-0004@example.com', best
    assert best['recoversAt'] == iso(7200), best
    assert only_c['recoversAt'] == iso(2 * 86400), only_c
    return 3

def check_transient_fetch_errors_keep_target(tmp: str) -> int:
    ok_errors = (None, 'timeout', 'network', 'http-429', 'http-500', 'http-529', 'bad-response', 'transient', 'RemoteDisconnected', 'invalid_client', 'refresh-failed', 'consume-busy')
    bad_errors = ('invalid_grant', 'no_refresh_token', 'no-access-token', 'http-401', 'http-403', 'something-new')
    with _Patch(is_vacated=lambda e: False):
        for err in ok_errors:
            best, _ = decide.pick([_drow('account-0011@example.com', '2', {'5h': 30.0}, error=err)], set())
            assert best is not None, err
        for err in bad_errors:
            best, _ = decide.pick([_drow('account-0011@example.com', '2', {'5h': 30.0}, error=err)], set())
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
        email = 't%account-0017@example.com' % i
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
        decide.CACHE.write_text(json.dumps({'accounts': {'4': {'email': 'account-0001@example.com', 'lastAttemptAt': 1000.0, 'lastGood': {'five_hour': {'pct': 100.0, 'resets_at': '2020-01-01T00:00:00+00:00'}, 'seven_day': {'pct': 30.0, 'resets_at': '2099-01-01T00:00:00+00:00'}}}}}), encoding='utf-8')
        row, = decide.rows_from_cache()
    finally:
        decide.CACHE, decide.SEQ = (orig_cache, orig_seq)
    assert row['worstUsed'] == 30.0 and row['binding'] == '7d', row
    assert '5h' not in row['used'], row
    return 2

def check_switch_is_recorded_in_usage_history(tmp: str) -> int:
    hist = Path(tmp) / 'history-switch.jsonl'
    rows = [_drow('account-0006@example.com', '1', {'5h': 98.5}), _drow('account-0011@example.com', '2', {'5h': 20.0})]
    urows = [{'slot': '1', 'email': 'account-0006@example.com', 'active': False, 'error': None, 'fetched_at': 1234.5, 'windows': {'5h': {'pct': 98.5, 'resets_at': ''}}}]
    fake = _SwitchFake('account-0006@example.com')
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
    assert 'account-0006@example.com' in rec['reason'] and 'account-0011@example.com' in rec['reason'], rec
    assert rec['accounts'][0]['fetched_at'] == 1234.5, rec
    assert seen_identity == ['account-0011@example.com'], seen_identity
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
    with _Patch(CACHE=cpath, SEQ=spath, LEDGER=ledger, SAMPLES=sample_file, active_email=lambda: fake.current, refresh=lambda slots, budget=2: True, _cswap=cswap, time=_NoSleep, _record_history=lambda *a: None):
        res = decide.decide(dry_run=dry_run)
        led = decide.load_ledger()
    return (res, led, fake)
_REAL_0915 = [('1', 'account-0018@example.com', {'5h': 0, '7d': 100, 'Fable': 91}, {}, None, 300), ('2', 'account-0019@example.com', {'5h': 0, '7d': 96, 'Fable': 97}, {}, None, 300), ('6', 'account-0020@example.com', {'5h': 99, '7d': 68, 'Fable': 88}, {}, None, 200), ('8', 'account-0003@example.com', {'5h': 2, '7d': 95, 'Fable': 95}, {}, None, 100), ('9', 'account-0002@example.com', {'5h': 82, '7d': 96, 'Fable': 100}, {}, None, 300)]

def check_no_better_target_is_stay_not_blocked(tmp: str) -> int:
    n = 0
    for models in ('', 'all'):
        with _Env(CCSWITCH_MODELS=models):
            res, led, _ = _run_pool(tmp, 'stay-%s' % (models or 'default'), _REAL_0915, 'account-0003@example.com')
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
    pool = [('8', 'account-0003@example.com', {'5h': 99, '7d': 99, 'Fable': 99}, {'5h': _iso_in(hours=2), '7d': _iso_in(days=3)}, None, 100), ('9', 'account-0002@example.com', {'5h': 99, '7d': 99, 'Fable': 100}, {'5h': _iso_in(hours=1), '7d': _iso_in(days=2)}, None, 300)]
    res, _, _ = _run_pool(tmp, 'exhausted', pool, 'account-0003@example.com')
    assert res['action'] == 'blocked', res
    assert 'soonest' in res and 'soonestAt' in res, res
    assert res['soonest'] == 'account-0002@example.com', res
    return 3

def check_urgent_with_nowhere_to_go_stays(tmp: str) -> int:
    import time as _t
    now = _t.time()
    pool = [('1', 'account-0006@example.com', {'5h': 70, '7d': 30}, {}, None, 5), ('2', 'account-0001@example.com', {'5h': 96, '7d': 30}, {}, None, 50), ('3', 'account-0004@example.com', {'5h': 10, '7d': 99}, {}, None, 50)]
    res, _, _ = _run_pool(tmp, 'urgent', pool, 'account-0006@example.com', samples=[[now - 125, {'5h': 20.0, '7d': 30.0}], [now - 65, {'5h': 40.0, '7d': 30.0}]])
    assert res.get('urgent') is True, res
    assert res['action'] == 'stay' and '更好' in (res.get('why') or ''), res
    return 2

def check_all_switches_fail_but_current_usable_stays(tmp: str) -> int:
    pool3 = [('1', 'account-0006@example.com', {'5h': 92, '7d': 30}, {}, None, 5), ('2', 'account-0013@example.com', {'5h': 50, '7d': 30}, {}, None, 5), ('3', 'account-0014@example.com', {'5h': 60, '7d': 30}, {}, None, 5), ('4', 'account-0015@example.com', {'5h': 70, '7d': 30}, {}, None, 5)]
    res, led, fake = _run_pool(tmp, 'fail3', pool3, 'account-0006@example.com', dry_run=False, switch_rc=1)
    switches = [c[1] for c in fake.calls if c[:1] == ['switch']]
    assert len(switches) == decide.MAX_SWITCH_TRIES, switches
    assert res['action'] == 'stay' and '切换没成功' in (res.get('why') or ''), res
    assert res.get('switchFailed') is True, res
    assert 'account-0006@example.com' not in led, led
    res, led, _ = _run_pool(tmp, 'fail1', pool3[:2], 'account-0006@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'stay' and '试过 1 个' in (res.get('why') or ''), res
    assert res.get('switchFailed') is True, res
    hot = [('1', 'account-0006@example.com', {'5h': 98, '7d': 30}, {}, None, 5)] + pool3[1:]
    res, _, _ = _run_pool(tmp, 'fail-hot', hot, 'account-0006@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'blocked', res
    solo = [('1', 'account-0006@example.com', {'5h': 93, '7d': 30}, {}, None, 5), ('2', 'account-0013@example.com', {'5h': 99, '7d': 30}, {}, None, 5)]
    res, _, _ = _run_pool(tmp, 'nowhere', solo, 'account-0006@example.com', dry_run=False, switch_rc=1)
    assert res['action'] == 'stay' and (not res.get('switchFailed')), res
    return 9

def check_soonest_ignores_uncounted_windows(tmp: str) -> int:
    import datetime as _dt
    import time as _t
    pool = [('3', 'account-0021@example.com', {'5h': 38, '7d': 99, 'Fable': 67}, {'7d': _iso_in(days=-3)}, 'invalid_grant', 692795), ('8', 'account-0003@example.com', {'5h': 50, '7d': 99, 'Fable': 99}, {'7d': _iso_in(hours=9)}, None, 100), ('9', 'account-0002@example.com', {'5h': 50, '7d': 99, 'Fable': 100}, {'7d': _iso_in(hours=3)}, None, 300)]
    with _Env(CCSWITCH_MODELS=None):
        res, _, _ = _run_pool(tmp, 'soonest3', pool, 'account-0003@example.com')
    assert res['action'] == 'blocked', res
    assert res.get('soonest') == 'account-0002@example.com', res
    at = decide._iso_ts(res.get('soonestAt') or '')
    assert at is not None and at > _t.time(), res
    with _Env(CCSWITCH_MODELS='all'):
        res, _, _ = _run_pool(tmp, 'soonest3-all', pool, 'account-0003@example.com')
    assert res['action'] == 'blocked' and res.get('soonest') == '', res
    return 5

def check_soonest_cross_offset(tmp: str) -> int:
    import datetime as _dt
    later_utc = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)
    sooner = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)).astimezone(_dt.timezone(_dt.timedelta(hours=9)))
    pool = [('8', 'account-0001@example.com', {'5h': 10, '7d': 99}, {'7d': later_utc.isoformat()}, None, 100), ('9', 'account-0004@example.com', {'5h': 10, '7d': 99}, {'7d': sooner.isoformat()}, None, 100)]
    res, _, _ = _run_pool(tmp, 'tz', pool, 'account-0001@example.com')
    assert res['action'] == 'blocked' and res.get('soonest') == 'account-0004@example.com', res
    return 1

def check_roster_slot_and_email(tmp: str) -> int:
    ghost = [('3', 'account-0021@example.com', {'5h': 0, '7d': 10, 'Fable': 10}, {}, None, 100), ('8', 'account-0003@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100)]
    res, _, _ = _run_pool(tmp, 'ghost', ghost, 'account-0003@example.com', roster=ghost[1:])
    assert res['action'] == 'stay' and res.get('to') != 'account-0021@example.com', res
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
    assert both(cache, seq) == ['account-0003@example.com']
    cache, seq = _write_pool(tmp, 'moved', ghost, roster=[('3', 'account-0022@example.com'), ('8', 'account-0003@example.com')])
    assert both(cache, seq) == ['account-0003@example.com']
    cache, seq = _write_pool(tmp, 'case', ghost, roster=[('3', 'account-0021@example.com'), ('8', 'account-0003@example.com')])
    assert len(both(cache, seq)) == 2
    seq.write_text('{ this is not json', encoding='utf-8')
    assert len(both(cache, seq)) == 2
    seq.write_text(json.dumps({'accounts': {}}), encoding='utf-8')
    assert both(cache, seq) == []
    return n + 5

def check_model_window_display_only_by_default(tmp: str) -> int:
    pool = [('2', 'account-0019@example.com', {'5h': 0, '7d': 64, 'Fable': 97}, {}, None, 300), ('8', 'account-0003@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100), ('9', 'account-0002@example.com', {'5h': 82, '7d': 96, 'Fable': 100}, {}, None, 300)]
    with _Env(CCSWITCH_MODELS=None):
        res, _, _ = _run_pool(tmp, 'fable-default', pool, 'account-0003@example.com')
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
    pool = [('2', 'account-0019@example.com', {'5h': 0, '7d': 64, 'Fable': 97}, {}, None, 300), ('8', 'account-0003@example.com', {'5h': 2, '7d': 69, 'Fable': 95}, {}, None, 100)]
    n = 0
    for models, want_used, want_binding in (('Fable', 95.0, 'Fable'), ('fable', 95.0, 'Fable'), ('FABLE,Opus', 95.0, 'Fable'), ('all', 95.0, 'Fable'), ('Opus', 69.0, '7d'), ('none', 69.0, '7d')):
        with _Env(CCSWITCH_MODELS=models):
            res, _, _ = _run_pool(tmp, 'optin', pool, 'account-0003@example.com')
        assert res.get('used') == want_used and res.get('binding') == want_binding, (models, res)
        n += 1
    return n

def check_ledger_entry_on_uncounted_window_is_short_hold(tmp: str) -> int:
    import time as _t
    ledger = Path(tmp) / 'ledger-uncounted.json'
    ledger.write_text(json.dumps({'account-0023@example.com': {'at': _t.time() - 20 * 60, 'used': 99.0, 'binding': 'Fable', 'resetsAt': _iso_in(days=6), 'why': '已满'}, 'account-0024@example.com': {'at': _t.time() - 20 * 60, 'used': 99.0, 'binding': '7d', 'resetsAt': _iso_in(days=6), 'why': '已满'}}), encoding='utf-8')
    with _Patch(LEDGER=ledger):
        with _Env(CCSWITCH_MODELS=None):
            off = decide.is_vacated('account-0023@example.com')
            seven = decide.is_vacated('account-0024@example.com')
        with _Env(CCSWITCH_MODELS='Fable'):
            on = decide.is_vacated('account-0023@example.com')
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
    tools = home / '.claude' / 'tools' / 'ccpick'
    tools.mkdir(parents=True)
    (tools / 'ccpick_usage.py').write_text('def collect(now=None):\n    return []\n', encoding='utf-8')
    cache_dir = home / '.claude-swap-backup' / 'cache'
    cache_dir.mkdir(parents=True)
    now = _t.time()
    (cache_dir / 'usage.json').write_text(json.dumps({'accounts': {'1': {'email': 'account-0023@example.com', 'fetchedAt': now, 'lastGood': {'five_hour': {'pct': 10.0, 'resets_at': _iso_in(hours=2)}, 'seven_day': {'pct': 20.0, 'resets_at': _iso_in(days=3)}, 'scoped': [{'name': 'Fable', 'pct': 99.0, 'resets_at': _iso_in(days=2)}]}}}}), encoding='utf-8')
    (home / '.claude-swap-backup' / 'sequence.json').write_text(json.dumps({'accounts': {'1': {'email': 'account-0023@example.com'}}}), encoding='utf-8')
    (home / 'Library' / 'Logs').mkdir(parents=True)
    (home / 'Library' / 'Logs' / 'claude-autoswitch-status.json').write_text(json.dumps({'ts': now, 'activeEmail': 'account-0023@example.com'}), encoding='utf-8')
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
    decide.record_sample('account-0004@example.com', {'5h': 88.8}, 6000.0)
    decide.record_sample('account-0004@example.com', {'5h': 89.0}, 6060.0)
    pace = decide.pace_of('account-0004@example.com', {'5h': 89.0}, 6060.0)
    assert pace['urgent'] is False, pace
    assert abs(pace['nextCheckS'] - 75) < 2, pace
    _fresh(tmp)
    decide.record_sample('account-0005@example.com', {'5h': 49.8}, 6000.0)
    decide.record_sample('account-0005@example.com', {'5h': 50.0}, 6060.0)
    pace = decide.pace_of('account-0005@example.com', {'5h': 50.0}, 6060.0)
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
            decide.record_sample('account-0025@example.com', {'5h': used}, t)
            pace = decide.pace_of('account-0025@example.com', {'5h': used}, t)
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
            decide.record_sample('account-0025@example.com', {'5h': used}, t)
            pace = decide.pace_of('account-0025@example.com', {'5h': used}, t)
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
        decide.record_sample('account-0005@example.com', {'5h': used}, t0 + i * step)
    now = t0 + 4 * step
    rate, eta = decide.burn_eta('account-0005@example.com', {'5h': 57.0}, now)
    assert rate is not None, '真实缓存节奏下算不出速率 —— RATE_WINDOW_S 又太小了'
    assert abs(rate - 6.0 / (step / 60)) < 0.05, rate
    assert decide.pace_of('account-0005@example.com', {'5h': 57.0}, now)['nextCheckS'] is not None
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

    def __init__(self, add_rc: int=0, add_out: str='Added Account 8: account-0022@example.com'):
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
        out = [_row('account-0026@example.com', '3', 50.0)]
        if self.added and visible_after_add:
            out.append(_row('account-0022@example.com', '8', 40.0))
        return out

def _wire(fake: _Fake, tmp: str, visible_after_add: bool=True) -> None:
    _fresh(tmp)
    decide._cswap = fake.cswap
    decide.rows_from_cache = lambda: fake.rows(visible_after_add)
    decide.active_email = lambda: 'account-0022@example.com'
    decide.refresh = lambda slots, budget: True
    decide.is_vacated = lambda email: False

def check_unenrolled_account_is_auto_enrolled(tmp: str) -> int:
    fake = _Fake()
    _wire(fake, tmp)
    res = decide.decide(dry_run=False)
    assert ['add'] in fake.calls, fake.calls
    assert res['action'] != 'error', res
    assert res.get('active') == 'account-0022@example.com', res
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
    assert seen == [('Profile 2', 'account-0022@example.com')], seen
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
    assert 'account-0022@example.com' in res['why'], res
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

def main() -> int:
    checks = 0
    with tempfile.TemporaryDirectory() as tmp, _Env(CCSWITCH_MODELS=None):
        for fn in (check_rate_needs_two_samples, check_stale_cache_is_not_zero_rate, check_worst_window_wins, check_uncounted_model_window_does_not_drive_rate, check_pace_ignores_uncounted_for_gate_and_hot_ceiling, check_no_better_target_is_stay_not_blocked, check_truly_exhausted_still_blocked, check_urgent_with_nowhere_to_go_stays, check_all_switches_fail_but_current_usable_stays, check_soonest_ignores_uncounted_windows, check_soonest_cross_offset, check_roster_slot_and_email, check_model_window_display_only_by_default, check_ccswitch_models_opt_in, check_ledger_entry_on_uncounted_window_is_short_hold, check_helper_loads_sibling_usage, check_burst_not_averaged, check_window_expires, check_parse_active_email, check_next_check_clamped, check_removed_account_is_not_a_candidate, check_soonest_skips_past_and_dead, check_soonest_waits_for_every_blocking_window, check_ledger_only_after_really_leaving, check_burned_landing_is_not_a_dead_end, check_stranded_on_burned_goes_home, check_home_is_not_held_to_target_ceiling, check_third_account_landing_can_recover, check_move_resets_pace_and_reports_switch, check_history_identity_is_final_account, check_net_move_that_stays_is_a_switch, check_load_usage_rejects_stale_module, check_load_usage_from_home_bin_copy, check_failed_fetch_does_not_fake_fresh_data, check_transient_fetch_errors_keep_target, check_usable_implies_autoswitch_target, check_expired_window_not_counted, check_switch_is_recorded_in_usage_history, check_hot_zone_never_relaxes, check_gate_is_every_threshold_not_just_a_hundred, check_offline_round_keeps_water_level, check_interval_tracks_decision_gate_not_wall, check_slow_burn_still_used_to_the_end, check_replay_wall_2, check_replay_2026_09_20_outage, check_replay_wall_1, check_replay_normal_burn_not_twitchy, check_rate_at_real_cache_cadence, check_identity_lookup_is_bounded, check_panel_never_forks_on_open, check_no_byte_slicing_of_chinese, check_status_write_survives_bad_bytes, check_unenrolled_account_is_auto_enrolled, check_enroll_records_profile_mapping, check_mapping_failure_never_blocks_enroll, check_dry_run_never_enrolls, check_failed_enroll_says_why, check_enrolled_but_cache_lags_is_not_an_error):
            checks += fn(tmp)
    print('pace selftest: %d checks passed; 未联网、未碰真实样本文件' % checks)
    for why in _SKIPPED:
        print('  [跳过] %s' % why)
    if _SKIPPED:
        print('  ↑ %d 条因本平台限制未执行 —— 它们【没有】计入上面的 checks。' % len(_SKIPPED))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
