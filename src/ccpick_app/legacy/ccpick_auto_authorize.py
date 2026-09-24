from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from ccpick import JS_GATE_REMEDIATION, OSASCRIPT, _applescript_str, _js_gate_remediation, _probe_chrome_applescript, _probe_chrome_js_gate, launcher_path, list_profiles, validate_launcher
FLOW_HOSTS = {'claude.ai', 'www.claude.ai', 'claude.com', 'www.claude.com', 'platform.claude.com', 'console.anthropic.com', 'accounts.google.com'}

class AppleEventsJavaScriptDisabled(RuntimeError):
    pass
_SENSITIVE_AUTH_RE = re.compile('oauth/authorize|code_challenge\\s*=|state[\\"\']?\\s*[:=]|\\b(?:authorization[\\s_-]*|auth[\\s_-]*)?code[\\"\']?\\s*[:=]', re.IGNORECASE)

def contains_sensitive_auth_material(value: object) -> bool:
    return bool(_SENSITIVE_AUTH_RE.search(str(value or '')))

def classify_flow_url(url: str) -> dict:
    try:
        u = urlparse(url)
        host = (u.hostname or '').lower().rstrip('.')
        port = u.port
    except (TypeError, ValueError):
        return {'kind': 'other'}
    if u.username is not None or u.password is not None:
        return {'kind': 'other'}
    if u.scheme == 'http' and port is not None and (host in ('localhost', '127.0.0.1')) and (u.path.rstrip('/') == '/callback'):
        q = parse_qs(u.query, keep_blank_values=True)
        return {'kind': 'callback', 'error': (q.get('error') or [''])[0], 'error_description': (q.get('error_description') or [''])[0]}
    if u.scheme != 'https' or port not in (None, 443) or host not in FLOW_HOSTS:
        return {'kind': 'other'}
    if host == 'accounts.google.com':
        return {'kind': 'google'}
    return {'kind': 'claude'}

def decide_action(observation: dict, tries: dict, max_attempts: int) -> str | None:
    stage = observation.get('stage')
    action = {'signin': 'signin', 'google_choose': 'google', 'google_confirm': 'confirm', 'authorize': 'authorize'}.get(stage)
    if not action or observation.get('actionable') is not True:
        return None
    return action if tries.get(action, 0) < max_attempts else None

def callback_verdict(observation: dict, click_count: int, saw_authorize: bool) -> tuple[str, str]:

    def safe(value, fallback=''):
        text = ' '.join(str(value or fallback).split())[:240]
        if contains_sensitive_auth_material(text):
            return '[redacted]'
        return text
    error = safe(observation.get('error'), 'unknown')
    desc = safe(observation.get('error_description'))
    summary = 'error=%s%s' % (error, ' description=' + desc if desc else '')
    if click_count == 0 and (not saw_authorize):
        return ('account', summary)
    return ('flow', summary)

