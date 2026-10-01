from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import datetime
import math
import hashlib
import os
import random
import sys
import time
from pathlib import Path
CCPICK = _runtime.legacy_dir()

def _wait_context() -> str | None:
    return _runtime.wait_context()

def _save_wait_status(dest: Path, payload: dict) -> None:
    tmp = dest.with_name(dest.name + '.tmp.%s' % os.getpid())
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()

def cmd_defer(argv: list[str]) -> int:
    result = json.loads(sys.stdin.read().lstrip('\ufeff'))
    if result.get('action') != 'blocked' or any((result.get(k) for k in ('activeDenied', 'deniedFrom', 'denyUnsure', 'switchFailed'))):
        return 1
    try:
        retry = datetime.datetime.fromisoformat((result.get('soonestAt') or '').replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 1
    if not math.isfinite(retry) or retry <= time.time():
        return 1
    dest = Path(argv[0])
    payload = json.loads(dest.read_text(encoding='utf-8'))
    if payload.get('state') != 'blocked':
        return 1
    payload['retryAt'] = retry + 60
    payload['waitKind'] = 'quota'
    payload['waitContext'] = _wait_context()
    payload['lastUsageCheckAt'] = payload.get('ts')
    _save_wait_status(dest, payload)
    return cmd_wait(argv)

def monitor_interval(result: dict) -> float | None:
    if not result.get('watchOnly') or result.get('action') != 'stay' or any((result.get(k) for k in ('activeDenied', 'currentDenied', 'deniedFrom', 'denyUnsure', 'switchFailed'))):
        return None
    try:
        used = float(result['used'])
        rate = float(result.get('burnRate') or 0)
    except (KeyError, ValueError, TypeError):
        return None
    if not math.isfinite(used) or not 0 <= used <= 100 or (not math.isfinite(rate)):
        return None
    interval = 1800 if used < 50 else 600 if used < 80 else 120 if used < 90 else 60
    rate_limit = math.inf
    if rate > 0:
        gate = 80 if used < 80 else 90 if used < 90 else 100
        rate_limit = max(20, (gate - used) / rate * 60 / 4)
        interval = min(interval, rate_limit)
    if result.get('urgent') or result.get('wouldSwitchTo'):
        interval = min(interval, 60)
    if interval == 1800:
        interval = min(random.uniform(1620, 1980), rate_limit)
    return interval

def cmd_schedule(argv: list[str]) -> int:
    result = json.loads(sys.stdin.read().lstrip('\ufeff'))
    interval = monitor_interval(result)
    if interval is None:
        return 1
    dest = Path(argv[0])
    payload = json.loads(dest.read_text(encoding='utf-8'))
    if payload.get('state') not in ('ok', 'advise'):
        return 1
    payload['retryAt'] = time.time() + interval
    payload['waitKind'] = 'monitor'
    payload['waitContext'] = _wait_context()
    payload['lastUsageCheckAt'] = payload.get('ts')
    payload['waitExtra'] = payload.get('extra', '')
    _save_wait_status(dest, payload)
    return cmd_wait(argv)

def cmd_wait(argv: list[str]) -> int:
    dest = Path(argv[0])
    try:
        payload = json.loads(dest.read_text(encoding='utf-8'))
        retry = float(payload.get('retryAt', 0))
    except (OSError, ValueError, TypeError):
        return 1
    kind = payload.get('waitKind', 'quota')
    valid_states = ('blocked',) if kind == 'quota' else ('ok', 'advise') if kind == 'monitor' else ()
    if payload.get('state') not in valid_states or not math.isfinite(retry) or retry <= time.time():
        return 1
    context = _wait_context()
    if context is None or (payload.get('waitContext') is not None and payload['waitContext'] != context):
        return 1
    payload['waitContext'] = context
    when = datetime.datetime.fromtimestamp(retry).strftime('%m-%d %H:%M')
    payload['ts'] = time.time()
    if kind == 'quota':
        payload['message'] = '等待额度恢复'
        payload['extra'] = '已暂停联网检查，%s 再查；额度为上次查询结果' % when
    else:
        payload['extra'] = '%s · %s 再查（额度为上次查询结果）' % (payload.get('waitExtra', ''), when)
    payload['nextCheckS'] = max(20, min(300, retry - time.time()))
    _save_wait_status(dest, payload)
    return 0
_USAGE_NEEDS = ('collect', 'is_usable', 'is_blocked', 'counted_windows', 'counts_toward_limit')

def _load_usage():
    import importlib.util
    here = Path(__file__).resolve().parent
    why_not = []
    for src in (here / 'ccpick_usage.py', here.parent / 'ccpick_usage.py', CCPICK / 'ccpick_usage.py'):
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
        missing = [n for n in _USAGE_NEEDS if not hasattr(mod, n)]
        if sys.platform == 'darwin' and (not hasattr(mod, 'prepare_coordination_import')):
            missing.append('prepare_coordination_import')
        if missing:
            why_not.append('%s (太旧, 缺 %s)' % (src, ', '.join(missing)))
            continue
        if sys.platform == 'darwin':
            try:
                mod.prepare_coordination_import()
            except OSError as e:
                return (None, '%s (ccpick 安装不可用: %s)' % (src, type(e).__name__))
        return (mod, '')
    return (None, '; '.join(why_not))

def _clean(text: str) -> str:
    return (text or '').encode('utf-8', 'replace').decode('utf-8', 'replace')

def _inherit_from_old(dest: str, payload: dict, keys: tuple) -> None:
    try:
        with open(dest, 'r', encoding='utf-8') as fh:
            old = json.load(fh)
    except Exception:
        return
    if not isinstance(old, dict):
        return
    for k in keys:
        if payload.get(k) is None and old.get(k) is not None:
            payload[k] = old[k]

def cmd_write(argv: list[str]) -> int:
    dest, state, msg, extra, thr = (argv[0], argv[1], argv[2], argv[3], argv[4])
    windows = argv[5] if len(argv) > 5 else ''
    sched = argv[6] if len(argv) > 6 else ''
    tmp = '%s.tmp.%d' % (dest, os.getpid())
    payload = {'schema': 1, 'ts': time.time(), 'state': state, 'message': _clean(msg), 'extra': _clean(extra)}

    def _num(tok):
        try:
            return float(tok)
        except (TypeError, ValueError):
            return None
    parts = (windows or '').split('|')
    payload['usedPct'] = _num(parts[0]) if len(parts) > 0 else None
    payload['win5h'] = _num(parts[1]) if len(parts) > 1 else None
    payload['win7d'] = _num(parts[2]) if len(parts) > 2 else None
    payload['winModel'] = _num(parts[3]) if len(parts) > 3 else None
    payload['cooling'] = len(parts) > 4 and parts[4] == '1'
    payload['binding'] = (parts[5] if len(parts) > 5 else '') or None
    payload['modelName'] = (parts[6] if len(parts) > 6 else '') or None
    mc = parts[7] if len(parts) > 7 else ''
    payload['modelCounted'] = True if mc == '1' else False if mc == '0' else None
    sp = (sched or '').split('|')
    payload['nextCheckS'] = _num(sp[0]) if len(sp) > 0 else None
    payload['etaS'] = _num(sp[1]) if len(sp) > 1 else None
    payload['burnRate'] = _num(sp[2]) if len(sp) > 2 else None
    payload['activeEmail'] = (sp[3] if len(sp) > 3 else '') or None
    decision_action = sp[4] if len(sp) > 4 else None
    payload['decisionAction'] = decision_action if decision_action in ('stay', 'switched', 'would-switch', 'blocked', 'error') else None
    try:
        payload['threshold'] = float(thr)
    except (TypeError, ValueError):
        payload['threshold'] = None
    if not (windows or '').strip():
        _inherit_from_old(dest, payload, ('usedPct', 'win5h', 'win7d', 'winModel', 'binding', 'modelName', 'modelCounted'))
    if not (sched or '').strip():
        _inherit_from_old(dest, payload, ('activeEmail',))
    text = json.dumps(payload, ensure_ascii=False)
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return 0

def cmd_used(_argv: list[str]) -> int:
    poll = None
    reason = ''
    for line in sys.stdin:
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get('event') == 'poll':
            poll = obj
        elif obj.get('reason'):
            reason = str(obj['reason'])
    if not poll:
        print('')
        return 0
    slot = str((poll.get('active') or {}).get('number', ''))
    win = (poll.get('windowsPct') or {}).get(slot) or {}
    head = (poll.get('headroomPct') or {}).get(slot)

    def fmt(key):
        v = win.get(key)
        return '-' if v is None else '%d' % round(float(v))
    binding = '-' if head is None else '%d' % round(100.0 - float(head))
    print('%s|%s|%s|%s|%s' % (binding, fmt('5h'), fmt('7d'), fmt('Fable'), '1' if reason == 'cooldown' else '0'))
    return 0

def cmd_earliest(_argv: list[str]) -> int:
    import datetime
    mod, _why = _load_usage()
    if mod is None:
        return 1
    best = None
    for row in mod.collect():
        if row.get('autoSwitchEnabled') is False:
            continue
        windows = row.get('windows') or {}
        for key in ('5h', '7d'):
            win = windows.get(key) or {}
            pct = win.get('pct')
            if pct is None or 100.0 - float(pct) > 0:
                continue
            iso = win.get('resets_at')
            if not iso:
                continue
            try:
                when = datetime.datetime.fromisoformat(iso)
            except Exception:
                continue
            if best is None or when < best[0]:
                best = (when, row.get('email'), win.get('at'), win.get('in'))
    if best:
        print('%s 最早恢复：%s（还有 %s）' % (best[1], best[2] or '?', best[3] or '?'))
    return 0

def _active_email_from_status(max_age_s: float=600.0) -> str:
    path = _runtime.data_dir() / 'autoswitch' / 'status.json'
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return ''
    if time.time() - float(obj.get('ts') or 0) > max_age_s:
        return ''
    return str(obj.get('activeEmail') or '')

def cmd_accounts(_argv: list[str]) -> int:
    mod, why_not = _load_usage()
    if mod is None:
        print(json.dumps({'error': '找不到可用的 ccpick_usage.py: %s' % why_not}, ensure_ascii=False))
        return 1
    cached = _active_email_from_status()
    if cached:
        mod.live_identity = lambda *a, **k: cached
    cap_fn = getattr(mod, 'capacity', None)
    try:
        plans = mod.plan_info() if hasattr(mod, 'plan_info') else {}
    except Exception:
        plans = {}
    rows = []
    newest = None
    for row in mod.collect():
        windows = row.get('windows') or {}
        wins = []
        unavailable = []
        for key in ('5h', '7d'):
            w = windows.get(key) or {}
            if w.get('pct') is None:
                unavailable.append({'name': key, 'status': 'expired' if w.get('expired') else 'missing'})
                continue
            wins.append({'name': key, 'used': float(w['pct']), 'at': w.get('at') or '', 'in': w.get('in') or ''})
        for key, w in windows.items():
            if key in ('5h', '7d') or not isinstance(w, dict) or w.get('pct') is None:
                continue
            wins.append({'name': key, 'used': float(w['pct']), 'at': w.get('at') or '', 'in': w.get('in') or ''})
        for w in wins:
            w['counted'] = bool(mod.counts_toward_limit(w['name']))
        counted = mod.counted_windows({w['name']: w['used'] for w in wins})
        worst = max(counted.values(), default=None)
        fetched = row.get('fetched_at')
        if isinstance(fetched, (int, float)) and (newest is None or fetched > newest):
            newest = float(fetched)
        ok, why = mod.is_usable(row)
        blocked, blocked_why = mod.is_blocked(row)
        plan = plans.get(str(row.get('email') or '').lower()) or {}
        cap = cap_fn(counted, plan.get('scale')) if cap_fn and counted else None
        rows.append({'slot': row.get('slot'), 'email': row.get('email') or '?', 'active': bool(row.get('active')), 'autoSwitchEnabled': row.get('autoSwitchEnabled') is not False, 'headroom': None if worst is None else 100.0 - worst, 'cap': None if cap is None else round(cap, 1), '_err': row.get('error'), '_disabled': bool(row.get('disabled')), 'plan': plan.get('plan') or '?', 'windows': wins, 'unavailableWindows': unavailable, 'usable': bool(ok), 'why': why, 'blocked': bool(blocked), 'blockedWhy': blocked_why})
    kind = getattr(mod, 'fetch_error_kind', None)

    def _pts(r):
        return r['cap'] if r['cap'] is not None else r['headroom']

    def _sink(r):
        return r['blocked'] or r.get('_disabled') or (kind is not None and kind(r.get('_err')) == 'unknown')
    rows.sort(key=lambda r: (not r['autoSwitchEnabled'], _sink(r), _pts(r) is None, -(_pts(r) or 0.0)))
    for r in rows:
        r.pop('_err', None)
        r.pop('_disabled', None)
    print(json.dumps({'fetchedAt': newest, 'accounts': rows}, ensure_ascii=False))
    return 0

def main() -> int:
    sys.path.insert(0, str(CCPICK))
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    table = {'write': cmd_write, 'used': cmd_used, 'earliest': cmd_earliest, 'accounts': cmd_accounts, 'defer': cmd_defer, 'wait': cmd_wait, 'schedule': cmd_schedule}
    fn = table.get(sys.argv[1])
    if fn is None:
        print('未知子命令: %s' % sys.argv[1], file=sys.stderr)
        return 2
    try:
        return fn(sys.argv[2:])
    except Exception as e:
        print('%s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 1
if __name__ == '__main__':
    sys.exit(main())
