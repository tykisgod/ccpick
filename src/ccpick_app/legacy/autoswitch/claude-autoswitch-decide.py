from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import datetime
import json
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
        missing = [n for n in ('fetch_error_kind', 'snapshot', 'collect', 'counted_windows', 'counts_toward_limit', 'managed_roster', 'in_roster', 'models_note') if not hasattr(mod, n)]
        if not missing and 'identity' not in inspect.signature(mod.collect).parameters:
            missing.append('collect(identity=)')
        if missing:
            why_not.append('%s (太旧, 缺 %s)' % (src, ', '.join(missing)))
            continue
        return mod
    raise SystemExit('claude-autoswitch-decide: 没有可用的 ccpick_usage.py —— ' + '; '.join(why_not))
usage = _load_usage()
CACHE = _runtime.backend_data_dir() / 'cache' / 'usage.json'
SEQ = _runtime.backend_data_dir() / 'sequence.json'
CSWAP = _backend.executable()

def _state_dir() -> Path:
    return _runtime.data_dir() / 'autoswitch'
LEDGER = _state_dir() / 'claude-autoswitch-ledger.json'
FRESH_S = 90
CONSIDER_AT = float(os.environ.get('CCSWITCH_THRESHOLD', '90'))
EXHAUSTED_AT = 97.0
USABLE_CEILING = 95.0
MIN_GAIN = 10.0
BURNED_AT = 98.0
MAX_SWITCH_TRIES = 3
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
    try:
        return _backend.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None

def rows_from_cache() -> list[dict]:
    raw = _read(CACHE)
    roster = usage.managed_roster(SEQ)
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
        out.append({'slot': slot, 'email': a.get('email') or '?', 'used': used, 'counted': counted, 'worstUsed': counted[binding] if binding else None, 'binding': binding, 'resetsAt': resets.get(binding, ''), 'resets': resets, 'age': now - float(data_ts), 'error': a.get('lastError'), 'disabled': not _runtime.account_enabled(slot, a.get('email'), SEQ)})
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

def refresh(slots_needed: list[str], budget: int) -> bool:
    _cswap(['auto', '--once', '--dry-run'], timeout=120)
    ages = {r['slot']: r['age'] for r in rows_from_cache()}
    return all((ages.get(s, 1000000000.0) <= FRESH_S for s in slots_needed))

def _fatal_error(err) -> bool:
    return usage.fetch_error_kind(err) in ('dead', 'unknown')

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
        if _fatal_error(r.get('error')):
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

def vacate(email: str, used: float, binding: str, resets_at: str) -> None:
    if not email:
        return
    if used >= BURNED_AT:
        rec = {'at': time.time(), 'used': used, 'binding': binding, 'resetsAt': resets_at or '', 'why': '已满'}
    else:
        rec = {'at': time.time(), 'used': used, 'binding': binding, 'resetsAt': '', 'why': '超阈值, 只压短时'}
    led = load_ledger()
    led[email] = rec
    _write_atomic(LEDGER, led)

def is_vacated(email: str) -> bool:
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
    arr = [x for x in s.get('samples') or [] if isinstance(x, list) and len(x) == 2 and isinstance(x[1], dict) and (now - float(x[0]) <= RATE_WINDOW_S)][-RATE_SAMPLES:]
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

def pace_of(email: str, used_now: dict, now: float) -> dict:
    used_now = usage.counted_windows(used_now)
    rate, eta = burn_eta(email, used_now, now)
    worst = max((float(v) for v in (used_now or {}).values()), default=None)
    nxt = next_check_s(eta_to_gate(used_now, rate), worst)
    react_s = (nxt if nxt is not None else NEXT_CHECK_MAX_S) + SWITCH_COST_S + REACT_MARGIN_S
    return {'burnRate': round(rate, 1) if rate else None, 'etaS': round(eta) if eta is not None else None, 'nextCheckS': round(nxt) if nxt is not None else None, 'consider': CONSIDER_AT, 'urgent': eta is not None and eta <= react_s}

def _record_history(chain: str, final: str) -> None:
    try:
        usage.snapshot(usage.collect(identity=final), 'autoswitch: %s' % chain)
    except Exception:
        pass