def _js_inspector(email: str) -> str:
    email_js = json.dumps(email, ensure_ascii=False)
    return '(() => {\n  const norm = s => (s || \'\').replace(/\\s+/g, \' \').trim();\n  const u = new URL(location.href);\n  const host = u.hostname.toLowerCase().replace(/\\.$/, \'\');\n  const path = u.pathname.replace(/\\/$/, \'\');\n  const isClaude = [\'claude.ai\',\'www.claude.ai\',\'claude.com\',\'www.claude.com\',\n    \'platform.claude.com\',\'console.anthropic.com\'].includes(host);\n  const clickable = [...document.querySelectorAll(\'button, a, [role="button"], [role="link"], [data-identifier]\')];\n  const enabled = e => !!e && !e.disabled && e.getAttribute(\'aria-disabled\') !== \'true\';\n  // ★不要用 enabled 过滤"目标在不在"★ —— Chrome 对后台窗口做渲染节流, 主按钮会停在\n  // disabled=true(实测: Authorize disabled=true 而同页 Decline disabled=false;\n  // 把该窗口抬到前台后立刻变 false)。用 enabled 过滤会让状态机判成"没有目标"而永远\n  // 轮询、一次都不尝试 —— 与 Windows 那次"48 条点了却一次没点中"同源。\n  // 所以: 存在即报 actionable, 另外用 throttled 标出"当前禁用", 点击前由调用方抬前台。\n  const exact = labels => clickable.find(e => labels.includes(norm(e.innerText || e.textContent)));\n  if (u.protocol === \'http:\' && (host === \'localhost\' || host === \'127.0.0.1\') && path === \'/callback\') {\n    const error = u.searchParams.get(\'error\') || \'\';\n    return JSON.stringify({stage: error ? \'callback_error\' : \'callback_success\', actionable:false,\n      error, error_description:u.searchParams.get(\'error_description\') || \'\'});\n  }\n  if (host === \'accounts.google.com\') {\n    const targetEmail = ' + email_js + ";\n    const body = norm(document.body && document.body.innerText).toLowerCase();\n    // Google 的账号选择器可属于任何网站；页面没有 Claude 证据时绝不碰。\n    if (!body.includes('claude')) return JSON.stringify({stage:'irrelevant', actionable:false});\n    const target = clickable.find(e => ((e.getAttribute('data-identifier') || '') === targetEmail ||\n      norm(e.innerText || e.textContent).includes(targetEmail)));\n    if (target) return JSON.stringify({stage:'google_choose', actionable:true, throttled:!enabled(target)});\n    const confirm = exact(['Continue', '继续', 'Confirm', '确认']);\n    if (confirm && body.includes('claude')) return JSON.stringify({stage:'google_confirm', actionable:true, throttled:!enabled(confirm)});\n    return JSON.stringify({stage:'google_wait', actionable:false});\n  }\n  const oauthPath = path === '/oauth/authorize' || path === '/cai/oauth/authorize';\n  const authorize = exact(['Authorize', '授权']);\n  if (isClaude && oauthPath && authorize) return JSON.stringify({stage:'authorize', actionable:true, throttled:!enabled(authorize)});\n  const signin = exact(['Continue with Google', '使用 Google 账号继续', '继续使用 Google']);\n  if (isClaude && signin) return JSON.stringify({stage:'signin', actionable:true, throttled:!enabled(signin)});\n  return JSON.stringify({stage:(isClaude && oauthPath) ? 'claude_wait' : 'irrelevant', actionable:false});\n})()"

def _candidate_predicate_applescript() -> str:
    return 'on isCandidateURL(u)\n    return (u starts with "https://accounts.google.com/") or ¬\n        (u starts with "https://claude.ai/") or ¬\n        (u starts with "https://www.claude.ai/") or ¬\n        (u starts with "https://claude.com/") or ¬\n        (u starts with "https://www.claude.com/") or ¬\n        (u starts with "https://platform.claude.com/") or ¬\n        (u starts with "https://console.anthropic.com/") or ¬\n        (u starts with "http://localhost:") or ¬\n        (u starts with "http://127.0.0.1:")\nend isCandidateURL\n'

def _run_script(script: str, timeout: int=20) -> subprocess.CompletedProcess:
    if not Path(OSASCRIPT).is_file():
        raise RuntimeError('找不到 /usr/bin/osascript')
    return subprocess.run([OSASCRIPT, '-'], input=script, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')

def chrome_window_ids() -> set[int]:
    script = 'set outText to ""\ntell application "Google Chrome"\n    repeat with w in windows\n        set outText to outText & (id of w as text) & linefeed\n    end repeat\nend tell\nreturn outText\n'
    r = _run_script(script)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or 'AppleScript 失败').strip())
    try:
        return {int(line) for line in r.stdout.splitlines() if line.strip()}
    except ValueError as e:
        raise RuntimeError('Chrome window id 无法解析') from e

def _raise_gate(error_text: str) -> None:
    if 'Executing JavaScript through AppleScript is turned off' in error_text:
        raise AppleEventsJavaScriptDisabled(JS_GATE_REMEDIATION)

