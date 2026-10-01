from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def prepare_coordination_import() -> None:
    _runtime.bootstrap()
CACHE = _runtime.backend_data_dir() / 'cache' / 'usage.json'
SEQ = _runtime.backend_data_dir() / 'sequence.json'
SWAP_LOG = _runtime.backend_data_dir() / 'claude-swap.log'

def _watch_only_flag() -> Path:
    return _runtime.data_dir() / 'autoswitch' / 'claude-autoswitch.watch-only'
WATCH_ONLY_FLAG = _watch_only_flag()

def watch_only() -> bool:
    if os.environ.get('CCSWITCH_WATCH_ONLY', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return True
    return WATCH_ONLY_FLAG.exists()
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
DENIED_ERROR = 'http-403'
_DENIED_LOG_RE = re.compile('^(\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d)[,.]\\d+ - \\w+ - Usage fetch failed for account (\\S+?)(?: after refresh)?: http-403\\b')
_SWITCH_LOG_RE = re.compile('^(\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d)[,.]\\d+ - \\w+ - Switched from account \\S+ to (\\S+)\\s*$')
_DENIED_LOG_TAIL = 512 * 1024
_DENIED_LOG_MEMO: dict = {}

def _swap_log_events(log_path: Path | None=None) -> dict:
    path = Path(log_path) if log_path else SWAP_LOG
    files = [path.with_name(path.name + '.1'), path]
    key = []
    for p in files:
        try:
            st = p.stat()
            key.append((str(p), st.st_size, st.st_mtime_ns))
        except OSError:
            key.append((str(p), None, None))
    key = tuple(key)
    if key in _DENIED_LOG_MEMO:
        return _DENIED_LOG_MEMO[key]
    ev: dict = {'403': {}, 'in': {}}
    for p in files:
        try:
            with open(p, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - _DENIED_LOG_TAIL))
                text = f.read().decode('utf-8', 'replace')
        except OSError:
            continue
        for line in text.splitlines():
            m, kind = (_DENIED_LOG_RE.match(line), '403')
            if not m:
                m, kind = (_SWITCH_LOG_RE.match(line), 'in')
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
            except (ValueError, OverflowError):
                continue
            ev[kind].setdefault(m.group(2), []).append(ts)
    for d in ev.values():
        for arr in d.values():
            arr.sort()
    _DENIED_LOG_MEMO.clear()
    _DENIED_LOG_MEMO[key] = ev
    return ev

def _denied_log_hits(log_path: Path | None=None) -> dict:
    return _swap_log_events(log_path)['403']

def recent_403_times(log_path: Path | None=None) -> dict:
    return _denied_log_hits(log_path)

def switch_in_times(log_path: Path | None=None) -> dict:
    return {s: arr[-1] for s, arr in _swap_log_events(log_path)['in'].items() if arr}

def deny_detail(accounts: dict | None, log_path: Path | None=None) -> dict:
    out: dict = {}
    hits = None
    for slot, a in (accounts or {}).items():
        if not isinstance(a, dict):
            continue
        try:
            good = float(a.get('fetchedAt') or 0)
        except (TypeError, ValueError):
            good = 0.0
        err = a.get('lastError')
        if not good or not err:
            continue
        if hits is None:
            hits = _denied_log_hits(log_path)
        after = [ts for ts in hits.get(str(slot), ()) if ts > good]
        try:
            attempt = float(a.get('lastAttemptAt') or good)
        except (TypeError, ValueError):
            attempt = good
        if after:
            last = max(after[-1], attempt) if err == DENIED_ERROR else after[-1]
            out[str(slot)] = {'first': after[0], 'last': last}
        elif err == DENIED_ERROR:
            out[str(slot)] = {'first': attempt, 'last': attempt}
    return out

def denied_slots(accounts: dict | None, log_path: Path | None=None) -> dict:
    return {s: d['first'] for s, d in deny_detail(accounts, log_path).items()}
