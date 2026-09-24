from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
_PROBE_THRESHOLD = '50'
_KNOWN_SCHEMA = 1
DEFAULT_MAX_AGE_S = 300
DEFAULT_MIN_GAIN = 10.0

def _run(argv, timeout=180):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return (r.returncode, r.stdout or '', r.stderr or '')
    except Exception as e:
        return (-1, '', '%s: %s' % (type(e).__name__, e))

def cache_age_s() -> float | None:
    from ccpick_usage import load_usage
    ts = [a.get('fetchedAt') for a in (load_usage().get('accounts') or {}).values() if a.get('fetchedAt')]
    if not ts:
        return None
    return datetime.now().timestamp() - max(ts)

def refresh(cswap: str | None, max_age_s: float) -> str:
    age = cache_age_s()
    if age is None:
        why = '本地还没有缓存'
    elif age <= max_age_s:
        return '缓存 %.0f 分钟前采集，够新，跳过刷新' % (age / 60)
    else:
        why = '缓存已 %.0f 分钟' % (age / 60)
    if not cswap:
        return '%s，但找不到 cswap，无法刷新' % why
    rc, _, err = _run([cswap, 'list'], timeout=180)
    if rc != 0:
        return '%s，刷新失败（rc=%s %s）' % (why, rc, err.strip()[:80])
    new = cache_age_s()
    return '%s → 已刷新（%.0f 秒前）' % (why, new if new is not None else 0)

def probe_cswap(cswap: str | None, model: str | None) -> tuple[dict, str]:
    if not cswap:
        return ({}, '找不到 cswap')
    from ccpick_usage import cswap_model_args
    argv = [cswap, 'auto', '--once', '--dry-run', '--json', '--strategy', 'best', '--threshold', _PROBE_THRESHOLD] + cswap_model_args(model)
    rc, out, err = _run(argv, timeout=180)
    poll = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('event') == 'poll':
            poll = ev
    if poll is None:
        return ({}, 'cswap auto 没吐出 poll 事件（rc=%s %s）' % (rc, (err or out).strip()[:80]))
    sv = poll.get('schemaVersion')
    if sv != _KNOWN_SCHEMA:
        return ({}, 'cswap 事件 schemaVersion=%r（本工具只认 %d），不敢硬读' % (sv, _KNOWN_SCHEMA))
    head = poll.get('headroomPct') or {}
    wins = poll.get('windowsPct') or {}
    errs = poll.get('fetchErrors') or {}
    out_rows = {}
    for slot, hr in head.items():
        out_rows[str(slot)] = {'headroom': hr, 'windows': wins.get(str(slot)) or {}, 'error': errs.get(str(slot))}
    return (out_rows, 'cswap 实时余量（%s）' % _models_label(model))

def _models_label(model: str | None) -> str:
    from ccpick_usage import counted_models
    count_all, names = counted_models(model)
    if count_all:
        return '含全部按模型的周额度'
    if names:
        return '含 %s 周额度' % ', '.join(sorted(names))
    return '只算 5h/7d，按模型的周额度不计入'

def _wanted_windows(model: str | None, names) -> list[str]:
    from ccpick_usage import counts_toward_limit
    return [k for k in names if k not in ('5h', '7d') and counts_toward_limit(k, model)]

def probe_local(model: str | None) -> tuple[dict, str]:
    from ccpick_usage import collect
    rows = {}
    for r in collect():
        w = r['windows']
        keys = ['5h', '7d'] + _wanted_windows(model, w)
        pcts, wd = ([], {})
        for k in keys:
            v = w.get(k) or {}
            p = v.get('pct')
            if p is not None:
                pcts.append(100.0 - float(p))
            wd[k] = p
        rows[str(r['slot'])] = {'headroom': min(pcts) if pcts else None, 'windows': wd, 'error': r.get('error')}
    return (rows, '本地缓存自算（cswap 探测失败）')

def merge_probes(primary: dict, fallback: dict) -> tuple[dict, list[str]]:
    out, patched = ({}, [])
    for slot in set(primary) | set(fallback):
        p = primary.get(slot) or {}
        f = fallback.get(slot) or {}
        if p.get('headroom') is not None:
            out[slot] = p
        elif f.get('headroom') is not None:
            out[slot] = dict(f, patched=True)
            patched.append(slot)
        else:
            out[slot] = p or f
    return (out, patched)

def slot_meta() -> dict:
    from ccpick_usage import collect
    out = {}
    for r in collect():
        out[str(r['slot'])] = {'email': r['email'], 'active': r['active'], 'windows': r['windows'], 'error': r.get('error'), 'disabled': r.get('disabled', False)}
    return out