def scan_flow_tabs(email: str, exclude_window_ids: set[int] | None=None) -> list[dict]:
    inspector = _applescript_str(_js_inspector(email))
    excluded = '{' + ', '.join((str(i) for i in sorted(exclude_window_ids or set()))) + '}'
    script = _candidate_predicate_applescript() + f'-- ★AppleScript 的 tab 常量在 tell application "Google Chrome" 块内会被\n-- Chrome 字典的 tab(标签页)类遮蔽，拼出的是字面量 "tab" 而不是制表符。\n-- 所以分隔符一律在块外求值成 SEP 再用。实测: 块内 "A"&tab&"B" -> A t a b B。\nset SEP to tab\nset outText to ""\nset excludedWindowIDs to {excluded}\ntry\n    tell application "Google Chrome"\n        repeat with wi from 1 to (count of windows)\n            set w to window wi\n            set wid to id of w\n            if wid is not in excludedWindowIDs then\n                repeat with ti from 1 to (count of tabs of w)\n                    set tabURL to URL of tab ti of w\n                    if my isCandidateURL(tabURL) then\n                        try\n                            set payload to execute tab ti of w javascript {inspector}\n                            set outText to outText & (wid as text) & SEP & (ti as text) & SEP & payload & linefeed\n                        on error errMsg number errNum\n                            -- 开关按 profile 保存；别让别的 profile 的关闭状态阻断目标流程。\n                            if errMsg does not contain "Executing JavaScript through AppleScript is turned off" then\n                                return "__CCPICK_ERROR__" & SEP & (errNum as text) & SEP & errMsg\n                            end if\n                        end try\n                    end if\n                end repeat\n            end if\n        end repeat\n    end tell\n    return outText\non error errMsg number errNum\n    return "__CCPICK_ERROR__" & SEP & (errNum as text) & SEP & errMsg\nend try\n'
    r = _run_script(script)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or 'AppleScript 失败').strip()
        _raise_gate(err)
        raise RuntimeError(err)
    out = r.stdout.strip()
    if out.startswith('__CCPICK_ERROR__\t'):
        _raise_gate(out)
        raise RuntimeError(out.split('\t', 2)[-1])
    observations = []
    for line in out.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3:
            continue
        try:
            payload = json.loads(parts[2])
            payload['window_id'] = int(parts[0])
            payload['tab_index'] = int(parts[1])
            observations.append(payload)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return observations

def _click_js(action: str, email: str) -> str:
    action_js = json.dumps(action)
    email_js = json.dumps(email, ensure_ascii=False)
    return '(() => {\n  const action = ' + action_js + ';\n  const email = ' + email_js + ';\n  const norm = s => (s || \'\').replace(/\\s+/g, \' \').trim();\n  const u = new URL(location.href);\n  const host = u.hostname.toLowerCase().replace(/\\.$/, \'\');\n  const path = u.pathname.replace(/\\/$/, \'\');\n  const isClaude = [\'claude.ai\',\'www.claude.ai\',\'claude.com\',\'www.claude.com\',\n    \'platform.claude.com\',\'console.anthropic.com\'].includes(host);\n  const isOAuth = path === \'/oauth/authorize\' || path === \'/cai/oauth/authorize\';\n  const body = norm(document.body && document.body.innerText).toLowerCase();\n  const nodes = [...document.querySelectorAll(\'button, a, [role="button"], [role="link"], [data-identifier]\')];\n  const enabled = e => !!e && !e.disabled && e.getAttribute(\'aria-disabled\') !== \'true\';\n  // 同 inspector: 先按文案找到目标(不看 enabled), 禁用与否单独报, 让调用方能重试。\n  const exact = labels => nodes.find(e => labels.includes(norm(e.innerText || e.textContent)));\n  let el = null;\n  if (action === \'signin\' && isClaude)\n    el = exact([\'Continue with Google\', \'使用 Google 账号继续\', \'继续使用 Google\']);\n  if (action === \'google\' && host === \'accounts.google.com\' && body.includes(\'claude\'))\n    el = nodes.find(e => ((e.getAttribute(\'data-identifier\') || \'\') === email ||\n    norm(e.innerText || e.textContent).includes(email)));\n  if (action === \'confirm\' && host === \'accounts.google.com\' && body.includes(\'claude\'))\n    el = exact([\'Continue\', \'继续\', \'Confirm\', \'确认\']);\n  if (action === \'authorize\' && isClaude && isOAuth) el = exact([\'Authorize\', \'授权\']);\n  if (!el) return JSON.stringify({clicked:false, reason:\'target-not-found\'});\n  // 禁用态点了是 no-op, 必须报成可重试的独立原因, 不能和"找不到"混为一谈 ——\n  // 混了就会在"其实只是被节流"时误判为页面不对而放弃。\n  if (!enabled(el)) return JSON.stringify({clicked:false, reason:\'target-disabled\'});\n  el.click();\n  return JSON.stringify({clicked:true});\n})()'