DENIED_NOTE = '用量接口拒绝访问 (http-403)，疑似被 Anthropic 停用'

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
    from ccpick_app.account_context import manager
    unified = manager() if path is None else None
    if unified is not None:
        return {p.get('legacySlot', p['name']): (p.get('account') or {}).get('email', p.get('email', '')) for p in unified.profiles()}
    'sequence.json 里真实托管的账号 {slot: email}。读不到返回 None（= 别过滤）。\n\n    ★为什么需要这个★ (2026-09-20 实测)\n    `cswap remove` 会把账号从 sequence.json 删掉，但【不清 usage.json 缓存】——\n    删掉 Account-3 之后缓存里那条还在，于是托盘继续列一个已经不存在的账号。\n    用户删了却没消失，比不删更让人困惑。\n\n    sequence.json 是权威，usage.json 只是缓存：缓存里多出来的一律不认。\n    但读不到 sequence.json 时返回 None 而不是空 —— 空会把所有账号都过滤掉，\n    面板整片空白；那种情况下宁可多显示也别全不显示（同 refresh() 的取舍）。\n    文件在、accounts 是 {} 时照实返回 {}：那是"全新安装、一个号都没托管"，\n    同 managed_account_count() 的口径 (example 7af68f583 把它也当读不到，这里不跟)。\n\n    ★连 email 一起给★ (2026-09-15 example 7af68f583，2026-09-24 移植)：\n    `cswap move/swap` 会重排 slot，只看 slot 在不在的话，缓存里旧 slot 那条会顶着\n    别人的位置混进来。path 给 decide.py 用 —— 它有自己的 SEQ，测试会换掉。\n    '
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
UNIT_SCALE = 5.0
DEFAULT_SCALE = 5.0
TEAM_SCALE = 5.0
WEEK_TO_5H = 4.0
CONFIGS = _runtime.backend_data_dir() / 'configs'
_PLAN_MEMO: dict = {}

def _plan_from_oauth(oa: dict) -> tuple[float | None, str]:
    if not oa.get('profileFetchedAt'):
        return (None, '')
    if (oa.get('organizationType') or '') == 'claude_team' or oa.get('seatTier'):
        return (TEAM_SCALE, 'Team')
    tiers = ' '.join((str(oa.get(k) or '') for k in ('userRateLimitTier', 'organizationRateLimitTier'))).lower()
    if 'max_20x' in tiers:
        return (20.0, '20x')
    if 'max_5x' in tiers:
        return (5.0, '5x')
    if (oa.get('organizationType') or '') == 'claude_pro' or 'pro' in tiers:
        return (1.0, 'Pro')
    return (None, '')

def plan_info(seq_path: Path | None=None, configs_dir: Path | None=None) -> dict:
    from ccpick_app.account_context import manager
    unified = manager() if seq_path is None and configs_dir is None else None
    if unified is not None:
        return {(p.get('account') or {}).get('email', '').lower(): p.get('plan', {}) for p in unified.profiles()}
    '{email 小写: {"scale": 倍数, "plan": "20x"/"5x"/"Team"/"Pro"/"?", "org": 组织 uuid}}\n\n    认法, 先中先得:\n      1. ★同一个组织里有两个以上托管的号 ⇒ Team★ (sequence.json 的 organizationUuid,\n         cswap 入库时写的)。个人 Max / Pro 的组织只有自己一个人, 不会撞上。\n         ★排在快照前面是有原因的★: 本机 example@ 的快照里 oauthAccount 除了邮箱, 套餐\n         字段整段是 example@ 的 (accountCreatedAt 都一样) —— 照快照它是个人 20x,\n         可 sequence.json 里它跟另外四个 Team 号在同一个组织。\n      2. cswap 存的快照 configs/.claude-config-<slot>-<email>.json 里的 oauthAccount:\n         Team 席位 / *_max_20x / *_max_5x / Pro\n      3. 都认不出 ⇒ DEFAULT_SCALE, plan="?"\n    读不到任何东西时返回 {} —— 调用方一律退回 DEFAULT_SCALE。\n    按文件 mtime 记忆: decide 一轮要摊平好几次缓存, 快照每份 ~100KB, 不值得每次重读。\n    '
    seq_path = seq_path or SEQ
    configs_dir = configs_dir or CONFIGS
    try:
        seq = json.loads(seq_path.read_text(encoding='utf-8')).get('accounts') or {}
        seq_mt = seq_path.stat().st_mtime
    except Exception:
        return {}
    if not isinstance(seq, dict):
        return {}
    files = {}
    try:
        for p in configs_dir.glob('.claude-config-*.json'):
            files[p.name] = p
        cfg_mt = tuple(sorted(((n, p.stat().st_mtime) for n, p in files.items())))
    except Exception:
        cfg_mt = ()
    key = (str(seq_path), seq_mt, str(configs_dir), cfg_mt)
    if key in _PLAN_MEMO:
        return _PLAN_MEMO[key]
    snaps, orgs = ({}, {})
    for slot, v in seq.items():
        if not isinstance(v, dict):
            continue
        email = str(v.get('email') or '').strip()
        if not email:
            continue
        oa = {}
        p = files.get('.claude-config-%s-%s.json' % (slot, email))
        if p is not None:
            try:
                oa = json.loads(p.read_text(encoding='utf-8')).get('oauthAccount') or {}
            except Exception:
                oa = {}
            if str(oa.get('emailAddress') or '').strip().lower() not in ('', email.lower()):
                oa = {}
        snaps[slot] = (email, oa)
        orgs[slot] = str(v.get('organizationUuid') or oa.get('organizationUuid') or '')
    org_members: dict = {}
    for org in orgs.values():
        if org:
            org_members[org] = org_members.get(org, 0) + 1
    out = {}
    for slot, (email, oa) in snaps.items():
        org = orgs[slot]
        if org and org_members.get(org, 0) >= 2:
            scale, plan = (TEAM_SCALE, 'Team')
        else:
            scale, plan = _plan_from_oauth(oa)
        out[email.lower()] = {'scale': scale if scale else DEFAULT_SCALE, 'plan': plan or '?', 'org': org}
    _PLAN_MEMO.clear()
    _PLAN_MEMO[key] = out
    return out