def rank(probe: dict, meta: dict) -> list[dict]:
    from ccpick_usage import fetch_error_kind, known_status
    ks = known_status()
    rows = []
    for slot, m in meta.items():
        p = probe.get(slot) or {}
        email = m['email']
        hr = p.get('headroom')
        err = p.get('error') or m.get('error')
        kind = fetch_error_kind(err)
        why_bad = None
        st = ks.get(email, {})
        if m.get('disabled'):
            why_bad = 'Account disabled for automatic rotation'
        elif st.get('status') == 'account_on_hold':
            why_bad = '账号被 Anthropic 暂停（%s 实测），重新入库无效' % st.get('observed', '')
        elif err == 'invalid_grant':
            why_bad = '凭据已失效，需要重新入库（ccpick enroll）'
        elif err == 'http-403':
            why_bad = 'setup-token 拿不到用量（正常，非故障）'
        elif kind == 'dead':
            why_bad = '凭据已失效（%s），需要重新入库（ccpick enroll）' % err
        elif kind == 'unknown':
            why_bad = '拿不到用量（%s）' % err
        elif hr is None:
            why_bad = '暂时拿不到用量（cswap 与本地缓存都没有数据），跑 cswap list 再看'
        elif hr <= 0:
            scored_now = {k: v for k, v in (p.get('windows') or {}).items() if v is not None}
            k_bind = max(scored_now, key=scored_now.get) if scored_now else '5h'
            wb = m['windows'].get(k_bind) or {}
            label = {'5h': '5 小时窗口', '7d': '周额度'}.get(k_bind, k_bind + ' 周额度')
            at = wb.get('at')
            why_bad = '额度已用尽（%s）%s' % (label, '，%s 恢复（还有 %s）' % (at, wb.get('in')) if at and at != '—' else '')
        scored = p.get('windows') or {}
        disp = {k: (v or {}).get('pct') for k, v in m['windows'].items()}
        disp.update({k: v for k, v in scored.items() if v is not None})
        rows.append({'slot': slot, 'email': email, 'active': m['active'], 'headroom': hr, 'windows': scored, 'display': disp, 'resets': {k: (v or {}).get('in') for k, v in m['windows'].items()}, 'usable': why_bad is None, 'why_bad': why_bad, 'note': '上次取数 %s，用的是缓存里的数字' % err if why_bad is None and kind == 'transient' else None})
    rows.sort(key=lambda r: (not r['usable'], -(r['headroom'] or 0) if r['usable'] else 0, int(r['slot']) if r['slot'].isdigit() else 999))
    return rows

def binding_reason(row: dict) -> str:
    w = row.get('windows') or {}
    pairs = [(k, v) for k, v in w.items() if v is not None]
    if not pairs:
        return ''
    k, v = max(pairs, key=lambda kv: kv[1])
    label = {'5h': '5 小时窗口', '7d': '周额度'}.get(k, k + ' 额度')
    return '%s用了 %.0f%%，是它最紧的一道' % (label, v)

def switch_and_verify(cswap: str, email: str, timeout_s: int=25) -> tuple[bool, str]:
    from ccpick_enroll import auth_status
    rc, out, err = _run([cswap, 'switch', email], timeout=180)
    if rc != 0:
        return (False, 'cswap switch 失败（rc=%s）: %s' % (rc, (err or out).strip()[:200]))
    MIN_POLLS = 8
    HARD_CEILING_MULT = 4
    t0 = time.time()
    last = None
    polls = 0
    worst = 0.0
    while True:
        t_poll = time.time()
        st = auth_status()
        worst = max(worst, time.time() - t_poll)
        polls += 1
        last = st.get('email')
        if last == email:
            return (True, '已生效（%s）' % (st.get('subscriptionType') or '?'))
        elapsed = time.time() - t0
        if elapsed >= timeout_s * HARD_CEILING_MULT:
            break
        if elapsed >= timeout_s and polls >= MIN_POLLS:
            break
        time.sleep(1)
    return (False, '切换命令成功了，但 %.0f 秒内采样 %d 次，claude 报的身份仍是 %r（单次 auth status 最慢 %.1fs）' % (time.time() - t0, polls, last, worst))

