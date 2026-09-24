from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import sys
import time
from pathlib import Path
CCPICK = _runtime.legacy_dir()
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
        if missing:
            why_not.append('%s (太旧, 缺 %s)' % (src, ', '.join(missing)))
            continue
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
    rows = []
    newest = None
    for row in mod.collect():
        windows = row.get('windows') or {}
        wins = []
        for key in ('5h', '7d'):
            w = windows.get(key) or {}
            if w.get('pct') is None:
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
        rows.append({'slot': row.get('slot'), 'email': row.get('email') or '?', 'active': bool(row.get('active')), 'headroom': None if worst is None else 100.0 - worst, 'windows': wins, 'usable': bool(ok), 'why': why, 'blocked': bool(blocked), 'blockedWhy': blocked_why})
    rows.sort(key=lambda r: (not r['usable'], r['headroom'] is None, -(r['headroom'] or 0.0)))
    print(json.dumps({'fetchedAt': newest, 'accounts': rows}, ensure_ascii=False))
    return 0

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    table = {'write': cmd_write, 'used': cmd_used, 'earliest': cmd_earliest, 'accounts': cmd_accounts}
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