def capacity(counted: dict, scale: float | None=None) -> float | None:
    rooms = []
    has_5h = False
    for name, pct in (counted or {}).items():
        if pct is None:
            continue
        room = max(0.0, 100.0 - float(pct))
        if name == '5h':
            has_5h = True
            rooms.append(room)
        else:
            rooms.append(room * WEEK_TO_5H)
    if not rooms:
        return None
    if not has_5h:
        rooms.append(100.0)
    return (scale or DEFAULT_SCALE) / UNIT_SCALE * min(rooms)

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
    from ccpick_app.account_context import manager
    unified = manager()
    if unified is not None:
        return unified.current()['account']['email']
    '当前真正生效的账号邮箱 —— 问 claude 自己，不猜。\n\n    ★timeout 默认 5 秒, 不是 60★ (2026-09-05)\n    这里 fork 的是 `claude auth status`, 一个 Node 进程。菜单栏面板给【整个】取数\n    只有 6 秒硬预算 (loadAccounts), 所以 60 秒的上限等于没有上限 —— 机器一忙,\n    面板就整片空白显示"读不到账号余量"(当天实测 7 次)。问不到身份只是少标一个\n    "当前", 比整张表都不显示好得多。\n\n    ★不要用 sequence.json 的 activeAccountNumber★：那个字段会陈旧\n    （实测它指向 slot 5，而真实身份是 slot 4 的账号）。标错"当前账号"\n    会让人按错误前提做决定，比不显示更糟。\n    '
    import subprocess
    exe = _claude_exe()
    if not exe:
        return None
    try:
        prepare_coordination_import()
        from ccpick_coordination import child_environment
        env = child_environment() or dict(os.environ)
        env['BROWSER'] = 'true'
        out = subprocess.run([exe, 'auth', 'status'], capture_output=True, text=True, timeout=timeout, env=env, encoding='utf-8', errors='replace').stdout
        return json.loads(out.lstrip('\ufeff').strip()).get('email')
    except Exception:
        return None

def collect(now=None, identity: str | None=None) -> list[dict]:
    from ccpick_app.account_context import manager
    unified = manager()
    if unified is not None:
        return unified.collect()
    '把缓存整理成一行一个账号的结构。\n\n    identity：调用方已经确知的当前邮箱（比如 autoswitch 刚验完落地身份）。给了就不再\n    fork 一次 `claude auth status` 去问 —— 那是个 Node 进程，机器忙时要好几秒。\n    '
    now = now or datetime.now(timezone.utc)
    data = load_usage()
    me = identity if identity is not None else live_identity()
    rows = []

    def mk(pct, resets_at):
        rst = _parse_iso(resets_at)
        expired = bool(rst and rst <= now)
        return {'pct': None if expired else pct, 'stale_pct': pct if expired else None, 'expired': expired, 'resets_at': resets_at, 'in': '—' if expired or not rst else _fmt_delta(rst, now), 'at': '—' if expired or not rst else _fmt_local(rst)}
    roster = managed_roster()
    try:
        account_policy = json.loads(SEQ.read_text(encoding='utf-8')).get('accounts') or {}
        if not isinstance(account_policy, dict):
            account_policy = {}
    except (OSError, ValueError, AttributeError):
        account_policy = {}
    denied = denied_slots(data.get('accounts'))
    for slot, a in sorted((data.get('accounts') or {}).items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
        if not in_roster(slot, a.get('email'), roster):
            continue
        good = a.get('lastGood') or {}
        email = a.get('email', '')
        policy = account_policy.get(str(slot)) or {}
        if not isinstance(policy, dict):
            policy = {}
        row = {'slot': slot, 'email': email, 'active': bool(me) and email == me, 'autoSwitchEnabled': _runtime.account_enabled(slot, email, SEQ), 'error': a.get('lastError'), 'fetched_at': a.get('fetchedAt'), 'denied': denied.get(str(slot)), 'windows': {}, 'disabled': not _runtime.account_enabled(slot, email, SEQ)}
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
    if row.get('denied'):
        return (True, DENIED_NOTE + '；确认后 cswap remove')
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
        if r.get('autoSwitchEnabled') is False:
            mark += ' [仅手动]'
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
        if r.get('denied'):
            out.append('     ★%s★ —— 确认后 cswap remove' % DENIED_NOTE)
        elif r['error']:
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