def autopilot(profile_dir: str, email: str, timeout_s: int=240, *, headless: bool=False, user_agent: str | None=None, max_attempts: int=6, cdp_context: dict | None=None) -> tuple[bool, str]:
    here = Path(__file__).resolve().parent
    cdp_unavailable = ''
    try:
        from ccpick import chrome_binary, chrome_user_data_dir
        from ccpick_cdp import backend_status, run_authorization
        existing_pipe = cdp_context.get('pipe') if cdp_context is not None else None
        if existing_pipe is not None and (not existing_pipe.alive):
            return (False, '[flow-failure] 批处理 CDP Chrome 已提前退出；拒绝中途改用 AppleScript/UIA 或另起实例')
        if existing_pipe is not None:
            cdp_ready, cdp_why = (True, 'CDP pipe（复用本批次 Chrome）')
        else:
            cdp_ready, cdp_why = backend_status(chrome_binary(), chrome_user_data_dir())
        if cdp_ready:
            if timeout_s == 0:
                return (True, cdp_why)
            exe = chrome_binary()
            if not exe:
                cdp_unavailable = 'CDP pipe 不可用：找不到 Chrome 可执行文件'
            else:
                ok, detail = run_authorization(exe, profile_dir, email, timeout_s=timeout_s, headless=headless, user_agent=user_agent, max_attempts=max_attempts, config_dir=os.environ.get('CLAUDE_CONFIG_DIR'), batch_context=cdp_context)
                if cdp_context is not None and cdp_context.get('pipe') is not None:
                    return (ok, '[backend=cdp] ' + detail)
                if not detail.startswith('[cdp-unavailable]'):
                    return (ok, '[backend=cdp] ' + detail)
                cdp_unavailable = detail
        else:
            cdp_unavailable = cdp_why
    except Exception as e:
        if cdp_context is not None and cdp_context.get('pipe') is not None:
            return (False, '[backend=cdp] [flow-failure] 批处理 CDP 状态异常（%s）；拒绝中途改用 AppleScript/UIA\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0' % type(e).__name__)
        cdp_unavailable = 'CDP pipe 探测失败（%s）' % type(e).__name__
    if headless or user_agent:
        return (False, '[flow-failure] %s；请完全退出 Chrome 后重试 CDP' % cdp_unavailable)
    if sys.platform == 'win32':
        if timeout_s == 0:
            return (True, '本平台没有额外的逐 profile 自动化前置条件')
        ps1 = here / 'auto_authorize.ps1'
        if not ps1.is_file():
            return (False, '找不到 auto_authorize.ps1')
        argv = [_runtime.powershell_path(), '-WindowStyle', 'Hidden', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ps1), '-Profile', profile_dir, '-Email', email, '-TimeoutSec', str(timeout_s), '-NoAdd', '-CcpickLauncher', _runtime.launcher_path() or '']
        config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
        if config_dir:
            argv += ['-ConfigDir', config_dir]
        rc, out, err = _run(argv, timeout=timeout_s + 120)
        full = (out or err).strip()
        detail = full[-600:]
        refused = '★授权被拒★' in full
        zero_click = 'signin=False(0次)' in full and 'google=False(0次)' in full and ('consent=0次' in full) and ('authorize=False(0次)' in full)
        if refused and zero_click:
            return (False, '[account-refusal] ' + detail)
        if refused:
            return (False, '[flow-failure] ' + detail)
        return (rc == 0, detail)
    if sys.platform == 'darwin':
        if timeout_s == 0:
            from ccpick import _probe_chrome_js_gate
            ready, detail = _probe_chrome_js_gate(profile_dir)
            if ready is False:
                detail = 'Apple Events JS gate 未启用（profile 专属设置）'
            return (ready, detail)
        config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
        if os.environ.get('CCPICK_AUTOPILOT_ALLOW_MANUAL') == '1':
            from ccpick import _probe_chrome_js_gate
            js_ok, js_detail = _probe_chrome_js_gate(profile_dir)
            if js_ok is not True:
                from ccpick_enroll import launcher
                lp = launcher()
                if not lp:
                    return (False, '[flow-failure] 找不到 ccpick launcher，无法启动人工点击流程')
                argv = [lp, 'enroll', '--profile', profile_dir, '--email', email, '--timeout', str(timeout_s)]
                if config_dir:
                    argv += ['--config-dir', config_dir]
                rc, out, err = _run(argv, timeout=timeout_s + 120)
                detail = (out or err).strip()[-600:] or js_detail
                return (rc == 0, '[manual-click] ' + detail)
        script = here / 'ccpick_auto_authorize.py'
        if not script.is_file():
            return (False, '找不到 ccpick_auto_authorize.py')
        if not sys.executable or not Path(sys.executable).is_absolute():
            return (False, '当前 Python 解释器不是绝对路径，拒绝启动自动授权')
        argv = [sys.executable, str(script), '--profile', profile_dir, '--email', email, '--timeout', str(timeout_s), '--no-add', '--max-attempts', str(max_attempts)]
        if config_dir:
            argv += ['--config-dir', config_dir]
        rc, out, err = _run(argv, timeout=timeout_s + 120)
        detail = (out or err).strip()[-600:]
        if not detail:
            detail = 'macOS 自动授权退出码 %s' % rc
        if rc == 3:
            return (False, '[account-refusal] ' + detail)
        if rc == 4:
            return (False, '[flow-failure] ' + detail)
        return (rc == 0, detail)
    return (False, '%s 上还没有自动点授权的实现，请人工点一次' % sys.platform)