def execute_action(observation: dict, action: str, email: str) -> bool:
    wid = int(observation['window_id'])
    ti = int(observation['tab_index'])
    js = _applescript_str(_click_js(action, email))
    script = f'-- ★AppleScript 的 tab 常量在 tell application "Google Chrome" 块内会被\n-- Chrome 字典的 tab(标签页)类遮蔽，拼出的是字面量 "tab" 而不是制表符。\n-- 所以分隔符一律在块外求值成 SEP 再用。实测: 块内 "A"&tab&"B" -> A t a b B。\nset SEP to tab\ntry\n    tell application "Google Chrome"\n        set w to first window whose id is {wid}\n        set active tab index of w to {ti}\n        set index of w to 1\n        activate\n        -- 抬前台后要给渲染解除节流的时间。实测 0.2s 不够(按钮仍 disabled), 1.5s 够;\n        -- Windows 那版用的是 1200ms。取 1.2s 并由调用方按 target-disabled 重试兜底。\n        delay 1.2\n        return execute tab {ti} of w javascript {js}\n    end tell\non error errMsg number errNum\n    return "__CCPICK_ERROR__" & SEP & (errNum as text) & SEP & errMsg\nend try\n'
    r = _run_script(script)
    out = (r.stdout or r.stderr or '').strip()
    _raise_gate(out)
    if r.returncode != 0 or out.startswith('__CCPICK_ERROR__\t'):
        return False
    try:
        return json.loads(out).get('clicked') is True
    except json.JSONDecodeError:
        return False

def close_stale_tab(observation: dict, email: str) -> bool:
    safe = {'signin', 'authorize', 'callback_error', 'callback_success', 'google_choose', 'google_confirm'}
    stage = observation.get('stage')
    if stage not in safe:
        return False
    wid = int(observation['window_id'])
    ti = int(observation['tab_index'])
    inspector = _applescript_str(_js_inspector(email))
    expected = _applescript_str('"stage":"%s"' % stage)
    script = _candidate_predicate_applescript() + f'-- ★AppleScript 的 tab 常量在 tell application "Google Chrome" 块内会被\n-- Chrome 字典的 tab(标签页)类遮蔽，拼出的是字面量 "tab" 而不是制表符。\n-- 所以分隔符一律在块外求值成 SEP 再用。实测: 块内 "A"&tab&"B" -> A t a b B。\nset SEP to tab\ntry\n    tell application "Google Chrome"\n        set w to first window whose id is {wid}\n        if {ti} > (count of tabs of w) then return "skipped"\n        set t to tab {ti} of w\n        if not my isCandidateURL(URL of t) then return "skipped"\n        set payload to execute t javascript {inspector}\n        if payload does not contain {expected} then return "skipped"\n        close t\n        return "closed"\n    end tell\non error errMsg number errNum\n    return "__CCPICK_ERROR__" & SEP & (errNum as text) & SEP & errMsg\nend try\n'
    r = _run_script(script)
    out = (r.stdout or r.stderr or '').strip()
    _raise_gate(out)
    return r.returncode == 0 and out == 'closed'