def pick(rows: list[dict], exclude: set[str]) -> tuple[dict | None, str]:
    base = [r for r in rows if r['email'] not in exclude and r['worstUsed'] is not None and (not _fatal_error(r['error'])) and (not is_vacated(r['email'])) if not r.get('disabled', False)]
    ok = [r for r in base if r['worstUsed'] < USABLE_CEILING if not r.get('disabled', False)]
    if not ok:
        return (None, '')
    best = min(ok, key=lambda r: (r['worstUsed'], r['age']))
    tier = '宽裕' if best['worstUsed'] < CONSIDER_AT else '★都不宽裕, 挑矮子里的高个'
    return (best, tier)

def decide(dry_run: bool) -> dict:
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
    me = {r['email']: r for r in rows_from_cache()}[me_email]
    if me['worstUsed'] is None:
        return {'action': 'error', 'why': '拿不到活跃账号的用量'}
    now = time.time()
    record_sample(me_email, me['used'], now - float(me['age']))
    pace = pace_of(me_email, me['used'], now)
    urgent = pace['urgent']
    if me['worstUsed'] < CONSIDER_AT and (not urgent):
        return {'action': 'stay', 'active': me['email'], 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], 'why': '还有额度, 继续用', 'dataAge': round(me['age']), 'fresh': fresh_ok, **pace}
    refresh([r['slot'] for r in rows_from_cache()], budget=2)
    tried: set[str] = {me['email']}
    cur = me
    hops: list[str] = []

    def burned(r: dict) -> bool:
        return r.get('worstUsed') is not None and r['worstUsed'] >= BURNED_AT

    def home_option() -> dict | None:
        if me.get('disabled'):
            return None
        '已经被动挪了窝之后, 原号还能不能退回去。\n\n        ★不受 USABLE_CEILING 管★ (2026-09-24 第二轮审查) —— 那条线是给【新】目标的\n        ("它也快死了, 换过去只是把撞墙推迟几十秒"); 退回原号比的是"比现在待的地方好不好"。\n        原来原号 96% 时回不去, 人被留在 99% 的号上。只拦死号和烧干的。\n        '
        if _fatal_error(me.get('error')) or me['worstUsed'] is None or burned(me):
            return None
        if cur.get('worstUsed') is not None and me['worstUsed'] >= cur['worstUsed']:
            return None
        return me

    def soonest_extra() -> dict:
        soonest = soonest_recovery(rows_from_cache(), time.time())
        return {'soonest': soonest['email'] if soonest else '', 'soonestAt': soonest['recoversAt'] if soonest else ''}

    def conclude(action: str, extra: dict) -> dict:
        chain = ' → '.join([me['email']] + hops)
        if cur['email'] == me['email']:
            if hops:
                _record_history(chain, me['email'])
            return {'action': action, 'active': me['email'], 'used': me['worstUsed'], 'binding': me['binding'], 'windows': me['used'], **extra, **pace}
        if not burned(cur):
            vacate(me['email'], me['worstUsed'], me['binding'], me['resetsAt'])
        _write_atomic(SAMPLES, {'email': cur['email'], 'samples': []})
        _record_history(chain, cur['email'])
        return {'action': 'switched' if action == 'stay' else action, 'from': me['email'], 'to': cur['email'], 'fromUsed': me['worstUsed'], 'toUsed': cur.get('worstUsed'), 'active': cur['email'], 'used': cur.get('worstUsed'), 'binding': cur.get('binding'), 'windows': cur.get('used') or {}, **extra, **{**pace, 'burnRate': None, 'etaS': None, 'urgent': False, 'nextCheckS': round(POST_SWITCH_CHECK_S)}}
    for attempt in range(MAX_SWITCH_TRIES):
        last = attempt == MAX_SWITCH_TRIES - 1
        target, tier = pick(rows_from_cache(), exclude=tried)
        if cur['email'] == me['email']:
            if target is not None and me['worstUsed'] < EXHAUSTED_AT:
                gain = me['worstUsed'] - target['worstUsed']
                if gain < MIN_GAIN:
                    return conclude('stay', {'why': '最好的目标 %s 也要 %.0f%%, 只好 %.0f 个点 (要 %.0f 才值得切); 继续把当前这个用到 %.0f%% 再说' % (target['email'], target['worstUsed'], gain, MIN_GAIN, EXHAUSTED_AT), 'dataAge': round(me['age']), 'fresh': fresh_ok})
            if target is None:
                if me['worstUsed'] < EXHAUSTED_AT:
                    others = sorted(tried - {me['email']})
                    tried_note = '试过 %d 个 (%s) 都没切过去, 剩下的' % (len(others), ', '.join(others)) if others else '别的号'
                    return conclude('stay', {'why': '没有更好的去处 (%s最紧那道闸都 >= %.0f%%); 当前这个 %.0f%% 还有余量, 继续用到 %.0f%%' % (tried_note, USABLE_CEILING, me['worstUsed'], EXHAUSTED_AT), **({'switchFailed': True} if others else {}), 'dataAge': round(me['age']), 'fresh': fresh_ok})
                return conclude('blocked', soonest_extra())
        else:
            home = home_option()
            cands = [home] if last and home is not None else [x for x in (target, home) if x is not None]
            best = min(cands, key=lambda r: r['worstUsed']) if cands else None
            if best is None or (cur.get('worstUsed') is not None and best['worstUsed'] >= cur['worstUsed']):
                return conclude('blocked' if burned(cur) else 'stay', {'why': '落到的 %s 不能用, 能去的都不比它好' % cur['email'], **soonest_extra()})
            if best is home:
                target, tier = (home, '回原号')
            else:
                target = best
        if dry_run:
            return {'action': 'would-switch', 'from': me['email'], 'to': target['email'], 'fromUsed': me['worstUsed'], 'toUsed': target['worstUsed'], 'toAge': round(target['age']), 'tier': tier, 'windows': target['used'], **pace}
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
            vacate(landed_email, cur['worstUsed'], cur['binding'], cur['resetsAt'])
            continue
        if landed_email not in (target['email'], me['email']) and (cur.get('worstUsed') is None or cur['worstUsed'] >= USABLE_CEILING):
            continue
        if cur['email'] == me['email']:
            return conclude('stay', {'why': '落到的 %s 也满了, 已切回原号' % ' → '.join(hops[:-1])})
        extra = {'tier': tier}
        if len(hops) > 1:
            extra['why'] = '中途落到 %s 不能用, 最后停在 %s' % (' → '.join(hops[:-1]), cur['email'])
        return conclude('switched', extra)
    if cur['email'] == me['email'] and me['worstUsed'] < EXHAUSTED_AT:
        return conclude('stay', {'why': '切换没成功 (连试 %d 个: %s); 当前这个 %.0f%% 还能用, 继续用到 %.0f%%' % (MAX_SWITCH_TRIES, ', '.join(sorted(tried - {me['email']})), me['worstUsed'], EXHAUSTED_AT), 'switchFailed': True, 'dataAge': round(me['age']), 'fresh': fresh_ok})
    return conclude('blocked' if cur['email'] == me['email'] or burned(cur) else 'stay', {'why': '连试 %d 个都不可用' % MAX_SWITCH_TRIES, **soonest_extra()})