def probe_automation_prerequisite(profile_dir: str, *, headless: bool=False, user_agent: str | None=None) -> tuple[bool | None, str]:
    return autopilot(profile_dir, '', 0, headless=headless, user_agent=user_agent)

def render(rows: list[dict], src: str, fresh: str, model: str | None) -> str:
    out = []
    show = model.strip() if model and ',' not in model and (model.strip().lower() not in ('all', 'none')) else 'Fable'
    out.append('账号自动挑选        余量 = 算数的闸里剩得最少的那个；右边 5h/7d/%s 三列是【已用】%%' % show)
    out.append('数据: %s ｜ %s' % (src, fresh))
    out.append('=' * 76)
    out.append('  余量  账号                            5h    7d   %-5s 备注' % show)
    for r in rows:
        w = r['display']
        mark = '←当前' if r['active'] else ''
        if r['usable']:
            best = ' ★最优' if r is next((x for x in rows if x['usable']), None) else ''

            def pc(k):
                v = w.get(k)
                return '%4.0f%%' % v if v is not None else '   —'
            scored = {k: v for k, v in (r.get('windows') or {}).items() if v is not None}
            k_bind = max(scored, key=scored.get) if scored else None
            hidden = '  卡在 %s %.0f%%' % (k_bind, scored[k_bind]) if k_bind and k_bind not in ('5h', '7d', show) else ''
            out.append('  %4.0f%%  %-30s %s %s %s %s%s%s%s' % (r['headroom'], r['email'], pc('5h'), pc('7d'), pc(show), mark, best, hidden, '  （%s）' % r['note'] if r.get('note') else ''))
        else:
            out.append('     —   %-30s %s%s' % (r['email'], r['why_bad'], '  ' + mark if mark else ''))
    return '\n'.join(out)

