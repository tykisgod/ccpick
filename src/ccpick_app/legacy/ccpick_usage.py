from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
CACHE = _runtime.backend_data_dir() / 'cache' / 'usage.json'
SEQ = _runtime.backend_data_dir() / 'sequence.json'
HISTORY = _runtime.data_dir() / 'usage-history.jsonl'
STATUS = _runtime.data_dir() / 'account-status.json'

def known_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding='utf-8')).get('accounts', {})
    except Exception:
        return {}
BUSY_PCT = 90.0
WEEK_BUSY_PCT = 95.0
MODELS_ENV = 'CCSWITCH_MODELS'
ACCOUNT_WINDOWS = ('5h', '7d')

def counted_models(models: str | None=None) -> tuple[bool, frozenset]:
    raw = os.environ.get(MODELS_ENV, '') if models is None else models
    names = frozenset((p.strip().lower() for p in str(raw or '').split(',') if p.strip()))
    if 'all' in names:
        return (True, frozenset())
    return (False, names - {'none'})

def counts_toward_limit(name: str, models: str | None=None) -> bool:
    if name in ACCOUNT_WINDOWS:
        return True
    count_all, names = counted_models(models)
    return count_all or str(name).lower() in names

def counted_windows(windows: dict, models: str | None=None) -> dict:
    return {k: v for k, v in (windows or {}).items() if counts_toward_limit(k, models)}

def models_note(models: str | None=None) -> str:
    count_all, names = counted_models(models)
    if count_all:
        return '每模型周窗口全部计入切换判据 (%s=all)' % MODELS_ENV
    if names:
        return '每模型周窗口只计入 %s (%s)' % (', '.join(sorted(names)), MODELS_ENV)
    return '每模型周窗口 (Fable 等) 只显示、不计入切换判据 (%s 未设置)' % MODELS_ENV

def cswap_model_args(models: str | None=None) -> list:
    count_all, names = counted_models(models)
    return ['--model', 'all' if count_all else ','.join(sorted(names))]
_DEAD_ERRORS = frozenset(('invalid_grant', 'no_refresh_token', 'no-access-token'))
_TRANSIENT_ERRORS = frozenset(('timeout', 'network', 'bad-response', 'transient', 'invalid_client', 'http-408', 'refresh-failed', 'consume-busy'))
_TRANSIENT_WORDS = ('timeout', 'timed out', 'connection', 'disconnect', 'network', 'temporarily', 'ssl', 'incompleteread', 'reset')

def fetch_error_kind(err) -> str:
    if not err:
        return ''
    text = str(err).strip().lower()
    if text in _DEAD_ERRORS:
        return 'dead'
    if text in _TRANSIENT_ERRORS or re.search('\\b(?:429|5\\d\\d)\\b', text):
        return 'transient'
    if any((w in text for w in _TRANSIENT_WORDS)):
        return 'transient'
    return 'unknown'

def _window_label(key: str) -> str:
    return {'5h': '5h ', '7d': '周额度'}.get(key, key + ' 周额度')

def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None

def _fmt_delta(target, now=None):
    if target is None:
        return '—'
    now = now or datetime.now(timezone.utc)
    secs = (target - now).total_seconds()
    if secs <= 0:
        return '已恢复'
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return '%dd %dh' % (d, h)
    if h:
        return '%dh %dm' % (h, m)
    return '%dm' % m

def _fmt_local(target):
    if target is None:
        return '—'
    lt = target.astimezone()
    now = datetime.now().astimezone()
    if lt.date() == now.date():
        return lt.strftime('今天 %H:%M')
    if (lt.date() - now.date()).days == 1:
        return lt.strftime('明天 %H:%M')
    return lt.strftime('%m-%d %H:%M')