def _finalize(res: dict) -> dict:
    res.setdefault('activeEmail', res.get('to') or res.get('active') or '')
    res['countedWindows'] = sorted(usage.counted_windows(res.get('windows') or {}))
    return res

def main() -> int:
    args = sys.argv[1:]
    if '-h' in args or '--help' in args:
        print('用法: claude-autoswitch-decide.py [--dry-run]')
        print('  --dry-run   只判断并打印结果，不切账号')
        print('  不带参数    真的执行切换')
        print('退出码: 0=切了  2=不用切 (含"没有更好的去处")  3=没得切 (当前号也 >= %.0f%%)  1=出错' % EXHAUSTED_AT)
        print('环境变量: CCSWITCH_THRESHOLD (默认 90)  CCSWITCH_MODELS (默认空 = 每模型周窗口只显示不计入; Fable / all)')
        print('  当前: %s' % usage.models_note())
        return 0
    unknown = [a for a in args if a != '--dry-run']
    if unknown:
        print('未知参数: %s；本脚本只接受 --dry-run' % ' '.join(unknown), file=sys.stderr)
        print('（不带 --dry-run 会真的切账号，所以拼错的参数一律按出错处理，不替你猜。）', file=sys.stderr)
        return 1
    res = _finalize(decide('--dry-run' in args))
    print(json.dumps(res, ensure_ascii=False))
    return {'stay': 2, 'switched': 0, 'would-switch': 0, 'blocked': 3}.get(res['action'], 1)
if __name__ == '__main__':
    sys.exit(main())