def cmd_auto(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick auto', description='切到现在最该用的那个账号')
    ap.add_argument('--dry-run', action='store_true', help='只报告，不切')
    ap.add_argument('--model', help='把这些按模型的周额度也算进余量，如 Fable 或 all（默认看 CCSWITCH_MODELS，不设就只算 5h/7d）')
    ap.add_argument('--min-gain', type=float, default=DEFAULT_MIN_GAIN, help='余量至少高这么多个点才值得切（默认 %.0f）' % DEFAULT_MIN_GAIN)
    ap.add_argument('--max-age', type=float, default=DEFAULT_MAX_AGE_S, help='缓存超过这么多秒就先刷新（默认 %d）' % DEFAULT_MAX_AGE_S)
    ap.add_argument('--no-refresh', action='store_true', help='不刷新，直接用现成缓存')
    ap.add_argument('--enroll', action='store_true', help='一个能用的都没有时，自动跑一次入库（会开浏览器）')
    ap.add_argument('--json', action='store_true', help='机器可读')
    ns = ap.parse_args(args)
    from ccpick_enroll import cswap_bin
    from ccpick_usage import collect, snapshot
    res = {}
    say = (lambda *a, **k: None) if ns.json else print

    def finish(code: int) -> int:
        if ns.json:
            res['exit'] = code
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return code
    cswap = cswap_bin()
    fresh = '跳过刷新（--no-refresh）' if ns.no_refresh else refresh(cswap, ns.max_age)
    probe, src = probe_cswap(cswap, ns.model)
    local, lsrc = probe_local(ns.model)
    if not probe:
        probe, src = (local, lsrc)
    else:
        probe, patched = merge_probes(probe, local)
        if patched:
            src += '（%d 个槽位本轮没采到，已用本地缓存补）' % len(patched)
    meta = slot_meta()
    if not meta:
        if ns.enroll and (not ns.dry_run):
            code, detail = _enroll_fallback([], say)
            res.update(action='enroll', detail=detail)
            return finish(code)
        res.update(action='error', reason='读不到任何账号数据')
        say('读不到任何账号数据。先跑一次 cswap list。', file=sys.stderr)
        return finish(1)
    rows = rank(probe, meta)
    usable = [r for r in rows if r['usable']]
    cur = next((r for r in rows if r['active']), None)
    best = usable[0] if usable else None
    res.update(source=src, freshness=fresh, rows=rows, current=cur['email'] if cur else None, best=best['email'] if best else None)
    say(render(rows, src, fresh, ns.model))
    say('-' * 76)
    if not best:
        res.update(action='no-target')
        say('没有任何账号现在能用。')
        for r in rows:
            if r['why_bad']:
                say('   %-30s %s' % (r['email'], r['why_bad']))
        if ns.enroll and (not ns.dry_run):
            code, detail = _enroll_fallback(rows, say)
            res.update(action='enroll', detail=detail)
            return finish(code)
        say('')
        say('下一步：ccpick auto --enroll   （会开浏览器走一次授权）')
        return finish(3)
    cur_hr = cur['headroom'] if cur and cur['usable'] else None
    if cur and best['email'] == cur['email']:
        res.update(action='no-switch', reason='already-best', headroom=best['headroom'])
        say('已经在最优账号上：%s（余量 %.0f%%）' % (cur['email'], best['headroom']))
        say('   %s' % binding_reason(best))
        return finish(2)
    gain = best['headroom'] - cur_hr if cur_hr is not None else None
    if gain is not None and gain < ns.min_gain:
        res.update(action='no-switch', reason='gain-too-small', gain=gain, min_gain=ns.min_gain)
        say('当前 %s（余量 %.0f%%）与最优 %s（%.0f%%）只差 %.0f 个点，不值得折腾。' % (cur['email'], cur_hr, best['email'], best['headroom'], gain))
        say('   要切就加 --min-gain 0')
        return finish(2)
    frm = cur['email'] if cur else None
    res.update(action='switch', **{'from': frm, 'to': best['email'], 'gain': gain, 'why': binding_reason(best)})
    say('决定: %s  →  %s' % (frm or '(读不到当前身份)', best['email']))
    say('      余量 %s → %.0f%%%s' % ('%.0f%%' % cur_hr if cur_hr is not None else '(当前账号不可用)', best['headroom'], '  (+%.0f)' % gain if gain is not None else ''))
    say('      理由: %s' % binding_reason(best))
    if ns.dry_run:
        res.update(action='dry-run')
        say('（--dry-run，没有真的切）')
        return finish(0)
    if not cswap:
        res.update(action='error', reason='找不到 cswap')
        say('找不到 cswap，无法切换。', file=sys.stderr)
        return finish(1)
    ok, why = switch_and_verify(cswap, best['email'])
    res.update(verified=ok, verify_detail=why)
    say('切换: %s' % why)
    if not ok:
        return finish(1)
    try:
        snapshot(collect(), 'auto-switch -> %s' % best['email'])
    except Exception:
        pass
    return finish(0)

def _enroll_fallback(rows: list[dict], say=print) -> tuple[int, str]:
    from ccpick import list_profiles
    from ccpick_usage import known_status
    known = {r['email'] for r in rows if r['email']}
    ks = known_status()
    cands = [p for p in list_profiles() if p['account'] and p['account'] not in known and (ks.get(p['account'], {}).get('status') != 'account_on_hold')]
    if not cands:
        say('')
        say('也没有可入库的 Chrome 配置文件了（都已入库或已确认被暂停）。')
        say('要加新账号：先在 Chrome 里新建配置文件并登录 claude.ai，再跑 ccpick list。')
        return (3, '没有可入库的配置文件')
    if len(cands) > 1:
        say('')
        say('有多个候选配置文件，不替你选：')
        for p in cands:
            say('   %-12s %s' % (p['dir'], p['account']))
        say('挑一个：ccpick enroll --profile "<目录名>" --add')
        return (3, '候选不唯一: ' + ', '.join((p['dir'] for p in cands)))
    p = cands[0]
    say('')
    say('唯一候选: %s (%s)，开始自动授权……' % (p['dir'], p['account']))
    ok, detail = autopilot(p['dir'], p['account'])
    say(detail)
    if not ok:
        say('')
        say('自动授权没跑成。人工路径：ccpick enroll --profile "%s" --add' % p['dir'])
        return (1, detail)
    from ccpick_enroll import auth_status, cswap_bin
    status = auth_status()
    if not (status.get('loggedIn') and status.get('email') == p['account']):
        detail = '自动授权结束，但 claude auth status 不是目标账号 %r' % p['account']
        say(detail)
        return (1, detail)
    cswap = cswap_bin()
    if not cswap:
        return (1, '登录成功，但找不到 cswap，无法入库')
    rc, out, err = _run([cswap, 'add'], timeout=180)
    if rc != 0:
        return (1, '登录成功，但 cswap add 失败（rc=%s）: %s' % (rc, (err or out).strip()[:200]))
    return (0, detail)
