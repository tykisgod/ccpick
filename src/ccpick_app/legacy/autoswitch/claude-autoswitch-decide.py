from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import datetime
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

def _load_usage():
    import importlib.util
    import inspect
    here = Path(__file__).resolve().parent
    cands = [here / 'ccpick_usage.py', here.parent / 'ccpick_usage.py', _runtime.legacy_dir() / 'ccpick_usage.py']
    why_not = []
    for src in cands:
        if not src.is_file():
            why_not.append('%s (不存在)' % src)
            continue
        try:
            spec = importlib.util.spec_from_file_location('ccpick_usage', src)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            why_not.append('%s (读不了: %s)' % (src, type(e).__name__))
            continue
        missing = [n for n in ('fetch_error_kind', 'snapshot', 'collect', 'counted_windows', 'counts_toward_limit', 'managed_roster', 'in_roster', 'models_note', 'plan_info', 'capacity', 'denied_slots', 'deny_detail', 'switch_in_times', 'recent_403_times') if not hasattr(mod, n)]
        if sys.platform == 'darwin' and (not hasattr(mod, 'prepare_coordination_import')):
            missing.append('prepare_coordination_import')
        if not missing and 'identity' not in inspect.signature(mod.collect).parameters:
            missing.append('collect(identity=)')
        if missing:
            why_not.append('%s (太旧, 缺 %s)' % (src, ', '.join(missing)))
            continue
        return mod
    raise SystemExit('claude-autoswitch-decide: 没有可用的 ccpick_usage.py —— ' + '; '.join(why_not))
usage = _load_usage()
sys.path.insert(0, str(Path(usage.__file__).resolve().parent))
if sys.platform == 'darwin':
    usage.prepare_coordination_import()
CACHE = _runtime.backend_data_dir() / 'cache' / 'usage.json'
SEQ = _runtime.backend_data_dir() / 'sequence.json'
CSWAP = _backend.executable()

def _state_dir() -> Path:
    return _runtime.data_dir() / 'autoswitch'
LEDGER = _state_dir() / 'claude-autoswitch-ledger.json'
WATCH_ONLY_FLAG = _state_dir() / 'claude-autoswitch.watch-only'

def watch_only() -> bool:
    if os.environ.get('CCSWITCH_WATCH_ONLY', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return True
    return WATCH_ONLY_FLAG.exists()
FRESH_S = 90
CONSIDER_AT = float(os.environ.get('CCSWITCH_THRESHOLD', '90'))
EXHAUSTED_AT = 97.0
USABLE_CEILING = 95.0
MIN_GAIN = 10.0
SWITCH_COST = 25.0
BURNED_AT = 98.0
MAX_SWITCH_TRIES = 3
UNSURE_CHECK_S = 60.0
UNSURE_QUIET_S = 900.0
SAMPLES = _state_dir() / 'claude-autoswitch-samples.json'
RATE_WINDOW_S = 600.0
RATE_SAMPLES = 3
SAMPLE_KEEP = 40
NEXT_CHECK_MIN_S = 20.0
NEXT_CHECK_MAX_S = 300.0
NEXT_CHECK_MAX_HOT_S = 45.0
SWITCH_COST_S = 60.0
REACT_MARGIN_S = 30.0
POST_SWITCH_CHECK_S = 45.0

def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}