def close_confirmed_flow_tabs(observations: list[dict], email: str) -> int:
    closed = 0
    for observation in sorted(observations, key=lambda x: (x['window_id'], x['tab_index']), reverse=True):
        if close_stale_tab(observation, email):
            closed += 1
    return closed

def redact_child_line(line: str) -> str:
    if contains_sensitive_auth_material(line):
        return '(授权 URL 已发往浏览器，按规矩不回显)'
    return line.rstrip('\n')

def _pump(stream, output: list[str], done: threading.Event) -> None:
    try:
        for line in iter(stream.readline, ''):
            output.append(redact_child_line(line))
    finally:
        done.set()

def _terminate_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

def cmd_auto_enroll(args: list[str]) -> int:
    if sys.platform != 'darwin':
        print('ccpick auto-enroll 目前只用于 macOS；Windows 请继续用 auto_authorize.ps1。', file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog='ccpick auto-enroll')
    ap.add_argument('--profile', required=True, help='Chrome profile 目录名，如 Profile 4')
    ap.add_argument('--email', required=True)
    ap.add_argument('--timeout', type=int, default=240)
    ap.add_argument('--no-add', action='store_true')
    ap.add_argument('--config-dir', help='隔离演练用 CLAUDE_CONFIG_DIR')
    ap.add_argument('--max-attempts', type=int, default=6)
    ns = ap.parse_args(args)
    if ns.timeout <= 0 or ns.max_attempts <= 0:
        print('--timeout 和 --max-attempts 必须为正数。', file=sys.stderr)
        return 2
    if any((ord(c) < 32 or ord(c) == 127 for c in ns.email)):
        print('email 含控制字符，拒绝。', file=sys.stderr)
        return 2
    profile_rows = list_profiles()
    profiles = {p['dir']: p for p in profile_rows}
    if ns.profile not in profiles:
        print('没有这个 Chrome profile: %r' % ns.profile, file=sys.stderr)
        return 2
    lp = launcher_path()
    lp_ok, lp_detail = validate_launcher(lp)
    if not lp_ok:
        print('ccpick launcher 不可用: %s' % lp_detail, file=sys.stderr)
        return 2
    chrome_ok, chrome_detail, _windows = _probe_chrome_applescript()
    if not chrome_ok:
        print('Chrome AppleScript 不可用: %s' % chrome_detail, file=sys.stderr)
        return 2
    js_ok, js_detail = _probe_chrome_js_gate(ns.profile)
    if js_ok is not True:
        print(_js_gate_remediation(ns.profile) if js_ok is False else js_detail, file=sys.stderr)
        return 2
    try:
        enabled_profiles = [p['dir'] for p in profile_rows if _probe_chrome_js_gate(p['dir'])[0] is True]
        if enabled_profiles == [ns.profile]:
            stale = scan_flow_tabs(ns.email)
            closed = close_confirmed_flow_tabs(stale, ns.email)
            if closed:
                print('[auto] 清理了 %d 个经 URL+DOM 双重确认的旧 OAuth tab' % closed)
        else:
            print('[auto] 多个 profile 已打开 Apple Events JS；跳过无法归属的旧 tab 清理')
        initial_window_ids = chrome_window_ids()
    except AppleEventsJavaScriptDisabled:
        print(_js_gate_remediation(ns.profile), file=sys.stderr)
        return 2
    except Exception as e:
        print('旧流程 tab 扫描失败，未启动 enroll: %s' % type(e).__name__, file=sys.stderr)
        return 2
    enroll_args = [lp, 'enroll', '--profile', ns.profile, '--email', ns.email, '--timeout', str(ns.timeout)]
    if not ns.no_add:
        enroll_args.append('--add')
    if ns.config_dir:
        enroll_args += ['--config-dir', ns.config_dir]
    print('[auto] 起 enroll: profile=%s email=%s' % (ns.profile, ns.email))
    proc = subprocess.Popen(enroll_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', start_new_session=True)
    output: list[str] = []
    done = threading.Event()
    threading.Thread(target=_pump, args=(proc.stdout, output, done), daemon=True).start()
    tries = {'signin': 0, 'google': 0, 'confirm': 0, 'authorize': 0}
    clicked_total = 0
    saw_authorize = False
    authorize_clicked = False
    callback_problem: tuple[str, str] | None = None
    deadline = time.monotonic() + ns.timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            observations = scan_flow_tabs(ns.email, exclude_window_ids=initial_window_ids)
        except AppleEventsJavaScriptDisabled:
            print(_js_gate_remediation(ns.profile), file=sys.stderr)
            _terminate_group(proc)
            return 2
        except Exception as e:
            print('[auto] AppleScript 扫描失败(将重试): %s: %s' % (type(e).__name__, str(e)[:200]), file=sys.stderr)
            time.sleep(1)
            continue
        errors = [o for o in observations if o.get('stage') == 'callback_error']
        if errors:
            callback_problem = callback_verdict(errors[0], clicked_total, saw_authorize)
            break
        if any((o.get('stage') == 'authorize' for o in observations)):
            saw_authorize = True
        if authorize_clicked:
            time.sleep(1)
            continue
        acted = False
        priority = {'authorize': 0, 'google_confirm': 1, 'google_choose': 2, 'signin': 3}
        for obs in sorted(observations, key=lambda o: priority.get(o.get('stage'), 9)):
            action = decide_action(obs, tries, ns.max_attempts)
            if not action:
                continue
            try:
                clicked = execute_action(obs, action, ns.email)
            except Exception as e:
                print('[auto] %s 执行失败，未计 attempt: %s' % (action, type(e).__name__), file=sys.stderr)
                continue
            if clicked:
                tries[action] += 1
                clicked_total += 1
                print('[auto] %s：已执行第 %d 次' % (action, tries[action]))
                if action == 'authorize':
                    authorize_clicked = True
                    print('[auto] 已点 Authorize，等待 enroll 自行收到 localhost 回调并退出…')
                    deadline = max(deadline, time.monotonic() + 120)
                acted = True
                break
        time.sleep(2 if acted else 1)
    if callback_problem is None:
        try:
            final_observations = scan_flow_tabs(ns.email, exclude_window_ids=initial_window_ids)
            final_errors = [o for o in final_observations if o.get('stage') == 'callback_error']
            if final_errors:
                callback_problem = callback_verdict(final_errors[0], clicked_total, saw_authorize)
        except Exception:
            pass
    if callback_problem:
        verdict, detail = callback_problem
        if verdict == 'account':
            print('[auto] 账号侧直接拒绝（零点击、未出现授权页）：%s' % detail, file=sys.stderr)
        else:
            print('[auto] 点击后收到错误回调，按流程故障处理，不写账号判定：%s' % detail, file=sys.stderr)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate_group(proc)
    elif proc.poll() is None:
        print('[auto] 超时；enroll 尚未自行结束，终止本轮进程组。', file=sys.stderr)
        _terminate_group(proc)
    done.wait(timeout=5)
    print('--- enroll 输出 ---')
    for line in output:
        print('  ' + line)
    print('[auto] 阶段计数: signin=%d google=%d confirm=%d authorize=%d' % (tries['signin'], tries['google'], tries['confirm'], tries['authorize']))
    try:
        current_flow = scan_flow_tabs(ns.email, exclude_window_ids=initial_window_ids)
        closed = close_confirmed_flow_tabs(current_flow, ns.email)
        if closed:
            print('[auto] 收尾清理了 %d 个本轮 OAuth tab' % closed)
    except Exception as e:
        print('[auto] 收尾 tab 清理失败（不改账号判定）：%s' % type(e).__name__, file=sys.stderr)
    if callback_problem:
        return 3 if callback_problem[0] == 'account' else 4
    return proc.returncode if proc.returncode is not None else 1
if __name__ == '__main__':
    raise SystemExit(cmd_auto_enroll(sys.argv[1:]))
