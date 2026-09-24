from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
CODE_WAIT_S = 600
LOGIN_FINISH_S = 120

def _find(name: str, extra: list[str] | None=None) -> str | None:
    p = shutil.which(name)
    if p and Path(p).is_absolute():
        return p
    for c in extra or []:
        if c and Path(c).is_file():
            return c
    return None

def _system_exe(name: str) -> str | None:
    root = os.environ.get('SystemRoot') or 'C:\\Windows'
    p = Path(root) / 'System32' / name
    return str(p) if p.is_file() else None

def claude_bin() -> str | None:
    appdata = os.environ.get('APPDATA', '')
    if sys.platform == 'win32' and appdata:
        real = Path(appdata) / 'npm' / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'bin' / 'claude.exe'
        if real.is_file():
            return str(real)
    return _find('claude', [str(Path(appdata) / 'npm' / 'claude.cmd') if appdata else '', str(Path.home() / '.local' / 'bin' / 'claude.exe'), str(Path.home() / '.local' / 'bin' / 'claude'), '/usr/local/bin/claude'])

def kill_tree(proc) -> None:
    if sys.platform == 'win32':
        tk = _system_exe('taskkill.exe')
        if tk:
            try:
                subprocess.run([tk, '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=30)
                return
            except Exception:
                pass
    try:
        proc.kill()
    except Exception:
        pass

def refresh_and_check_quota(email: str) -> tuple[bool | None, str]:
    cs = cswap_bin()
    if cs:
        try:
            rr = subprocess.run([cs, 'list', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
            if rr.returncode != 0:
                return (None, '额度未判定：cswap list --json 失败（rc=%d）' % rr.returncode)
            payload = json.loads((rr.stdout or '{}').lstrip('\ufeff'))
            if not (payload.get('accounts') or []):
                return (None, '额度未判定：claude-swap 尚无托管账号，无法预热用量缓存')
        except Exception as e:
            return (None, '额度未判定：cswap 刷新失败（%s）' % type(e).__name__)
    else:
        return (None, '额度未判定：找不到 cswap')
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ccpick_usage import collect, is_usable
        for r in collect():
            if r['email'] == email:
                ok, why = is_usable(r)
                return (ok, why)
        return (None, '额度未判定：缓存里没有这个账号的用量数据')
    except Exception as e:
        return (None, '额度未判定：查用量失败（%s）' % type(e).__name__)

def best_account(exclude: str | None=None) -> tuple[str | None, str]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ccpick_auto import probe_cswap, probe_local, rank, slot_meta, binding_reason
        probe, _ = probe_cswap(cswap_bin(), None)
        if not probe:
            probe, _ = probe_local(None)
        for r in rank(probe, slot_meta()):
            if not r['usable'] or (exclude and r['email'] == exclude):
                continue
            return (r['email'], '余量 %.0f%%，%s' % (r['headroom'], binding_reason(r)))
        return (None, '没有可用账号')
    except Exception as e:
        return (None, '挑选失败（%s: %s）' % (type(e).__name__, e))

def cswap_bin() -> str | None:
    return _backend.executable()

def launcher() -> str | None:
    return _runtime.launcher_path()

def auth_status(config_dir: str | None=None) -> dict:
    cb = claude_bin()
    if not cb:
        return {}
    env = dict(os.environ)
    if config_dir:
        env['CLAUDE_CONFIG_DIR'] = config_dir
    env['BROWSER'] = 'true'
    try:
        out = subprocess.run([cb, 'auth', 'status'], capture_output=True, text=True, timeout=60, env=env, encoding='utf-8', errors='replace').stdout
        return json.loads(out.lstrip('\ufeff').strip())
    except Exception:
        return {}

def _pump(stream, q):
    try:
        for line in iter(stream.readline, ''):
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)

def _redact_auth_diagnostic(value: object, limit: int=500) -> str:
    return _runtime.redact_auth_diagnostic(value, limit)

def _safe_echo(line: str, verbose: bool) -> None:
    if not verbose or not line.strip():
        return
    if _redact_auth_diagnostic(line) == '[redacted]':
        print('  | (授权 URL 已发往浏览器，按规矩不回显)', flush=True)
    else:
        print('  | ' + line.rstrip(), flush=True)

def enroll_one(profile_dir: str, email: str | None, code_file: Path, config_dir: str | None=None, verbose: bool=True, wait_s: int=CODE_WAIT_S) -> dict:
    _runtime.ensure_data_dir()
    '跑一次登录。返回 {ok, email_before, email_after, reason}。\n\n    wait_s 是显式参数而不是改模块级全局 —— 全局可变状态在批量循环里会互相污染，\n    而且 argparse 的 default=CODE_WAIT_S 先读了它，再 global 声明就是 SyntaxError。\n    '
    cb = claude_bin()
    if not cb:
        return {'ok': False, 'reason': '找不到 claude 可执行文件'}
    lp = launcher()
    if not lp:
        b = Path.home() / '.local' / 'bin'
        return {'ok': False, 'reason': '找不到 ccpick 入口脚本（%s）。BROWSER 只能是单个可执行路径，没有回退形式，请先创建它。' % (b / ('ccpick.cmd' if sys.platform == 'win32' else 'ccpick'))}
    before = auth_status(config_dir).get('email')
    env = dict(os.environ)
    env['CCPICK_PROFILE'] = profile_dir
    env['BROWSER'] = lp
    if config_dir:
        env['CLAUDE_CONFIG_DIR'] = config_dir
    argv = [cb, 'auth', 'login']
    if email:
        argv += ['--email', email]
    code_file.unlink(missing_ok=True)
    if verbose:
        print('[enroll] profile=%s email=%s' % (profile_dir, email or '(未指定)'), flush=True)
        print('[enroll] 等 code 投递到: %s' % code_file, flush=True)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env, bufsize=1)
    q = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()
    saw_prompt = False
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            line = q.get(timeout=1)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        _safe_echo(line, verbose)
        if 'paste code' in line.lower():
            saw_prompt = True
            break
    if verbose:
        print('[enroll] %s' % ('已出现粘贴提示' if saw_prompt else '未见粘贴提示，仍继续等 code'), flush=True)
    code = None
    auto_done = False
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if proc.poll() is not None:
            auto_done = True
            break
        try:
            line = q.get_nowait()
            if line:
                _safe_echo(line, verbose)
        except queue.Empty:
            pass
        if code_file.is_file():
            raw = code_file.read_text(encoding='utf-8').strip()
            if raw:
                code = raw
                break
        time.sleep(1)
    if auto_done:
        if verbose:
            print('[enroll] 子进程已自行结束（localhost 回调自动完成，无需粘贴 code）', flush=True)
    else:
        if code is None:
            kill_tree(proc)
            return {'ok': False, 'email_before': before, 'reason': '超时：授权未完成，且没有 code 被投递'}
        code_file.unlink(missing_ok=True)
        try:
            proc.stdin.write(code + '\n')
            proc.stdin.flush()
        except Exception as e:
            kill_tree(proc)
            return {'ok': False, 'email_before': before, 'reason': '写入 code 失败: %r' % (e,)}
        t0 = time.time()
        while time.time() - t0 < LOGIN_FINISH_S and proc.poll() is None:
            try:
                line = q.get(timeout=1)
                if line:
                    _safe_echo(line, verbose)
            except queue.Empty:
                pass
        if proc.poll() is None:
            kill_tree(proc)
    st = auth_status(config_dir)
    after = st.get('email')
    changed = bool(after) and after != before
    logged = bool(st.get('loggedIn')) and bool(after)
    if email:
        ok = logged and after == email
        why = '' if ok else '目标是 %r，登录后却是 %r' % (email, after)
    else:
        ok = logged and changed
        why = '' if ok else '身份未变（仍是 %r）' % (after,)
    if ok and after:
        from ccpick import record_profile_account
        record_profile_account(profile_dir, after)
    return {'ok': ok, 'changed': changed, 'email_before': before, 'email_after': after, 'target': email, 'auto': auto_done, 'reason': why}

def pick_many(profiles: list[dict], preselect: set[str] | None=None) -> list[str] | None:
    import tkinter as tk
    from tkinter import ttk
    out: list = [None]
    root = tk.Tk()
    root.title('ccpick — 选要入库的 Chrome 配置文件（可多选）')
    root.geometry('620x480')
    root.attributes('-topmost', True)
    root.after(400, lambda: root.attributes('-topmost', False))
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    ttk.Label(root, justify='left', text='按住 Ctrl / Shift 多选。会 **依次** 处理，每个都需要你在对应的\nChrome 窗口里点一次「授权」。并发会互相覆盖凭据，所以只能串行。').pack(anchor='w', padx=12, pady=(12, 6))
    frame = ttk.Frame(root)
    frame.pack(fill='both', expand=True, padx=12)
    sb = ttk.Scrollbar(frame, orient='vertical')
    lb = tk.Listbox(frame, selectmode='extended', yscrollcommand=sb.set, exportselection=False)
    sb.config(command=lb.yview)
    sb.pack(side='right', fill='y')
    lb.pack(side='left', fill='both', expand=True)
    for i, p in enumerate(profiles):
        bits = [p['name']]
        if p['account']:
            bits.append(p['account'])
        if p['label']:
            bits.append(p['label'])
        lb.insert('end', '   ·   '.join(bits) + '      [%s]' % p['dir'])
        if preselect and p['dir'] in preselect:
            lb.selection_set(i)
    lb.focus_set()

    def confirm(_e=None):
        out[0] = [profiles[i]['dir'] for i in lb.curselection()]
        root.destroy()

    def cancel(_e=None):
        out[0] = None
        root.destroy()
    root.bind('<Escape>', cancel)
    root.protocol('WM_DELETE_WINDOW', cancel)
    bar = ttk.Frame(root)
    bar.pack(fill='x', padx=12, pady=10)
    ttk.Button(bar, text='开始入库', command=confirm).pack(side='right')
    ttk.Button(bar, text='取消', command=cancel).pack(side='left')
    root.after(600 * 1000, cancel)
    root.mainloop()
    return out[0]

def _split_profile_args(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    out = []
    seen = set()
    for value in values:
        for item in value.split(','):
            item = item.strip()
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    return out

def _managed_account_rows(path: Path | None=None) -> tuple[list[dict], str | None]:
    if path is None:
        from ccpick_usage import SEQ
        path = SEQ
    if not path.exists():
        return ([], None)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        accounts = payload.get('accounts') or {}
        if not isinstance(accounts, dict):
            raise ValueError('accounts 不是对象')
        rows = []
        for slot, account in accounts.items():
            if not isinstance(account, dict):
                continue
            email = account.get('email')
            if email:
                rows.append({'slot': str(slot), 'email': str(email)})
        rows.sort(key=lambda r: (int(r['slot']) if r['slot'].isdigit() else 999, r['slot']))
        return (rows, None)
    except Exception as e:
        return ([], 'sequence.json 无法解析（%s）' % type(e).__name__)

def classify_auto_enroll_preflight(profiles: list[dict], requested: list[str] | None, managed_emails: set[str], prerequisite_probe, include_gateless: bool, held_emails: set[str] | None=None, profile_accounts: dict | None=None) -> list[dict]:
    by_dir = {p['dir']: p for p in profiles}
    managed_normalized = {email.casefold() for email in managed_emails}
    held_normalized = {e.casefold() for e in held_emails or set()}
    mapping = {k: str(v).strip().casefold() for k, v in (profile_accounts or {}).items() if v}
    dirs = requested if requested is not None else [p['dir'] for p in profiles]
    rows = []
    for profile_dir in dirs:
        p = by_dir.get(profile_dir)
        if p is None:
            rows.append({'dir': profile_dir, 'name': '(不存在)', 'account': '', 'managed': False, 'prerequisite': None, 'prerequisite_detail': '无法探测不存在的 profile', 'attempt': False, 'reason': 'Chrome profile 不存在', 'missing': True})
            continue
        email = p.get('account') or ''
        claude_email = mapping.get(profile_dir)
        match_email = claude_email or email.casefold()
        managed = bool(match_email) and match_email in managed_normalized
        try:
            prereq, prereq_detail = prerequisite_probe(profile_dir)
        except Exception as e:
            prereq = None
            prereq_detail = '前置条件探测失败（%s）' % type(e).__name__
        attempt = False
        reason = ''
        if not email:
            reason = '没有 Google 账号'
        elif managed:
            reason = '已由 cswap 托管'
        elif match_email in held_normalized:
            if claude_email:
                reason = '账号被暂停 (account_on_hold)，重新入库无效'
            else:
                reason = 'Google 账号 %s 在暂停名单里；该 profile 的 Claude 账号未知，先 /login 一次建立映射再判' % email
        elif prereq is True:
            attempt = True
            reason = '将自动尝试'
        elif include_gateless:
            attempt = True
            reason = '将尝试；缺少自动化前置条件，需要人工点击'
        else:
            reason = '缺少自动化前置条件：%s' % prereq_detail
        rows.append({'dir': profile_dir, 'name': p.get('name') or profile_dir, 'account': email, 'managed': managed, 'prerequisite': prereq, 'prerequisite_detail': prereq_detail, 'attempt': attempt, 'reason': reason, 'missing': False})
    return rows

def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))
    lines = [' | '.join((h.ljust(widths[i]) for i, h in enumerate(headers)))]
    lines.append('-+-'.join(('-' * width for width in widths)))
    lines += [' | '.join((value.ljust(widths[i]) for i, value in enumerate(row))) for row in rows]
    return '\n'.join(lines)

def render_auto_enroll_preflight(rows: list[dict]) -> str:
    body = []
    for row in rows:
        prereq = '满足' if row['prerequisite'] is True else '缺失' if row['prerequisite'] is False else '未知'
        prereq += '：' + row['prerequisite_detail']
        body.append([row['dir'], row['name'], row['account'] or '(无)', '是' if row['managed'] else '否', prereq, ('将尝试' if row['attempt'] else '跳过') + '：' + row['reason']])
    return _table(['PROFILE', 'DISPLAY NAME', 'GOOGLE ACCOUNT', 'CSWAP', 'AUTOMATION', 'PLAN'], body)

def auto_enroll_batch_exit_code(attempted: int, failed: int, fatal_error: bool=False) -> int:
    if fatal_error:
        return 1
    if attempted == 0:
        return 2
    return 3 if failed else 0

def _autopilot_evidence(detail: str) -> str:
    if not detail:
        return '无 autopilot 输出'
    m = re.search('阶段计数: *([^\\n；]*)', detail)
    if m:
        counts = m.group(1).strip()
        total = sum((int(x) for x in re.findall('=(\\d+)', counts))) if counts else 0
        return '自动点击 %s（合计 %d 次）' % (counts, total) if total else '自动点击 %s —— ★一次都没点成, 说明这次是人工完成的★' % counts
    return '未见阶段计数（autopilot 可能提前退出）'

def _failure_outcome(detail: str) -> str:
    compact = ' '.join((detail or '未提供失败详情').split())[:500]
    if compact.startswith('[account-refusal]'):
        return '账号侧拒绝（零点击直接错误回调）：' + compact[len('[account-refusal]'):].strip()
    return '流程失败：' + compact.replace('[flow-failure]', '', 1).strip()

def _render_auto_enroll_summary(results: list[dict], accounts: list[dict], accounts_error: str | None) -> None:
    print('\n最终汇总:')
    print(_table(['PROFILE', 'GOOGLE ACCOUNT', 'OUTCOME'], [[r['dir'], r.get('account') or '(无)', r['outcome']] for r in results]))
    print('\n当前 cswap 账号:')
    if accounts_error:
        print('  无法读取：%s' % accounts_error)
    elif not accounts:
        print('  (无)')
    else:
        for account in accounts:
            print('  %s. %s' % (account['slot'], account['email']))

def cmd_auto_enroll(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick auto-enroll')
    ap.add_argument('--profile', required=True, help='Chrome profile 目录名，如 Profile 4')
    ap.add_argument('--email', required=True)
    ap.add_argument('--timeout', type=int, default=240)
    ap.add_argument('--no-add', action='store_true')
    ap.add_argument('--config-dir', help='隔离演练用 CLAUDE_CONFIG_DIR')
    ap.add_argument('--max-attempts', type=int, default=6)
    ap.add_argument('--headless', action='store_true', help='用无窗口 Chrome CDP（Google 登录兼容性尚未实测）')
    ap.add_argument('--user-agent', help='覆盖 Chrome User-Agent；通常只用于 headless 兼容排查')
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    if ns.timeout <= 0 or ns.max_attempts <= 0:
        print('--timeout 和 --max-attempts 必须为正数。', file=sys.stderr)
        return 2
    if any((ord(c) < 32 or ord(c) == 127 for c in ns.email)):
        print('email 含控制字符，拒绝。', file=sys.stderr)
        return 2
    if ns.user_agent and any((ord(c) < 32 or ord(c) == 127 for c in ns.user_agent)):
        print('--user-agent 含控制字符，拒绝。', file=sys.stderr)
        return 2
    from ccpick import list_profiles
    import ccpick_auto
    if ns.profile not in {profile['dir'] for profile in list_profiles()}:
        print('没有这个 Chrome profile: %r' % ns.profile, file=sys.stderr)
        return 2
    old_config = os.environ.get('CLAUDE_CONFIG_DIR')
    try:
        if ns.config_dir:
            os.environ['CLAUDE_CONFIG_DIR'] = ns.config_dir
        auto_ok, detail = ccpick_auto.autopilot(ns.profile, ns.email, ns.timeout, headless=ns.headless, user_agent=ns.user_agent, max_attempts=ns.max_attempts)
    except Exception as e:
        auto_ok, detail = (False, 'autopilot 抛出 %s: %s' % (type(e).__name__, _redact_auth_diagnostic(e)))
    finally:
        if ns.config_dir:
            if old_config is None:
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
            else:
                os.environ['CLAUDE_CONFIG_DIR'] = old_config
    print(detail)
    if '[interrupted]' in detail:
        return 130
    effective_config_dir = ns.config_dir or old_config
    status = auth_status(effective_config_dir)
    after = status.get('email')
    login_ok = auto_ok and (bool(status.get('loggedIn')) and after == ns.email)
    if not login_ok:
        print('failed：%s；claude auth status=%r' % (_failure_outcome(detail), after), file=sys.stderr)
        return 1
    if not ns.no_add and (not effective_config_dir):
        cs = cswap_bin()
        if not cs:
            print('登录身份已验证，但找不到 cswap，无法入库。', file=sys.stderr)
            return 1
        try:
            added = subprocess.run([cs, 'add'], capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
        except Exception as e:
            print('登录身份已验证，但 cswap add 异常（%s）。' % type(e).__name__, file=sys.stderr)
            return 1
        if added.returncode != 0:
            print('登录身份已验证，但 cswap add 失败（rc=%d）。' % added.returncode, file=sys.stderr)
            return 1
    suffix = '（未执行 cswap add）' if ns.no_add or effective_config_dir else ''
    print('enrolled%s ｜ %s' % (suffix, _autopilot_evidence(detail)))
    return 0

def cmd_auto_enroll_all(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick auto-enroll-all')
    ap.add_argument('--profiles', nargs='+', help='配置文件目录名，可空格或逗号分隔')
    ap.add_argument('--include-gateless', action='store_true', help='缺自动化前置条件也尝试；届时需要人工点击')
    ap.add_argument('--no-add', action='store_true', help='登录成功后不跑 cswap add')
    ap.add_argument('--config-dir', help='隔离用的 CLAUDE_CONFIG_DIR；不会 cswap add 或切主账号')
    ap.add_argument('--timeout', type=int, default=240, help='每个 profile 的最长秒数（默认 240）')
    ap.add_argument('--dry-run', action='store_true', help='只打印预检表，不做任何登录或切换')
    ap.add_argument('--headless', action='store_true', help='整批复用一个无窗口 Chrome CDP（Google 登录兼容性尚未实测）')
    ap.add_argument('--user-agent', help='覆盖整批 Chrome User-Agent；通常只用于 headless 排查')
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if ns.timeout <= 0:
        print('--timeout 必须为正数。', file=sys.stderr)
        return 1
    if ns.user_agent and any((ord(c) < 32 or ord(c) == 127 for c in ns.user_agent)):
        print('--user-agent 含控制字符，拒绝。', file=sys.stderr)
        return 1
    from ccpick import list_profiles
    import ccpick_auto
    requested = _split_profile_args(ns.profiles)
    if requested == []:
        print('--profiles 没有给出有效目录名。', file=sys.stderr)
        return 1
    managed_rows, managed_error = _managed_account_rows()
    managed_emails = {r['email'].casefold() for r in managed_rows}
    prerequisite = lambda profile_dir: ccpick_auto.probe_automation_prerequisite(profile_dir, headless=ns.headless, user_agent=ns.user_agent)
    try:
        from ccpick_usage import known_status
        held = {e for e, v in (known_status() or {}).items() if isinstance(v, dict) and v.get('status') == 'account_on_hold'}
    except Exception:
        held = set()
    try:
        from ccpick import load_profile_accounts
        pmap = load_profile_accounts()
    except Exception:
        pmap = {}
    preflight = classify_auto_enroll_preflight(list_profiles(), requested, managed_emails, prerequisite, ns.include_gateless, held_emails=held, profile_accounts=pmap)
    print('auto-enroll-all 预检（尚未登录、切换或写入）:')
    print(render_auto_enroll_preflight(preflight))
    attempts = [r for r in preflight if r['attempt']]
    fatal_preflight = managed_error is not None or any((r['missing'] for r in preflight))
    if managed_error:
        print('\n预检错误：%s；无法可靠判断哪些账号已托管。' % managed_error, file=sys.stderr)
    if ns.dry_run:
        return auto_enroll_batch_exit_code(len(attempts), 0, fatal_preflight)
    if fatal_preflight:
        return 1
    results_by_dir = {}
    for row in preflight:
        if row['managed']:
            results_by_dir[row['dir']] = {'dir': row['dir'], 'account': row['account'], 'outcome': 'already had it（已由 cswap 托管）', 'attempted': False}
        elif not row['attempt']:
            results_by_dir[row['dir']] = {'dir': row['dir'], 'account': row['account'], 'outcome': 'skipped：%s' % row['reason'], 'attempted': False}
    if not attempts:
        print('\n没有需要尝试的 profile。')
        results = [results_by_dir[row['dir']] for row in preflight]
        _render_auto_enroll_summary(results, managed_rows, managed_error)
        return 2
    effective_config_dir = ns.config_dir or os.environ.get('CLAUDE_CONFIG_DIR')
    cs = cswap_bin()
    started = None
    if not effective_config_dir:
        started_state = auth_status()
        started = started_state.get('email') if started_state.get('loggedIn') else None
        if not started:
            print('\n错误：无法从 claude auth status 记录开始身份；拒绝启动，避免无法恢复。', file=sys.stderr)
            return 1
        if not cs:
            print('\n错误：找不到 cswap，无法在结束时恢复开始身份。', file=sys.stderr)
            return 1
        if started.casefold() not in managed_emails:
            print('\n错误：开始身份 %s 不在 cswap 托管列表中，无法保证恢复；拒绝启动。' % started, file=sys.stderr)
            return 1
        print('\n警告：接下来会在串行入库期间改变当前 Claude 账号；全部结束后将用 switch_and_verify 恢复并复核为 %s。' % started)
    failed = 0
    restore_failed = False
    run_error = None
    cdp_context = {}
    try:
        for index, row in enumerate(attempts, 1):
            print('\n[%d/%d] %s — %s' % (index, len(attempts), row['dir'], row['account']))
            if row['prerequisite'] is not True:
                print('  自动化前置条件缺失：本轮会启动流程，但需要人工点击授权。')
            old_config = os.environ.get('CLAUDE_CONFIG_DIR')
            old_manual = os.environ.get('CCPICK_AUTOPILOT_ALLOW_MANUAL')
            try:
                if effective_config_dir:
                    os.environ['CLAUDE_CONFIG_DIR'] = effective_config_dir
                if row['prerequisite'] is not True and ns.include_gateless:
                    os.environ['CCPICK_AUTOPILOT_ALLOW_MANUAL'] = '1'
                auto_ok, detail = ccpick_auto.autopilot(row['dir'], row['account'], ns.timeout, headless=ns.headless, user_agent=ns.user_agent, cdp_context=cdp_context)
                if '[interrupted]' in detail:
                    raise KeyboardInterrupt
            except Exception as e:
                auto_ok, detail = (False, 'autopilot 抛出 %s: %s' % (type(e).__name__, _redact_auth_diagnostic(e)))
            finally:
                if old_config is None:
                    os.environ.pop('CLAUDE_CONFIG_DIR', None)
                else:
                    os.environ['CLAUDE_CONFIG_DIR'] = old_config
                if old_manual is None:
                    os.environ.pop('CCPICK_AUTOPILOT_ALLOW_MANUAL', None)
                else:
                    os.environ['CCPICK_AUTOPILOT_ALLOW_MANUAL'] = old_manual
            status = auth_status(effective_config_dir)
            after = status.get('email')
            login_ok = auto_ok and (bool(status.get('loggedIn')) and after == row['account'])
            outcome = ''
            if login_ok:
                if not ns.no_add and (not effective_config_dir):
                    try:
                        added = subprocess.run([cs, 'add'], capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
                        if added.returncode != 0:
                            outcome = 'failed：cswap add 失败（rc=%d）' % added.returncode
                    except Exception as e:
                        outcome = 'failed：cswap add 异常（%s）' % type(e).__name__
                if not outcome:
                    suffix = '（未执行 cswap add）' if ns.no_add or effective_config_dir else ''
                    outcome = 'enrolled%s ｜ %s' % (suffix, _autopilot_evidence(detail))
            else:
                outcome = 'failed：%s；claude auth status=%r' % (_failure_outcome(detail), after)
            if outcome.startswith('failed：'):
                failed += 1
            print('  %s' % outcome)
            results_by_dir[row['dir']] = {'dir': row['dir'], 'account': row['account'], 'outcome': outcome, 'attempted': True, 'autopilot_ok': auto_ok}
    except KeyboardInterrupt:
        run_error = '批处理被中断'
        print('\n错误：批处理被中断；仍会先恢复开始身份。', file=sys.stderr)
    except Exception as e:
        run_error = '批处理异常（%s）' % type(e).__name__
        print('\n错误：%s；仍会先恢复开始身份。' % run_error, file=sys.stderr)
    finally:
        try:
            from ccpick_cdp import close_batch_context
            close_batch_context(cdp_context)
        except BaseException as e:
            restore_failed = True
            if isinstance(e, KeyboardInterrupt):
                run_error = '批处理在关闭 CDP Chrome 时被中断'
            print('\n！！！关闭批处理 CDP Chrome 失败（%s）！！！' % type(e).__name__, file=sys.stderr)
        if not effective_config_dir:
            try:
                restored, restore_detail = ccpick_auto.switch_and_verify(cs, started)
            except Exception as e:
                restored = False
                restore_detail = 'switch_and_verify 异常（%s）' % type(e).__name__
            if restored:
                print('\n已恢复开始身份：%s（%s）' % (started, restore_detail))
            else:
                restore_failed = True
                print('\n！！！恢复开始身份失败：目标 %s；%s ！！！' % (started, restore_detail), file=sys.stderr)
    final_accounts, final_accounts_error = _managed_account_rows()
    if run_error:
        for row in attempts:
            if row['dir'] not in results_by_dir:
                results_by_dir[row['dir']] = {'dir': row['dir'], 'account': row['account'], 'outcome': 'failed：%s，未完成' % run_error, 'attempted': True}
    results = [results_by_dir[row['dir']] for row in preflight if row['dir'] in results_by_dir]
    _render_auto_enroll_summary(results, final_accounts, final_accounts_error)
    return auto_enroll_batch_exit_code(len(attempts), failed, restore_failed or run_error is not None or final_accounts_error is not None)

def cmd_enroll_all(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick enroll-all')
    ap.add_argument('--profiles', help='逗号分隔的配置文件目录名；不给则弹窗多选')
    ap.add_argument('--config-dir', help='隔离用的 CLAUDE_CONFIG_DIR（演练用，不动主凭据）')
    ap.add_argument('--no-add', action='store_true', help='只登录，不跑 cswap add')
    ap.add_argument('--timeout', type=int, default=CODE_WAIT_S, help='每个配置文件等你点授权的秒数（默认 %d）' % CODE_WAIT_S)
    ns = ap.parse_args(args)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ccpick import list_profiles
    profiles = list_profiles()
    by_dir = {p['dir']: p for p in profiles}
    if ns.profiles:
        dirs = [d.strip() for d in ns.profiles.split(',') if d.strip()]
        bad = [d for d in dirs if d not in by_dir]
        if bad:
            print('没有这些配置文件: %s' % ', '.join(bad), file=sys.stderr)
            return 2
    else:
        dirs = pick_many(profiles)
        if not dirs:
            print('已取消。')
            return 130
    cs = cswap_bin()
    started = auth_status(ns.config_dir).get('email')
    print('=' * 72)
    print('批量入库 %d 个配置文件。开始前的身份: %s' % (len(dirs), started or '(未登录)'))
    if not ns.config_dir and started and cs:
        print('提醒：会真的切换登录身份。全部跑完后用 cswap switch 切回 %s' % started)
    print('=' * 72)
    results = []
    for i, d in enumerate(dirs, 1):
        p = by_dir[d]
        email = p['account'] or None
        print()
        print('[%d/%d] %s — %s' % (i, len(dirs), p['name'], email or '(无 Google 账号)'))
        print('       >>> 授权页会开在这个配置文件的 Chrome 窗口里，去点一下「授权」<<<')
        res = enroll_one(d, email, _runtime.data_dir() / '.code-inbox', ns.config_dir, wait_s=ns.timeout)
        res['profile'] = d
        res['profile_name'] = p['name']
        if res.get('ok') and (not ns.no_add) and (not ns.config_dir) and cs:
            r = subprocess.run([cs, 'add'], capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
            res['cswap_add_rc'] = r.returncode
            res['cswap_add_out'] = (r.stdout or r.stderr or '').strip()[-300:]
            print('       cswap add -> rc=%d' % r.returncode)
        results.append(res)
        print('       %s  %s' % ('成功' if res.get('ok') else '失败', res.get('email_after') or res.get('reason', '')))
    print()
    print('=' * 72)
    ok = [r for r in results if r.get('ok')]
    print('完成: %d / %d 成功' % (len(ok), len(results)))
    for r in results:
        print('  %-11s %-14s %s' % (r['profile'], '成功' if r.get('ok') else '失败', r.get('email_after') or r.get('reason', '')))
    if started and (not ns.config_dir):
        now_email = auth_status().get('email')
        print()
        print('当前身份: %s（开始时是 %s）' % (now_email, started))
        if now_email != started and cs:
            print('切回去:  cswap switch %s' % started)
    print(json.dumps(results, ensure_ascii=False))
    return 0 if len(ok) == len(results) else 1

def cmd_enroll(args: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ccpick enroll')
    ap.add_argument('--profile', help='Chrome 配置文件目录名，如 Default 或 "Profile 4"')
    ap.add_argument('--email', help='预填邮箱（成为 authorize 的 login_hint）')
    ap.add_argument('--code-file', help='code 投递文件路径')
    ap.add_argument('--config-dir', help='隔离用的 CLAUDE_CONFIG_DIR')
    ap.add_argument('--add', action='store_true', help='登录成功后跑 cswap add 入库')
    ap.add_argument('--slot', help='cswap add 的槽位号')
    ap.add_argument('--timeout', type=int, default=CODE_WAIT_S, help='等你点授权的秒数（默认 %d）' % CODE_WAIT_S)
    ap.add_argument('--no-restore', action='store_true', help='入库后即使新账号额度已满也不切回（默认会切回可用账号）')
    ns = ap.parse_args(args)
    if ns.config_dir and ns.add:
        ap.error('--config-dir cannot be combined with --add; isolated login must not change the default account store')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ccpick import list_profiles, pick_profile, PICK_CANCEL, PICK_DEFAULT
    profiles = list_profiles()
    by_dir = {p['dir']: p for p in profiles}
    prof = ns.profile
    if not prof:
        choice = pick_profile(profiles)
        if choice is PICK_CANCEL or choice == PICK_DEFAULT:
            print('已取消。')
            return 130
        prof = choice
    if prof not in by_dir:
        print('没有这个配置文件: %r' % (prof,), file=sys.stderr)
        print('可用: ' + ', '.join(sorted(by_dir)), file=sys.stderr)
        return 2
    email = ns.email or by_dir[prof]['account'] or None
    code_file = Path(ns.code_file) if ns.code_file else _runtime.data_dir() / '.code-inbox'
    res = enroll_one(prof, email, code_file, ns.config_dir, wait_s=ns.timeout)
    print(json.dumps(res, ensure_ascii=False))
    if not res.get('ok'):
        return 1
    if ns.add:
        cs = cswap_bin()
        if not cs:
            print('[warn] 找不到 cswap，跳过入库', file=sys.stderr)
            return 0
        argv = [cs, 'add']
        if ns.slot:
            argv += ['--slot', str(ns.slot)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
        print(r.stdout or r.stderr)
        if r.returncode != 0:
            return r.returncode
    if not ns.config_dir and (not ns.no_restore):
        after = res.get('email_after')
        if after:
            ok, why = refresh_and_check_quota(after)
            print()
            print('[额度] %s: %s' % (after, why))
            if ok is None:
                print('[额度] 判不出来就不动 —— 保持在 %s 上（刚入库的账号还没有用量缓存是正常的）' % after)
            if ok is False:
                back, bwhy = best_account(exclude=after)
                if back:
                    cs = cswap_bin()
                    if cs:
                        print('[额度] 该账号现在不可用，切回 %s (%s)' % (back, bwhy))
                        rr = subprocess.run([cs, 'switch', back], capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
                        print((rr.stdout or rr.stderr or '').strip()[-200:])
                else:
                    print('[额度] 警告：没有其它可用账号可切回 —— 当前身份用不了')
    return 0