def _write_atomic(path: Path, obj: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.ccpick-tmp')
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
        return True
    except Exception:
        return False

def _cswap(args: list[str], timeout: int=90):
    if args[:1] == ['switch'] and watch_only():
        return None
    if args[:1] == ['switch'] and (len(args) != 2 or not _runtime.automatic_target_enabled(args[1], SEQ)):
        return None
    try:
        from ccpick_coordination import child_environment
        return _backend.run(args, capture_output=True, text=True, timeout=timeout, env=child_environment())
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None

def rows_from_cache() -> list[dict]:
    raw = _read(CACHE)
    roster = usage.managed_roster(SEQ)
    account_settings = _read(SEQ).get('accounts')
    if not isinstance(account_settings, dict):
        account_settings = {}
    plans = usage.plan_info(SEQ)
    denied = usage.deny_detail(raw.get('accounts'))
    now = time.time()
    out = []
    for slot, a in (raw.get('accounts') or {}).items():
        if not usage.in_roster(slot, a.get('email'), roster):
            continue
        lg = a.get('lastGood') or {}
        used, resets = ({}, {})

        def take(name, w):
            if w.get('pct') is None:
                return
            rst = w.get('resets_at') or ''
            ts = _iso_ts(rst)
            if ts is not None and ts <= now:
                return
            used[name] = float(w['pct'])
            resets[name] = rst
        for key, name in (('five_hour', '5h'), ('seven_day', '7d')):
            take(name, lg.get(key) or {})
        for w in lg.get('scoped') or []:
            take(w.get('name') or 'model', w)
        counted = usage.counted_windows(used)
        binding = max(counted, key=counted.get) if counted else ''
        data_ts = a.get('fetchedAt') or a.get('lastAttemptAt') or 0
        plan = plans.get(str(a.get('email') or '').lower()) or {}
        scale = plan.get('scale') or usage.DEFAULT_SCALE
        setting = account_settings.get(str(slot)) or {}
        out.append({'slot': slot, 'email': a.get('email') or '?', 'autoSwitchEnabled': _runtime.account_enabled(slot, a.get('email'), SEQ), 'used': used, 'counted': counted, 'worstUsed': counted[binding] if binding else None, 'binding': binding, 'resetsAt': resets.get(binding, ''), 'resets': resets, 'age': now - float(data_ts), 'error': a.get('lastError'), 'denied': str(slot) in denied, 'deniedAt': (denied.get(str(slot)) or {}).get('first'), 'deniedLast': (denied.get(str(slot)) or {}).get('last'), 'fetchedAt': float(a.get('fetchedAt') or 0), 'attemptAt': float(a.get('lastAttemptAt') or 0), 'scale': scale, 'plan': plan.get('plan') or '?', 'org': plan.get('org') or '', 'cap': usage.capacity(counted, scale), 'disabled': not _runtime.account_enabled(slot, a.get('email'), SEQ)})
    return out
_EMAIL_RE = re.compile('[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}')

def parse_active_email(text: str) -> str:
    for line in (text or '').splitlines():
        m = _EMAIL_RE.search(line)
        if m:
            return m.group(0)
    return ''

def active_email() -> str:
    r = _cswap(['status'], timeout=30)
    if not r or r.returncode != 0:
        return ''
    return parse_active_email(r.stdout or '')
CCPICK_DIR = _runtime.legacy_dir()

def _ccpick():
    import importlib.util
    src = CCPICK_DIR / 'ccpick.py'
    if not src.is_file():
        return None
    spec = importlib.util.spec_from_file_location('ccpick_main', src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def claim_login_profile():
    mod = _ccpick()
    return mod.claim_login_profile() if mod else None

def record_profile_account(profile_dir: str, email: str) -> None:
    mod = _ccpick()
    if mod:
        mod.record_profile_account(profile_dir, email)

def _remember_login_profile(email: str) -> None:
    try:
        profile = claim_login_profile()
        if profile and email:
            record_profile_account(profile, email)
    except Exception:
        pass

def enroll_active() -> tuple[bool, str]:
    r = _cswap(['add'], timeout=180)
    if r is None:
        return (False, 'cswap 跑不起来')
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    if r.returncode != 0:
        return (False, out[-200:] or 'cswap add 退出码 %d' % r.returncode)
    return (True, out[-200:])

def refresh(slots_needed: list[str], budget: int, *, strict_success=False) -> bool:
    result = _cswap(['auto', '--once', '--dry-run'], timeout=120)
    if strict_success and (result is None or result.returncode not in (0, 2, 3)):
        from ccpick_coordination import CoordinationError
        raise CoordinationError('usage_refresh_unavailable')
    ages = {r['slot']: r['age'] for r in rows_from_cache()}
    return all((ages.get(s, 1000000000.0) <= FRESH_S for s in slots_needed))

def _fatal_error(err) -> bool:
    return usage.fetch_error_kind(err) in ('dead', 'unknown')
EGRESS_WINDOW_S = 900.0

def _deny_verdict(r: dict, rows: list[dict], hits403: dict, explained=()) -> tuple[bool, bool, str]:
    since = float(r.get('deniedAt') or 0)
    peers = [p for p in rows if p['email'] != r['email']]
    good = [p for p in peers if not p.get('denied') and float(p.get('fetchedAt') or 0) > since]
    if not good:
        return (False, False, '池子里没有别的号可对照' if not peers else '别的 %d 个号在那之后都还没成功取到过数' % len(peers))

    def co403(p: dict) -> bool:
        ts = list(hits403.get(str(p.get('slot')), ()))
        if p.get('error') == usage.DENIED_ERROR and p.get('attemptAt'):
            ts.append(float(p['attemptAt']))
        return any((abs(t - since) <= EGRESS_WINDOW_S for t in ts))
    egress = [p for p in peers if p['email'] not in explained and float(p.get('fetchedAt') or 0) > 0 and co403(p)]
    if not egress:
        return (True, False, '')
    first_good = min((float(p['fetchedAt']) for p in good))
    if r.get('error') and float(r.get('attemptAt') or 0) >= first_good:
        return (True, True, '')
    return (False, True, '别的号 (%s) 在同一时段也回过 403, 像是网络出口的问题; 等它在网络通了之后再试一次' % ', '.join((p['email'] for p in egress[:3])))

def _unusable(r: dict) -> bool:
    return _fatal_error(r.get('error')) or bool(r.get('denied'))

def auto_switch_enabled(r: dict) -> bool:
    return r.get('autoSwitchEnabled') is not False

def _iso_ts(iso: str) -> float | None:
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None

def soonest_recovery(rows: list[dict], now: float) -> dict | None:
    rows = [row for row in rows if not row.get('disabled')]
    '全部没得切时, 报"最早哪个号、几点能回来"。\n\n    ★只报将来的时刻、只报回得来的号★ (2026-09-24)\n    原来是 min(resetsAt) 一把梭。线上实测: 09-19 ~ 09-20 的 1334 条 BLOCKED 里 1316 条\n    报的是一个幽灵账号 18 天前的 2026-09-01T09:59, 托盘只显示"最早恢复 09:59", 看着像个\n    正常时刻, 其实早就过了。凭据死了的号恢复时刻再近也没用: 它回不来。\n\n    ★一个号要等【所有】卡着它的闸都恢复才回得来★ (2026-09-24 审查)\n    卡着它的 = 用量 >= USABLE_CEILING 的那几道 (pick() 就是按这条线拒的)。只看最紧那道时,\n    5h 100% + 7d 97% 的号会被报成"5h 恢复就回来", 可那时 7d 仍卡着; 两道都 100% 时\n    max() 取到先插入的 5h, 报"几小时后", 其实要等好几天。\n    返回的行多一个 recoversAt (ISO)。\n\n    ★只数【算数的】闸★ (2026-09-24, 同 rows_from_cache 的 counted): 不计数的 Fable 99%\n    又没有 resets_at 时, 原来整行被当成"说不准"跳过, 真正最早回来的号报不出来。\n    '
    best = None
    for r in rows:
        if not auto_switch_enabled(r) or _unusable(r):
            continue
        resets = r.get('resets') or {r.get('binding') or '': r.get('resetsAt') or ''}
        counted = r['counted'] if 'counted' in r else usage.counted_windows(r.get('used') or {})
        blocking = [k for k, v in counted.items() if v >= USABLE_CEILING]
        if not blocking:
            continue
        stamps = [(_iso_ts(resets.get(k) or ''), resets.get(k) or '') for k in blocking]
        if any((ts is None or ts <= now for ts, _ in stamps)):
            continue
        ts, iso = max(stamps)
        if best is None or ts < best[0]:
            best = (ts, dict(r, recoversAt=iso))
    return best[1] if best else None

def load_ledger() -> dict:
    return _read(LEDGER)

def vacate(email: str, used: float, binding: str, resets_at: str, full: bool | None=None) -> None:
    if not email:
        return
    if used >= BURNED_AT if full is None else full:
        rec = {'at': time.time(), 'used': used, 'binding': binding, 'resetsAt': resets_at or '', 'why': '已满'}
    else:
        rec = {'at': time.time(), 'used': used, 'binding': binding, 'resetsAt': '', 'why': '超阈值, 只压短时'}
    led = load_ledger()
    led[email] = rec
    _write_atomic(LEDGER, led)

def is_vacated(email: str, full_only: bool=False) -> bool:
    rec = load_ledger().get(email)
    if not rec:
        return False
    iso = rec.get('resetsAt') or ''
    binding = rec.get('binding') or ''
    if binding and (not usage.counts_toward_limit(binding)):
        iso = ''
    if iso:
        ts = _iso_ts(iso)
        if ts is not None:
            return time.time() < ts
    if full_only:
        return False
    return time.time() - float(rec.get('at') or 0) < 600

def record_sample(email: str, used: dict, data_ts: float) -> None:
    s = _read(SAMPLES)
    if s.get('email') != email:
        s = {'email': email, 'samples': []}
    arr = [x for x in s.get('samples') or [] if isinstance(x, list) and len(x) == 2 and isinstance(x[1], dict)]
    if arr and abs(float(arr[-1][0]) - data_ts) < 1.0:
        arr[-1] = [data_ts, used]
    else:
        arr.append([data_ts, used])
    s['samples'] = arr[-SAMPLE_KEEP:]
    _write_atomic(SAMPLES, s)

def burn_eta(email: str, used_now: dict, now: float) -> tuple[float | None, float | None]:
    s = _read(SAMPLES)
    if s.get('email') != email:
        return (None, None)
    arr = [x for x in s.get('samples') or [] if isinstance(x, list) and len(x) == 2 and isinstance(x[1], dict) and (0 <= now - float(x[0]) <= RATE_WINDOW_S)][-RATE_SAMPLES:]
    if len(arr) < 2:
        return (None, None)
    best = None
    for name, cur in (used_now or {}).items():
        if not usage.counts_toward_limit(name):
            continue
        pts = [(float(t), float(u[name])) for t, u in arr if u.get(name) is not None]
        if len(pts) < 2:
            continue
        rate = 0.0
        for i in range(1, len(pts)):
            dt = (pts[i][0] - pts[i - 1][0]) / 60.0
            if dt > 0:
                rate = max(rate, (pts[i][1] - pts[i - 1][1]) / dt)
        if rate <= 0:
            continue
        eta = max(0.0, 100.0 - float(cur)) / rate * 60.0
        if best is None or eta < best[1]:
            best = (rate, eta)
    return best if best else (None, None)

def next_check_s(eta: float | None, worst_used: float | None=None) -> float | None:
    if eta is None:
        return None
    ceiling = NEXT_CHECK_MAX_S
    if worst_used is not None and float(worst_used) >= CONSIDER_AT:
        ceiling = NEXT_CHECK_MAX_HOT_S
    return max(NEXT_CHECK_MIN_S, min(ceiling, eta / 4.0))

def eta_to_gate(used_now: dict, rate: float | None) -> float | None:
    if not rate or rate <= 0:
        return None
    worst = max((float(v) for v in usage.counted_windows(used_now).values()), default=None)
    if worst is None:
        return None
    ahead = [g for g in (CONSIDER_AT, EXHAUSTED_AT, 100.0) if g > worst]
    gate = min(ahead) if ahead else 100.0
    return max(0.0, gate - worst) / rate * 60.0

def pace_of(email: str, used_now: dict, now: float, *, data_age_s: float=0) -> dict:
    used_now = usage.counted_windows(used_now)
    age = max(0.0, float(data_age_s)) if math.isfinite(float(data_age_s)) else 0.0
    rate, eta = burn_eta(email, used_now, now - age) if age <= RATE_WINDOW_S else (None, None)
    if eta is not None:
        eta = max(0.0, eta - age)
    worst = max((float(v) for v in (used_now or {}).values()), default=None)
    gate_eta = eta_to_gate(used_now, rate)
    if gate_eta is not None:
        gate_eta = max(0.0, gate_eta - age)
    nxt = next_check_s(gate_eta, worst)
    react_s = (nxt if nxt is not None else NEXT_CHECK_MAX_S) + SWITCH_COST_S + REACT_MARGIN_S
    return {'burnRate': round(rate, 1) if rate else None, 'etaS': round(eta) if eta is not None else None, 'nextCheckS': round(nxt) if nxt is not None else None, 'consider': CONSIDER_AT, 'urgent': eta is not None and eta <= react_s}

def _record_history(chain: str, final: str) -> None:
    try:
        usage.snapshot(usage.collect(identity=final), 'autoswitch: %s' % chain)
    except Exception:
        pass

def _round(x: float | None) -> float | None:
    return None if x is None else round(float(x), 1)

def cap_of(r: dict | None) -> float | None:
    if not r:
        return None
    if r.get('cap') is not None:
        return float(r['cap'])
    counted = r.get('counted')
    if counted is None:
        counted = usage.counted_windows(r.get('used') or {})
    return usage.capacity(counted, r.get('scale'))

def past(r: dict | None, pct: float) -> bool:
    if not r or r.get('worstUsed') is None:
        return False
    if r['worstUsed'] < pct:
        return False
    return cap_floor(r) <= usage.WEEK_TO_5H * (100.0 - pct)

def cap_floor(r: dict | None) -> float:
    if not r:
        return 0.0
    counted = r.get('counted')
    if counted is None:
        counted = usage.counted_windows(r.get('used') or {})
    c = usage.capacity({k: min(100.0, float(v) + 1.0) for k, v in counted.items() if v is not None}, r.get('scale'))
    return c or 0.0

def cap_binding(r: dict) -> tuple[str, str]:
    counted = r.get('counted') or usage.counted_windows(r.get('used') or {})
    best = None
    for k, v in counted.items():
        if v is None:
            continue
        room = max(0.0, 100.0 - float(v)) * (1.0 if k == '5h' else usage.WEEK_TO_5H)
        if best is None or room < best[0]:
            best = (room, k)
    if best is None:
        return (r.get('binding') or '', r.get('resetsAt') or '')
    return (best[1], (r.get('resets') or {}).get(best[1]) or (r.get('resetsAt') or '' if best[1] == r.get('binding') else ''))

def switch_cost(frm: dict | None, to: dict | None) -> float:
    a = (frm or {}).get('org') or ''
    b = (to or {}).get('org') or ''
    return SWITCH_COST if a and b and (a != b) else 0.0

def net_cap(r: dict, frm: dict | None) -> float:
    return (cap_of(r) or 0.0) - switch_cost(frm, r)

def _week_reset_ts(r: dict) -> float:
    resets = r.get('resets') or {}
    counted = r.get('counted') or usage.counted_windows(r.get('used') or {})
    ts = [_iso_ts(resets.get(k) or '') for k in counted if k != '5h']
    ts = [t for t in ts if t is not None]
    return min(ts) if ts else float('inf')

def describe(r: dict | None) -> str:
    if not r:
        return '?'
    c = cap_of(r)
    plan = r.get('plan')
    return '%s%s 折 %s 点' % (r.get('email') or '?', ' (%s)' % plan if plan and plan != '?' else '', '?' if c is None else '%.0f' % c)

def targets(rows: list[dict], exclude: set[str], escape: bool=False) -> list[dict]:
    base = [r for r in rows if r['email'] not in exclude and auto_switch_enabled(r) and (r['worstUsed'] is not None) and (not _unusable(r)) and (not (is_vacated(r['email'], full_only=True) if escape else is_vacated(r['email']))) if not r.get('disabled', False)]
    if escape:
        return base
    return [r for r in base if not past(r, USABLE_CEILING) if not r.get('disabled', False)]

def pick(rows: list[dict], exclude: set[str], frm: dict | None=None, gate=None, escape: bool=False, avoid_orgs=()) -> tuple[dict | None, str]:
    ok = targets(rows, exclude, escape=escape)
    if gate is not None:
        ok = [r for r in ok if gate(r) if not r.get('disabled', False)]
    if avoid_orgs:
        ok = [r for r in ok if not r.get('org') or r['org'] not in avoid_orgs if not r.get('disabled', False)] or ok
    if not ok:
        return (None, '')
    top = max((net_cap(r, frm) for r in ok if not r.get('disabled', False)))
    near = [r for r in ok if top > 0 and net_cap(r, frm) >= 0.75 * top if not r.get('disabled', False)] or [r for r in ok if net_cap(r, frm) == top if not r.get('disabled', False)]
    near = [r for r in near if not past(r, CONSIDER_AT) or net_cap(r, frm) >= top - MIN_GAIN if not r.get('disabled', False)]
    best = min(near, key=lambda r: (_week_reset_ts(r), -net_cap(r, frm), r['age']))
    tier = '宽裕' if not past(best, CONSIDER_AT) else '★都不宽裕, 挑矮子里的高个'
    return (best, tier)

def decide_quota_only(dry_run: bool, allow_switch: bool=True, *, refresh_usage=True) -> dict:
    import contextlib
    from ccpick_coordination import Coordinator, CoordinationError, strict_roster, quota_state, child_environment
    from ccpick_enroll import auth_status
    from ccpick_auto import _switch_and_verify_uncoordinated
    base = {'policy': 'quota-only', 'nextCheckS': 60, 'urgent': False}
    observe_only = dry_run or not allow_switch or watch_only()
    try:
        child_environment()
        with contextlib.nullcontext(None) if observe_only else Coordinator(can_mutate=lambda: allow_switch and (not watch_only())) as coordinator:
            if coordinator and coordinator.pending and (coordinator.journal['phase'] in ('switching', 'failed')):
                raise CoordinationError('manual_verified_switch_required')
            roster = strict_roster(usage, SEQ)
            identity = auth_status().get('email')
            if not identity or identity not in roster.values():
                raise CoordinationError('active_identity_not_in_device_pool')
            if refresh_usage:
                refresh([], budget=2, strict_success=True)
            roster = strict_roster(usage, SEQ)
            if auth_status().get('email') != identity:
                raise CoordinationError('active_identity_changed_during_check')
            rows = rows_from_cache()
            rows = [r for r in rows if usage.in_roster(r['slot'], r['email'], roster)]
            current = next((r for r in rows if r['email'] == identity), None)
            state = quota_state(current)
            if not allow_switch or watch_only():
                return {**base, 'action': 'stay', 'watchOnly': True, 'why': 'watch_only_enabled'}
            if state == 'unknown':
                return {**base, 'action': 'stay', 'why': 'quota_evidence_unavailable_wait_for_refresh', 'gatePaused': bool(coordinator and coordinator.pending)}
            if state == 'available':
                if coordinator and coordinator.pending:

                    def resume_guard():
                        current_roster = strict_roster(usage, SEQ)
                        if auth_status().get('email') != identity:
                            raise CoordinationError('active_identity_changed_before_resume')
                        latest = next((r for r in rows_from_cache() if r['email'] == identity and usage.in_roster(r['slot'], r['email'], current_roster)), None)
                        if quota_state(latest) != 'available':
                            raise CoordinationError('quota_evidence_expired_before_resume')
                    coordinator.resume_current(guard=resume_guard)
                return {**base, 'action': 'stay', 'why': 'current_quota_available', 'windows': current['used'], 'used': current['worstUsed']}
            fresh = [r for r in rows if quota_state(r) == 'available']
            target, _ = pick(fresh, {identity}, frm=current)
            if observe_only:
                return {**base, 'action': 'would-switch' if dry_run and target and allow_switch else 'stay', 'watchOnly': not allow_switch or watch_only(), 'wouldSwitch': bool(target), 'why': 'confirmed_quota_exhausted_observation_only'}
            if target is None:
                coordinator.hold()
                soonest = soonest_recovery(rows, time.time())
                return {**base, 'action': 'blocked', 'gatePaused': True, 'why': 'confirmed_quota_exhausted_wait_for_pool_recovery', 'soonestAt': soonest['recoversAt'] if soonest else ''}

            def guard():
                if watch_only():
                    raise CoordinationError('watch_only_enabled')
                current_roster = strict_roster(usage, SEQ)
                if auth_status().get('email') != identity:
                    raise CoordinationError('active_identity_changed_before_switch')
                latest = {r['email']: r for r in rows_from_cache() if usage.in_roster(r['slot'], r['email'], current_roster)}
                if not auto_switch_enabled(latest.get(target['email'], {})):
                    raise CoordinationError('automatic_selection_disabled')
                if quota_state(latest.get(identity)) != 'exhausted' or quota_state(latest.get(target['email'])) != 'available':
                    raise CoordinationError('quota_evidence_expired_before_switch')
            coordinator.switch(lambda: _switch_and_verify_uncoordinated(CSWAP, target['email'])[0], guard=guard)
            vacate(identity, current['worstUsed'], current['binding'], current['resetsAt'], full=True)
            return {**base, 'action': 'switched', 'why': 'confirmed_quota_switch_verified', 'gatePaused': False}
    except CoordinationError as error:
        return {**base, 'action': 'error', 'why': str(error), 'recovery': 'Verify the local pool and gate; after a failed switch use ccpick switch with an explicit local target.'}
    except Exception:
        return {**base, 'action': 'error', 'why': 'quota_data_or_coordination_unavailable'}

def decide(dry_run: bool, allow_switch: bool=True) -> dict:
    from ccpick_coordination import enabled, CoordinationError
    try:
        if enabled():
            return decide_quota_only(dry_run, allow_switch)
    except CoordinationError as error:
        return {'action': 'error', 'why': str(error)}
    return decide_predictive(dry_run, allow_switch)

def decide_predictive(dry_run: bool, allow_switch: bool=True, *, manual: bool=False) -> dict:
    rows = rows_from_cache()
    if not rows:
        return {'action': 'error', 'why': '读不到 cswap 用量缓存'}
    me_email = active_email()
    if not me_email:
        return {'action': 'error', 'why': '认不出当前活跃账号'}
    by_email = {r['email']: r for r in rows}
    me = by_email.get(me_email)
    if not me:
        if dry_run:
            return {'action': 'error', 'why': '活跃账号 %s 不在 cswap 池子里; 真跑时会自动 `cswap add` 入库, --dry-run 不动手' % me_email}
        ok, detail = enroll_active()
        if ok:
            _remember_login_profile(me_email)
        if not ok:
            return {'action': 'error', 'why': '活跃账号 %s 不在 cswap 池子里, 自动入库失败: %s' % (me_email, detail)}
        rows = rows_from_cache()
        by_email = {r['email']: r for r in rows}
        me = by_email.get(me_email)
        if not me:
            return {'action': 'stay', 'active': me_email, 'used': None, 'binding': None, 'windows': {}, 'why': '已自动入库 %s; 用量缓存还没跟上, 下一轮再判' % me_email}
    fresh_ok = refresh([me['slot']], budget=2)
    rows = rows_from_cache()
    me = {r['email']: r for r in rows}[me_email]
    dead_me = bool(me.get('denied'))
    deny_note = '当前号 %s %s' % (me_email, usage.DENIED_NOTE)
    now = time.time()
    switched_in_after = False
    if dead_me:
        sw = usage.switch_in_times().get(str(me['slot']))
        if sw is not None and float(me.get('deniedLast') or 0) < sw:
            dead_me, switched_in_after = (False, True)
    hits403 = usage.recent_403_times()
    clean_confirm = False
    if dead_me:
        confirmed, strict, unsure_why = _deny_verdict(me, rows, hits403)
        clean_confirm = confirmed and (not strict)
        if not confirmed:
            waited = max(0.0, now - float(me.get('deniedAt') or now))
            return {'action': 'blocked', 'active': me_email, 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], 'cap': None, 'activeDenied': True, 'denyUnsure': True, 'quiet': waited < UNSURE_QUIET_S, 'why': '%s; %s —— 分不清是这个号被停还是网络出口被拦, 先不切' % (deny_note, unsure_why), 'notice': '%s 被拒 (403) %.0f 分钟了; 分不清是被封还是网络出口的问题, 先没换号' % (me_email, waited / 60.0), 'burnRate': None, 'etaS': None, 'nextCheckS': UNSURE_CHECK_S, 'consider': CONSIDER_AT, 'urgent': False}
    if me['worstUsed'] is None and (not dead_me):
        return {'action': 'error', 'why': '拿不到活跃账号的用量'}
    if dead_me:
        pace = {'burnRate': None, 'etaS': None, 'nextCheckS': None, 'consider': CONSIDER_AT, 'urgent': False}
    else:
        record_sample(me_email, me['used'], now - float(me['age']))
        pace = pace_of(me_email, me['used'], now, data_age_s=me['age'])
    urgent = pace['urgent']
    if not manual and (not dead_me) and (not past(me, CONSIDER_AT)) and (not urgent):
        why = '还有额度, 继续用'
        if me['worstUsed'] >= CONSIDER_AT:
            why = '%s: 水位 %.0f%% 但还多, 继续用' % (describe(me), me['worstUsed'])
        if switched_in_after:
            why += '; 它上次被拒 (403) 在切过来之前, 等它下一次取数再看'
        return {'action': 'stay', 'active': me['email'], 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], 'why': why, 'dataAge': round(me['age']), 'fresh': fresh_ok, 'cap': _round(cap_of(me)), **pace}
    refresh([r['slot'] for r in rows_from_cache() if auto_switch_enabled(r)], budget=2)
    tried: set[str] = {me['email']}
    cur = me
    hops: list[str] = []

    def burned(r: dict) -> bool:
        return past(r, BURNED_AT)

    def roomier(a: dict, b: dict) -> bool:
        if b.get('worstUsed') is None or b.get('denied'):
            return True
        cb = cap_floor(b) if burned(b) else cap_of(b) or 0.0
        return (cap_of(a) or 0.0) > cb

    def home_option() -> dict | None:
        if me.get('disabled'):
            return None
        '已经被动挪了窝之后, 原号还能不能退回去。\n\n        ★不受 USABLE_CEILING 管★ (2026-09-24 第二轮审查) —— 那条线是给【新】目标的\n        ("它也快死了, 换过去只是把撞墙推迟几十秒"); 退回原号比的是"比现在待的地方好不好"。\n        原来原号 96% 时回不去, 人被留在 99% 的号上。只拦死号和烧干的。\n        '
        if not auto_switch_enabled(me) or _unusable(me) or me['worstUsed'] is None or burned(me):
            return None
        if not roomier(me, cur):
            return None
        return me

    def soonest_extra() -> dict:
        soonest = soonest_recovery(rows_from_cache(), time.time())
        return {'soonest': soonest['email'] if soonest else '', 'soonestAt': soonest['recoversAt'] if soonest else ''}

    def conclude(action: str, extra: dict) -> dict:
        chain = ' → '.join([me['email']] + hops)
        if dead_me or cur.get('denied'):
            extra = dict(extra)
            moved = cur['email'] != me['email']
            parts = [deny_note] if dead_me else []
            if moved:
                if dead_me:
                    extra['deniedFrom'] = me['email']
                parts.append(('换到的 %s 也被拒 (http-403)' if cur.get('denied') else '已换到 %s') % cur['email'])
            if cur.get('denied'):
                action = 'blocked'
                extra['activeDenied'] = True
            if extra.get('why'):
                parts.append(extra['why'])
            extra['why'] = '; '.join(parts)
            if not moved:
                tail = '切到别的号没成功, 请手动切' if extra.get('switchFailed') else '别的号也都没有余量'
            elif cur.get('denied'):
                tail = '换到的 %s 也被拒' % cur['email']
            elif action == 'blocked':
                tail = '换到的 %s 也满了' % cur['email']
            else:
                tail = '已换到 %s' % cur['email']
            extra['notice'] = '%s 疑似被封 (403); %s' % (me['email'], tail) if dead_me else '换到的 %s 被拒 (403), 疑似被封; 别的号也去不了' % cur['email']
            if extra.get('denyUnsure') and moved:
                extra['notice'] = '%s换到的 %s 也被拒 (403); 分不清是被封还是网络出口的问题, 先不往下换' % ('%s 疑似被封; ' % me['email'] if dead_me else '', cur['email'])
        if cur['email'] == me['email']:
            if hops:
                _record_history(chain, me['email'])
            return {'action': action, 'active': me['email'], 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], 'cap': _round(cap_of(me)), **extra, **pace}
        if not burned(cur) and (not cur.get('denied')):
            full = burned(me)
            b_name, b_reset = cap_binding(me) if full else (me['binding'], me['resetsAt'])
            vacate(me['email'], me['worstUsed'], b_name, b_reset, full=full)
        _write_atomic(SAMPLES, {'email': cur['email'], 'samples': []})
        _record_history(chain, cur['email'])
        return {'action': 'switched' if action == 'stay' else action, 'from': me['email'], 'to': cur['email'], 'fromUsed': me['worstUsed'], 'toUsed': cur.get('worstUsed'), 'active': cur['email'], 'used': cur.get('worstUsed'), 'binding': cur.get('binding'), 'windows': cur.get('used') or {}, 'fromCap': _round(cap_of(me)), 'cap': _round(cap_of(cur)), **extra, **{**pace, 'burnRate': None, 'etaS': None, 'urgent': False, 'nextCheckS': round(POST_SWITCH_CHECK_S)}}
    exhausted = dead_me or past(me, EXHAUSTED_AT)
    forced = urgent or exhausted
    me_cap = 0.0 if dead_me else cap_floor(me) if exhausted else cap_of(me) or 0.0

    def cost_to(t: dict) -> float:
        return 0.0 if forced else switch_cost(me, t)

    def gain_of(t: dict) -> float:
        return (cap_of(t) or 0.0) - cost_to(t) - me_cap

    def worth(t: dict) -> bool:
        g = gain_of(t)
        return g > 0 if exhausted else g >= MIN_GAIN

    def dead_orgs() -> set:
        return {r.get('org') for r in (me, cur) if r.get('denied') and r.get('org')}
    for attempt in range(MAX_SWITCH_TRIES):
        last = attempt == MAX_SWITCH_TRIES - 1
        rows = rows_from_cache()
        if cur['email'] == me['email']:
            target, tier = pick(rows, exclude=tried, frm=None if dead_me else me, gate=worth, escape=dead_me, avoid_orgs=dead_orgs())
            if target is None:
                others = sorted(tried - {me['email']})
                failed = {'switchFailed': True} if others else {}
                tried_note = '试过 %d 个 (%s) 都没切过去; ' % (len(others), ', '.join(others)) if others else ''
                near = targets(rows, tried, escape=dead_me)
                if dead_me:
                    return conclude('blocked', {'why': tried_note + ('别的号也都去不了' if not near else '别的号也都没有余量'), **failed, **soonest_extra()})
                if exhausted and (others or not near):
                    return conclude('blocked', {**({'why': tried_note + ('剩下的都不能去' if not near else '剩下能去的都不比当前 %s 多' % describe(me))} if others else {}), **failed, **soonest_extra()})
                if exhausted:
                    best = max(near, key=lambda t: cap_of(t) or 0.0)
                    why = '%s当前 %s (往满里读剩 %.0f), 能去的都不比它多 (最多的 %s); 先用着, 用到比它们少再换' % (tried_note, describe(me), me_cap, describe(best))
                elif near:
                    best = max(near, key=gain_of)
                    cost = cost_to(best)
                    g = gain_of(best)
                    why = '%s没有明显更好的去处: 最好的目标 %s, 当前 %s; %s (要多 %.0f 才值得切%s); 继续把当前这个用到 %.0f%% 再说' % (tried_note, describe(best), describe(me), '切过去只多 %.0f 点' % g if g >= 0.5 else '切过去反而少 %.0f 点' % -g if g <= -0.5 else '切过去一样多', MIN_GAIN, '; 跨组织切一次先花 %.0f 点重建缓存' % cost if cost else '', EXHAUSTED_AT)
                else:
                    why = '没有更好的去处 (%s最紧那道闸都 >= %.0f%%); 当前这个 %.0f%% 还有余量, 继续用到 %.0f%%' % ('试过 %d 个 (%s) 都没切过去, 剩下的' % (len(others), ', '.join(others)) if others else '别的号', USABLE_CEILING, me['worstUsed'], EXHAUSTED_AT)
                return conclude('stay', {'why': why, **failed, 'dataAge': round(me['age']), 'fresh': fresh_ok})
        else:
            esc = bool(cur.get('denied'))
            target, tier = pick(rows, exclude=tried, frm=None if esc else cur, escape=esc, avoid_orgs=dead_orgs())
            home = home_option()
            cands = [home] if last and home is not None else [x for x in (target, home) if x is not None]
            best = max(cands, key=lambda r: cap_of(r) or 0.0) if cands else None
            if best is None or not roomier(best, cur):
                return conclude('blocked' if burned(cur) else 'stay', {'why': '落到的 %s 不能用, 能去的都不比它好' % cur['email'], **soonest_extra()})
            if best is home:
                target, tier = (home, '回原号')
            else:
                target = best
        if not allow_switch:
            return {'action': 'stay', 'watchOnly': True, 'active': me['email'], 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], 'cap': _round(cap_of(me)), 'wouldSwitchTo': target['email'], 'toUsed': target['worstUsed'], 'toCap': _round(cap_of(target)), 'tier': tier, 'why': '只看不切: 按规则该切到 %s (%s), 自动切换已关' % (target['email'], describe(target)), **({'currentDenied': True, 'why': '%s; 只看不切: 该切到 %s (%s), 自动切换已关' % (deny_note, target['email'], describe(target)), 'notice': '%s 疑似被封 (403); 自动切换已关, 请手动切到 %s' % (me['email'], target['email'])} if dead_me else {}), 'dataAge': round(me['age']), 'fresh': fresh_ok, **pace}
        if dry_run:
            return {'action': 'would-switch', 'from': me['email'], 'to': target['email'], 'fromUsed': me['worstUsed'], 'toUsed': target['worstUsed'], 'fromCap': _round(cap_of(me)), 'toCap': _round(cap_of(target)), 'toAge': round(target['age']), 'tier': tier, **({'deniedFrom': me['email'], 'why': deny_note, 'notice': '%s 疑似被封 (403); 该换到 %s' % (me['email'], target['email'])} if dead_me else {}), 'windows': target['used'], **pace}
        tried.add(target['email'])
        r = _cswap(['switch', target['email']], timeout=60)
        if r is None or r.returncode != 0:
            continue
        time.sleep(1.0)
        landed_email = active_email()
        if not landed_email or landed_email == cur['email']:
            continue
        by = {x['email']: x for x in rows_from_cache()}
        if landed_email in by:
            refresh([by[landed_email]['slot']], budget=3)
        landed = {x['email']: x for x in rows_from_cache()}.get(landed_email)
        hops.append(landed_email)
        cur = landed or {'email': landed_email, 'worstUsed': None, 'binding': '', 'used': {}, 'resetsAt': '', 'age': 0.0, 'error': None}
        if burned(cur):
            b_name, b_reset = cap_binding(cur)
            vacate(landed_email, cur['worstUsed'], b_name, b_reset, full=True)
            continue
        if cur.get('denied') and cur['email'] != me['email']:
            if dead_me and clean_confirm:
                continue
            ok, _strict, unsure_why = _deny_verdict(cur, rows_from_cache(), usage.recent_403_times(), explained={me['email']} if dead_me else set())
            if ok:
                continue
            waited = max(0.0, time.time() - float(cur.get('deniedAt') or time.time()))
            return conclude('blocked', {'denyUnsure': True, 'quiet': waited < UNSURE_QUIET_S, 'why': '落地发现也被拒 (403); %s —— 先停在这里, 不再往下换' % unsure_why})
        if landed_email not in (target['email'], me['email']) and (cur.get('worstUsed') is None or past(cur, USABLE_CEILING)):
            continue
        if cur['email'] == me['email']:
            return conclude('stay', {'why': '落到的 %s 也满了, 已切回原号' % ' → '.join(hops[:-1])})
        extra = {'tier': tier}
        if len(hops) > 1:
            extra['why'] = '中途落到 %s 不能用, 最后停在 %s' % (' → '.join(hops[:-1]), cur['email'])
        return conclude('switched', extra)
    if cur['email'] == me['email'] and (not dead_me) and (not past(me, EXHAUSTED_AT)):
        return conclude('stay', {'why': '切换没成功 (连试 %d 个: %s); 当前这个 %.0f%% 还能用, 继续用到 %.0f%%' % (MAX_SWITCH_TRIES, ', '.join(sorted(tried - {me['email']})), me['worstUsed'], EXHAUSTED_AT), 'switchFailed': True, 'dataAge': round(me['age']), 'fresh': fresh_ok})
    stuck = cur['email'] == me['email'] and bool(tried - {me['email']})
    return conclude('blocked' if cur['email'] == me['email'] or burned(cur) else 'stay', {'why': '切换没成功 (连试 %d 个: %s)%s' % (MAX_SWITCH_TRIES, ', '.join(sorted(tried - {me['email']})), '' if dead_me else ', 当前号也快满了') if stuck else '连试 %d 个都不可用' % MAX_SWITCH_TRIES, **({'switchFailed': True} if stuck else {}), **soonest_extra()})

