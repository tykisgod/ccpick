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
            rr = subprocess.run([cs, 'list', '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
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
        out = subprocess.run([cb, 'auth', 'status'], capture_output=True, text=True, timeout=60, env=env).stdout
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
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
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
                        rr = subprocess.run([cs, 'switch', back], capture_output=True, text=True, timeout=180)
                        print((rr.stdout or rr.stderr or '').strip()[-200:])
                else:
                    print('[额度] 警告：没有其它可用账号可切回 —— 当前身份用不了')
    return 0