def load_usage() -> dict:
    if not CACHE.is_file():
        return {}
    try:
        data = json.loads(CACHE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def managed_account_count() -> int | None:
    if not SEQ.exists():
        return 0
    try:
        accounts = json.loads(SEQ.read_text(encoding='utf-8')).get('accounts') or {}
        return len(accounts) if isinstance(accounts, dict) else None
    except Exception:
        return None

def managed_roster(path: Path | None=None) -> dict | None:
    try:
        accounts = json.loads((path or SEQ).read_text(encoding='utf-8')).get('accounts')
    except Exception:
        return None
    if not isinstance(accounts, dict):
        return None
    return {str(k): str((v or {}).get('email') or '') if isinstance(v, dict) else '' for k, v in accounts.items()}

def in_roster(slot, email, roster: dict | None) -> bool:
    if roster is None:
        return True
    s = str(slot)
    if s not in roster:
        return False
    want = (roster.get(s) or '').strip().lower()
    have = str(email or '').strip().lower()
    return not want or not have or want == have

def _claude_exe() -> str | None:
    import shutil
    appdata = os.environ.get('APPDATA', '')
    cands = []
    if appdata:
        cands.append(Path(appdata) / 'npm' / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'bin' / 'claude.exe')
        cands.append(Path(appdata) / 'npm' / 'claude.cmd')
    cands += [Path.home() / '.local' / 'bin' / 'claude.exe', Path.home() / '.local' / 'bin' / 'claude', Path('/usr/local/bin/claude')]
    exe = next((str(c) for c in cands if c.is_file()), None)
    if exe:
        return exe
    w = shutil.which('claude')
    return w if w and Path(w).is_absolute() else None

def live_identity(timeout: float=5.0) -> str | None:
    import subprocess
    exe = _claude_exe()
    if not exe:
        return None
    try:
        env = dict(os.environ)
        env['BROWSER'] = 'true'
        out = subprocess.run([exe, 'auth', 'status'], capture_output=True, text=True, timeout=timeout, env=env).stdout
        return json.loads(out.lstrip('\ufeff').strip()).get('email')
    except Exception:
        return None

def collect(now=None, identity: str | None=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    data = load_usage()
    me = identity if identity is not None else live_identity()
    rows = []

    def mk(pct, resets_at):
        rst = _parse_iso(resets_at)
        expired = bool(rst and rst <= now)
        return {'pct': None if expired else pct, 'stale_pct': pct if expired else None, 'expired': expired, 'resets_at': resets_at, 'in': '—' if expired or not rst else _fmt_delta(rst, now), 'at': '—' if expired or not rst else _fmt_local(rst)}
    roster = managed_roster()
    for slot, a in sorted((data.get('accounts') or {}).items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
        if not in_roster(slot, a.get('email'), roster):
            continue
        good = a.get('lastGood') or {}
        email = a.get('email', '')
        row = {'slot': slot, 'email': email, 'active': bool(me) and email == me, 'error': a.get('lastError'), 'fetched_at': a.get('fetchedAt'), 'windows': {}, 'disabled': not _runtime.account_enabled(slot, email, SEQ)}
        for key, label in (('five_hour', '5h'), ('seven_day', '7d')):
            w = good.get(key) or {}
            if w:
                row['windows'][label] = mk(w.get('pct'), w.get('resets_at'))
        for s in good.get('scoped') or []:
            row['windows'][s.get('name', '?')] = mk(s.get('pct'), s.get('resets_at'))
        rows.append(row)
    return rows

def is_blocked(row: dict) -> tuple[bool, str]:
    ks = known_status().get(row.get('email', ''), {})
    if ks.get('status') == 'account_on_hold':
        return (True, '账号被暂停 (account_on_hold, %s 实测)，重新入库无效' % ks.get('observed', ''))
    err = row.get('error')
    if fetch_error_kind(err) == 'dead':
        if err == 'invalid_grant':
            return (True, '凭据已失效，需要重新入库')
        return (True, '凭据已失效 (%s)，需要重新入库' % err)
    return (False, '')

def is_usable(row: dict) -> tuple[bool, str]:
    if row.get('disabled'):
        return (False, 'Account disabled for automatic rotation')
    '这个账号现在值不值得切过去用。返回 (能否, 原因)。\n\n    面板排序 / `ccpick usage` 的"现在可切换的账号" / 入库后的额度判定用它，口径是保守的；\n    人手动点能不能点用 is_blocked()，见那边的注释。\n    ★它不是 autoswitch 的判据★ —— autoswitch 挑目标在 claude-autoswitch-decide.py 的\n    pick()，池子紧时会降级去挑 90~95% 的号。两者的关系由 selftest_pace 的\n    check_usable_implies_autoswitch_target 钉住：这里说能用的，autoswitch 一定肯去。\n    '
    blocked, why = is_blocked(row)
    if blocked:
        return (False, why)
    err = row.get('error')
    kind = fetch_error_kind(err)
    if kind == 'unknown':
        return (False, '取数出错 (%s)，状态不明' % err)
    stale = '（上次取数 %s，用的是缓存里的数字）' % err if kind == 'transient' else ''
    wins = row['windows']
    fh = wins.get('5h') or {}
    if fh.get('expired'):
        return (False, '数据过期（窗口已重置），跑 cswap list 刷新后再判断')
    pct = fh.get('pct')
    if pct is None:
        return (False, '没有 5h 窗口数据')
    weekly = [k for k in counted_windows(wins) if k != '5h']
    for k in ['5h'] + weekly:
        w = wins.get(k) or {}
        p = w.get('pct')
        if p is not None and p >= 100:
            return (False, '%s已用尽，%s 恢复（还有 %s）' % (_window_label(k), w.get('at'), w.get('in')))
    if pct >= BUSY_PCT:
        return (False, '5h 已用 %.0f%%，快满了' % pct)
    for k in weekly:
        p = (wins.get(k) or {}).get('pct')
        if p is not None and p >= WEEK_BUSY_PCT:
            return (False, '%s已用 %.0f%%，快满了' % (_window_label(k), p))
    spct = (wins.get('7d') or {}).get('pct')
    hot = ['%s %.0f%%' % (k, (w or {}).get('pct')) for k, w in wins.items() if not counts_toward_limit(k) and isinstance(w, dict) and (w.get('pct') is not None) and (w['pct'] >= WEEK_BUSY_PCT)]
    return (True, '5h %.0f%%%s%s%s' % (pct, '，周 %.0f%%' % spct if spct is not None else '', '（%s 不计入）' % '、'.join(hot) if hot else '', stale))

def render(rows: list[dict]) -> str:
    now = datetime.now()
    out = []
    out.append('账号额度 —— 什么时候恢复      (读本地缓存，不发网络请求)')
    out.append('=' * 78)
    if not rows:
        managed = managed_account_count()
        if managed == 0:
            out.append('  claude-swap 尚无托管账号；没有对象可采集，不代表账号额度正常。')
            out.append('  先用 ccpick enroll --add 入库一个账号。')
        elif managed is None:
            out.append('  sequence.json 无法解析，无法判断为何没有额度数据。')
        else:
            out.append('  有 %d 个托管账号，但暂无缓存；跑 cswap list --json 刷新。' % managed)
        return '\n'.join(out)
    for r in rows:
        mark = ' ←当前' if r['active'] else ''
        head = '%s. %s%s' % (r['slot'], r['email'], mark)
        out.append('')
        out.append(head)
        ks = known_status().get(r['email'], {})
        if ks.get('status') == 'account_on_hold':
            out.append('     ★账号被 Anthropic 暂停 (account_on_hold) —— 重新入库无效★')
            out.append('     %s' % (ks.get('note') or ''))
            continue
        if ks.get('status') == 'unknown' and ks.get('note'):
            out.append('     状态未确认：%s' % ks['note'])
        if r['error']:
            note = {'invalid_grant': '凭据已失效 → 需要重新入库 (ccpick enroll)', 'http-403': 'setup-token 拿不到用量（正常，非故障）'}.get(r['error'], '采集失败: %s' % r['error'])
            out.append('     %s' % note)
        if not r['windows']:
            if not r['error']:
                out.append('     （无数据）')
            continue
        order = [k for k in ('5h', '7d') if k in r['windows']]
        order += [k for k in r['windows'] if k not in ('5h', '7d')]
        for k in order:
            w = r['windows'][k]
            if w.get('expired'):
                out.append('     %-6s (窗口已重置，缓存数据过期)' % k)
                continue
            pct = w.get('pct')
            bar_n = int(round((pct or 0) / 10))
            bar = '█' * bar_n + '·' * (10 - bar_n)
            flag = ''
            busy = BUSY_PCT if k == '5h' else WEEK_BUSY_PCT
            if pct is not None and pct >= 100:
                flag = '  用尽'
            elif pct is not None and pct >= busy:
                flag = '  快满'
            if flag and (not counts_toward_limit(k)):
                flag += ' · 不计入切换判据'
            out.append('     %-6s %s %3.0f%%   恢复 %-11s 还有 %-8s%s' % (k, bar, pct if pct is not None else 0, w['at'], w['in'], flag))
        ft = r.get('fetched_at')
        if ft:
            age = (datetime.now().timestamp() - ft) / 60
            fresh = '%.0f 分钟前采集' % age if age >= 1 else '刚采集'
            if age > 30:
                fresh += '（较旧，跑 cswap list 可刷新）'
            out.append('     %s' % fresh)
    out.append('')
    out.append('-' * 78)
    usable = [(r, is_usable(r)) for r in rows]
    good = [r['email'] for r, (ok, _) in usable if ok]
    out.append('现在可切换的账号: %s' % (', '.join(good) if good else '无'))
    for r, (ok, why) in usable:
        if not ok:
            out.append('   %-28s %s' % (r['email'], why))
    out.append(models_note())
    out.append('生成于 %s' % now.strftime('%Y-%m-%d %H:%M:%S'))
    return '\n'.join(out)

def snapshot(rows: list[dict], reason: str='') -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    rec = {'ts': datetime.now(timezone.utc).isoformat(), 'reason': reason, 'accounts': [{'slot': r['slot'], 'email': r['email'], 'active': r['active'], 'error': r['error'], 'fetched_at': r.get('fetched_at'), 'windows': {k: {'pct': v.get('pct'), 'resets_at': v.get('resets_at')} for k, v in r['windows'].items()}} for r in rows]}
    with HISTORY.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')

def show_history(limit: int=20) -> None:
    if not HISTORY.is_file():
        print('还没有历史记录。跑 ccpick usage --snapshot 记第一条。')
        return
    lines = HISTORY.read_text(encoding='utf-8').strip().splitlines()
    print('共 %d 条记录，显示最近 %d 条：' % (len(lines), min(limit, len(lines))))
    print()
    for line in lines[-limit:]:
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = _parse_iso(r['ts'])
        print('%s  %s' % (ts.astimezone().strftime('%m-%d %H:%M') if ts else '?', r.get('reason') or ''))
        for a in r['accounts']:
            w = a['windows']
            bits = []
            for k in ('5h', '7d'):
                if k in w and w[k].get('pct') is not None:
                    bits.append('%s=%.0f%%' % (k, w[k]['pct']))
            for k, v in w.items():
                if k not in ('5h', '7d') and v.get('pct') is not None:
                    bits.append('%s=%.0f%%' % (k, v['pct']))
            mark = '*' if a['active'] else ' '
            print('   %s %-28s %s' % (mark, a['email'], '  '.join(bits) or a.get('error') or '-'))
        print()

def cmd_usage(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick usage')
    ap.add_argument('--json', action='store_true', help='机器可读输出')
    ap.add_argument('--snapshot', action='store_true', help='顺手记一条历史')
    ap.add_argument('--reason', default='', help='记历史时标注原因')
    ap.add_argument('--history', action='store_true', help='看历史记录')
    ap.add_argument('--limit', type=int, default=20)
    ns = ap.parse_args(args)
    if ns.history:
        show_history(ns.limit)
        return 0
    rows = collect()
    if ns.snapshot:
        snapshot(rows, ns.reason or 'manual')
    if ns.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render(rows))
    return 0