def _finalize(res: dict) -> dict:
    res.setdefault('activeEmail', res.get('to') or res.get('active') or '')
    res['countedWindows'] = sorted(usage.counted_windows(res.get('windows') or {}))
    return res

def main() -> int:
    sys.path.insert(0, str(_runtime.legacy_dir()))
    from ccpick_app.account_context import dispatch
    unified = dispatch('autoswitch', ['auto', *sys.argv[1:]])
    if unified is not None:
        return unified
    args = sys.argv[1:]
    if '-h' in args or '--help' in args:
        print('用法: claude-autoswitch-decide.py [--dry-run]')
        print('  --dry-run   只判断并打印结果，不切账号')
        print('  不带参数    真的执行切换')
        print('退出码: 0=切了  2=不用切 (含"没有更好的去处")  3=没得切 (当前号也 >= %.0f%%)  1=出错' % EXHAUSTED_AT)
        print('环境变量: CCSWITCH_THRESHOLD (默认 90)  CCSWITCH_MODELS (默认空 = 每模型周窗口只显示不计入; Fable / all)')
        print('  当前: %s' % usage.models_note())
        print('只看不切: 文件 %s 存在 (或 CCSWITCH_WATCH_ONLY=1) 时照常判断但不切号, 该换号时只报 wouldSwitchTo' % WATCH_ONLY_FLAG)
        print('  当前: %s' % ('开 (不会自动切号)' if watch_only() else '关 (会自动切号)'))
        return 0
    unknown = [a for a in args if a != '--dry-run']
    if unknown:
        print('未知参数: %s；本脚本只接受 --dry-run' % ' '.join(unknown), file=sys.stderr)
        print('（不带 --dry-run 会真的切账号，所以拼错的参数一律按出错处理，不替你猜。）', file=sys.stderr)
        return 1
    wo = watch_only()
    res = _finalize(decide('--dry-run' in args, allow_switch=False) if wo else decide('--dry-run' in args))
    if wo:
        res['watchOnly'] = True
    print(json.dumps(res, ensure_ascii=False))
    return {'stay': 2, 'switched': 0, 'would-switch': 0, 'blocked': 3}.get(res['action'], 1)
if __name__ == '__main__':
    sys.exit(main())
