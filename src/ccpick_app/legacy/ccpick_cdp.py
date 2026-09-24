from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from ccpick_auto_authorize import _click_js, _js_inspector, callback_verdict, contains_sensitive_auth_material, decide_action, redact_child_line
from ccpick_cleanup import chrome_running
ENV_USER_DATA_DIR = 'CCPICK_CHROME_USER_DATA_DIR'
ENV_CDP_ACTIVE = 'CCPICK_CDP_ACTIVE'
_FORBIDDEN_METHODS = {'Network.getAllCookies', 'Network.getCookies', 'Storage.getCookies', 'Storage.getTrustTokens'}
_FLOW_STAGES = {'signin', 'google_choose', 'google_confirm', 'authorize', 'callback_error', 'callback_success'}

class ChromePipeError(RuntimeError):
    pass

class ChromeAlreadyRunning(ChromePipeError):
    pass

class CDPCommandError(ChromePipeError):
    pass

def _safe_text(value: object, limit: int=300) -> str:
    text = ' '.join(str(value or '').split())[:limit]
    if contains_sensitive_auth_material(text):
        return '[redacted]'
    return text

def _valid_profile_name(profile: str) -> bool:
    return bool(profile) and Path(profile).name == profile and (not any((token in profile for token in ('..', '/', '\\')))) and (not any((ord(c) < 32 or ord(c) == 127 for c in profile)))

def _sanitize_event(value):
    if isinstance(value, dict):
        return {key: '[redacted-url]' if key.lower() == 'url' else _sanitize_event(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_event(child) for child in value]
    if isinstance(value, str):
        return _safe_text(value, 1000)
    return value

def _walk_frames(node: dict) -> list[dict]:
    out = []
    if not isinstance(node, dict):
        return out
    frame = node.get('frame')
    if isinstance(frame, dict) and frame.get('id'):
        out.append(frame)
    for child in node.get('childFrames') or []:
        out.extend(_walk_frames(child))
    return out

def _windows_pipe_switch(read_handle: int, write_handle: int) -> str:
    return '--remote-debugging-io-pipes=%d,%d' % (read_handle, write_handle)

class ChromePipe:

    def __init__(self, chrome_binary: str, profile_directory: str, user_data_dir: str | Path | None=None, headless: bool=False, timeout: float=15, user_agent: str | None=None, *, popen_factory=None, running_check=None):
        if not chrome_binary:
            raise ValueError('Chrome 可执行文件路径为空')
        if not _valid_profile_name(profile_directory):
            raise ValueError('Chrome profile 目录名含路径成分或控制字符')
        if timeout <= 0:
            raise ValueError('timeout 必须为正数')
        if user_agent and any((ord(c) < 32 or ord(c) == 127 for c in user_agent)):
            raise ValueError('user-agent 含控制字符')
        explicit_ud = user_data_dir
        if explicit_ud is None:
            explicit_ud = os.environ.get(ENV_USER_DATA_DIR)
        self._pass_user_data_dir = explicit_ud is not None
        if explicit_ud is None:
            from ccpick import chrome_user_data_dir
            discovered = chrome_user_data_dir()
            if discovered is None:
                raise ValueError('找不到 Chrome User Data 目录')
            self.user_data_dir = discovered
        else:
            self.user_data_dir = Path(explicit_ud).expanduser()
        self.chrome_binary = str(chrome_binary)
        self.profile_directory = profile_directory
        self.headless = bool(headless)
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self._popen_factory = popen_factory or subprocess.Popen
        self._running_check = running_check or chrome_running
        self._proc = None
        self._read_fd = None
        self._write_fd = None
        self._reader = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._events = queue.Queue()
        self._next_id = 1
        self._reader_error = None
        self._sessions = {}

    @property
    def process(self):
        return self._proc

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self):
        try:
            self.open()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
        return False

    def _chrome_args(self) -> list[str]:
        args = ['--remote-debugging-pipe', '--profile-directory=%s' % self.profile_directory, '--no-first-run', '--no-default-browser-check']
        if self._pass_user_data_dir:
            args.append('--user-data-dir=%s' % self.user_data_dir)
        if self.headless:
            args.append('--headless=new')
        if self.user_agent:
            args.append('--user-agent=%s' % self.user_agent)
        return args

    def _spawn_posix(self, r_in: int, w_out: int, args: list[str]):
        sh = 'exec "$0" "$@" 3<&%d 4>&%d' % (r_in, w_out)
        return self._popen_factory(['/bin/sh', '-c', sh, self.chrome_binary] + args, pass_fds=(r_in, w_out), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _spawn_windows(self, r_in: int, w_out: int, args: list[str]):
        import msvcrt
        read_handle = int(msvcrt.get_osfhandle(r_in))
        write_handle = int(msvcrt.get_osfhandle(w_out))
        os.set_handle_inheritable(read_handle, True)
        os.set_handle_inheritable(write_handle, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [read_handle, write_handle]}
        try:
            return self._popen_factory([self.chrome_binary] + args + [_windows_pipe_switch(read_handle, write_handle)], close_fds=True, startupinfo=startup, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        finally:
            os.set_handle_inheritable(read_handle, False)
            os.set_handle_inheritable(write_handle, False)

    def open(self) -> None:
        if self._proc is not None:
            raise ChromePipeError('ChromePipe 已经启动')
        if not Path(self.chrome_binary).is_file():
            raise ChromePipeError('找不到 Chrome 可执行文件')
        if not self.user_data_dir.is_dir():
            raise ChromePipeError('Chrome User Data 目录不存在')
        profile_path = self.user_data_dir / self.profile_directory
        if not profile_path.is_dir():
            raise ChromePipeError('Chrome profile 目录不存在: %s' % self.profile_directory)
        running, detail = self._running_check(self.user_data_dir)
        if running is not False:
            if running is True:
                why = 'Chrome 正在使用该 User Data 目录'
            else:
                why = '无法确认该 User Data 目录是否已被 Chrome 使用（%s）' % _safe_text(detail)
            raise ChromeAlreadyRunning('%s；请完全退出 Chrome 后重试（please quit Chrome completely）。ccpick 不会把 CDP pipe 静默交给已有实例。' % why)
        r_in = w_in = r_out = w_out = None
        try:
            r_in, w_in = os.pipe()
            r_out, w_out = os.pipe()
            args = self._chrome_args()
            if os.name == 'nt':
                self._proc = self._spawn_windows(r_in, w_out, args)
            else:
                self._proc = self._spawn_posix(r_in, w_out, args)
            self._write_fd = w_in
            self._read_fd = r_out
            os.close(r_in)
            r_in = None
            os.close(w_out)
            w_out = None
            self._reader = threading.Thread(target=self._reader_loop, name='ccpick-cdp-reader', daemon=True)
            self._reader.start()
            self.send('Browser.getVersion')
        except BaseException:
            for fd in (r_in, w_in if self._write_fd is None else None, r_out if self._read_fd is None else None, w_out):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self.close()
            raise

    def _write_all(self, data: bytes) -> None:
        fd = self._write_fd
        if fd is None:
            raise ChromePipeError('CDP pipe 尚未启动或已关闭')
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ChromePipeError('CDP pipe 写入失败')
            view = view[written:]

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for waiter in pending:
            try:
                waiter.put_nowait(error)
            except queue.Full:
                pass

    def _reader_loop(self) -> None:
        buf = bytearray()
        try:
            while not self._stop.is_set():
                fd = self._read_fd
                if fd is None:
                    break
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise ChromePipeError('Chrome 已关闭 CDP pipe')
                buf.extend(chunk)
                while True:
                    end = buf.find(0)
                    if end < 0:
                        break
                    raw = bytes(buf[:end])
                    del buf[:end + 1]
                    if not raw:
                        continue
                    try:
                        message = json.loads(raw.decode('utf-8'))
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        raise ChromePipeError('CDP pipe 收到无效 JSON') from e
                    if not isinstance(message, dict):
                        raise ChromePipeError('CDP pipe 收到的 JSON 不是对象')
                    mid = message.get('id')
                    if isinstance(mid, int):
                        with self._pending_lock:
                            waiter = self._pending.get(mid)
                        if waiter is not None:
                            waiter.put(message)
                    elif message.get('method'):
                        self._events.put(_sanitize_event(message))
        except BaseException as e:
            if not self._stop.is_set():
                self._reader_error = e
                self._fail_pending(e)

    def _send(self, method: str, params: dict | None=None, session_id: str | None=None, response_timeout: float | None=None) -> dict:
        if method in _FORBIDDEN_METHODS:
            raise CDPCommandError('安全策略拒绝 CDP 方法 %s' % method)
        if not method or any((ord(c) < 32 for c in method)):
            raise ValueError('无效 CDP method')
        if self._reader_error is not None:
            raise ChromePipeError('CDP reader 已失败: %s' % type(self._reader_error).__name__)
        with self._write_lock:
            mid = self._next_id
            self._next_id += 1
            waiter = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._pending[mid] = waiter
            message = {'id': mid, 'method': method}
            if params is not None:
                message['params'] = params
            if session_id is not None:
                message['sessionId'] = session_id
            payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\x00'
            try:
                self._write_all(payload)
            except BaseException:
                with self._pending_lock:
                    self._pending.pop(mid, None)
                raise
        try:
            item = waiter.get(timeout=response_timeout or self.timeout)
        except queue.Empty as e:
            raise ChromePipeError('CDP %s 等响应超时' % method) from e
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)
        if isinstance(item, BaseException):
            raise item
        error = item.get('error')
        if isinstance(error, dict):
            raise CDPCommandError('CDP %s 失败（code=%s）: %s' % (method, error.get('code'), _safe_text(error.get('message') or 'unknown')))
        result = item.get('result')
        return result if isinstance(result, dict) else {}

    def send(self, method: str, params: dict | None=None, session_id: str | None=None) -> dict:
        return self._send(method, params, session_id)

    def poll_event(self, timeout: float=0) -> dict | None:
        try:
            return self._events.get(timeout=max(0, timeout))
        except queue.Empty:
            return None

    def target_ids(self) -> set[str]:
        result = self.send('Target.getTargets')
        return {info.get('targetId') for info in result.get('targetInfos') or [] if isinstance(info, dict) and info.get('targetId') and (info.get('type') in ('page', 'iframe'))}

    def _session_for(self, target_id: str) -> str:
        cached = self._sessions.get(target_id)
        if cached:
            return cached
        result = self.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
        session_id = result.get('sessionId')
        if not session_id:
            raise ChromePipeError('Target.attachToTarget 未返回 sessionId')
        self._sessions[target_id] = session_id
        return session_id

    def frame_contexts(self, only_target_ids: set[str] | None=None) -> list[dict]:
        result = self.send('Target.getTargets')
        infos = [info for info in result.get('targetInfos') or [] if isinstance(info, dict) and info.get('targetId') and (info.get('type') in ('page', 'iframe')) and (only_target_ids is None or info.get('targetId') in only_target_ids)]
        live = {info['targetId'] for info in infos}
        for target_id in list(self._sessions):
            if target_id not in live and (only_target_ids is None or target_id in only_target_ids):
                self._sessions.pop(target_id, None)
        contexts = []
        seen = set()
        for info in infos:
            target_id = info['targetId']
            try:
                session_id = self._session_for(target_id)
                tree = self.send('Page.getFrameTree', session_id=session_id).get('frameTree') or {}
            except ChromePipeError:
                self._sessions.pop(target_id, None)
                continue
            for frame in _walk_frames(tree):
                frame_id = frame.get('id')
                key = (target_id, frame_id)
                if not frame_id or key in seen:
                    continue
                seen.add(key)
                try:
                    world = self.send('Page.createIsolatedWorld', {'frameId': frame_id, 'worldName': 'ccpick-auth', 'grantUniveralAccess': False}, session_id=session_id)
                    context_id = world.get('executionContextId')
                    if context_id is None:
                        continue
                    contexts.append({'target_id': target_id, 'session_id': session_id, 'frame_id': frame_id, 'loader_id': frame.get('loaderId'), 'context_id': context_id})
                except ChromePipeError:
                    continue
        return contexts

    def evaluate(self, expression: str, context: dict) -> dict:
        return self.send('Runtime.evaluate', {'expression': expression, 'contextId': context['context_id'], 'returnByValue': True, 'awaitPromise': True}, session_id=context['session_id'])

    def call_on_global(self, function_declaration: str, context: dict) -> dict:
        return self.send('Runtime.callFunctionOn', {'executionContextId': context['context_id'], 'functionDeclaration': function_declaration, 'returnByValue': True, 'awaitPromise': True}, session_id=context['session_id'])

    def close_target(self, target_id: str) -> bool:
        try:
            result = self.send('Target.closeTarget', {'targetId': target_id})
            self._sessions.pop(target_id, None)
            return result.get('success') is True
        except ChromePipeError:
            return False

    def close(self) -> None:
        proc = self._proc
        if proc is None and self._read_fd is None and (self._write_fd is None):
            return
        interrupted = None
        cleanup_error = None

        def remember_interrupt(error: BaseException) -> None:
            nonlocal interrupted, cleanup_error
            if isinstance(error, KeyboardInterrupt) and interrupted is None:
                interrupted = error
            elif not isinstance(error, KeyboardInterrupt) and cleanup_error is None:
                cleanup_error = error

        def wait_once(timeout: float) -> bool:
            try:
                proc.wait(timeout=timeout)
                return True
            except subprocess.TimeoutExpired:
                return False
            except BaseException as error:
                remember_interrupt(error)
                return False

        def stop_process(hard: bool) -> None:
            try:
                if os.name != 'nt' and getattr(proc, 'pid', None):
                    os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
                elif hard:
                    proc.kill()
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            except BaseException as error:
                remember_interrupt(error)
        try:
            process_running = proc is not None and proc.poll() is None
        except BaseException as error:
            remember_interrupt(error)
            process_running = proc is not None
        if process_running and self._write_fd is not None:
            try:
                self._send('Browser.close', response_timeout=min(2.0, self.timeout))
            except BaseException as error:
                remember_interrupt(error)
        self._stop.set()
        for name in ('_write_fd', '_read_fd'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    remember_interrupt(error)
        reaped = proc is None
        if proc is not None:
            reaped = wait_once(5)
            if not reaped:
                stop_process(False)
                reaped = wait_once(5)
            if not reaped:
                stop_process(True)
                reaped = wait_once(5)
        if self._reader is not None and self._reader is not threading.current_thread():
            try:
                self._reader.join(timeout=2)
            except BaseException as error:
                remember_interrupt(error)
        self._fail_pending(ChromePipeError('CDP pipe 已关闭'))
        self._proc = None if reaped else proc
        self._reader = None
        self._sessions.clear()
        if not reaped:
            error = ChromePipeError('Chrome 子进程在 terminate/kill 后仍未被 wait 回收')
            if cleanup_error is not None:
                raise error from cleanup_error
            if interrupted is not None:
                raise error from interrupted
            raise error
        if interrupted is not None:
            raise interrupted
        if cleanup_error is not None:
            raise ChromePipeError('Chrome 子进程已回收，但收口期间发生 %s' % type(cleanup_error).__name__) from cleanup_error

def backend_status(chrome_path: str | None=None, user_data_dir: str | Path | None=None) -> tuple[bool, str]:
    if chrome_path is None or user_data_dir is None:
        from ccpick import chrome_binary, chrome_user_data_dir
        chrome_path = chrome_path or chrome_binary()
        user_data_dir = user_data_dir or chrome_user_data_dir()
    if not chrome_path or not Path(chrome_path).is_file():
        return (False, 'CDP pipe 不可用：找不到 Chrome 可执行文件')
    if user_data_dir is None or not Path(user_data_dir).is_dir():
        return (False, 'CDP pipe 不可用：找不到 Chrome User Data 目录')
    running, detail = chrome_running(Path(user_data_dir))
    if running is True:
        return (False, 'CDP pipe 需由 ccpick 启动 Chrome；请先完全退出 Chrome（please quit Chrome completely）')
    if running is None:
        return (False, 'CDP pipe 无法确认 Chrome 运行态（%s），为防 singleton 挂起而拒绝' % _safe_text(detail))
    return (True, 'CDP pipe（Chrome 已退出；将由 ccpick 启动，不需要 Apple Events JS gate）')

def _result_value(result: dict):
    remote = result.get('result') if isinstance(result, dict) else None
    if not isinstance(remote, dict) or result.get('exceptionDetails'):
        return None
    return remote.get('value')

def scan_flow_frames(pipe: ChromePipe, email: str, exclude_target_ids: set[str] | None=None, only_target_ids: set[str] | None=None) -> list[dict]:
    observations = []
    eligible = pipe.target_ids()
    if exclude_target_ids:
        eligible.difference_update(exclude_target_ids)
    if only_target_ids is not None:
        eligible.intersection_update(only_target_ids)
    for context in pipe.frame_contexts(eligible):
        try:
            value = _result_value(pipe.evaluate(_js_inspector(email), context))
            payload = json.loads(value) if isinstance(value, str) else None
        except (ChromePipeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.update(context)
            observations.append(payload)
    return observations

def execute_frame_action(pipe: ChromePipe, observation: dict, action: str, email: str) -> tuple[bool, str]:
    declaration = 'function(){return %s;}' % _click_js(action, email)
    try:
        value = _result_value(pipe.call_on_global(declaration, observation))
        result = json.loads(value) if isinstance(value, str) else None
    except (ChromePipeError, TypeError, ValueError, json.JSONDecodeError):
        return (False, 'execution-context-lost')
    if not isinstance(result, dict):
        return (False, 'invalid-result')
    if result.get('clicked') is True:
        return (True, 'clicked')
    reason = result.get('reason')
    return (False, reason if reason in ('target-disabled', 'target-not-found') else 'click-failed')

def close_confirmed_flow_targets(pipe: ChromePipe, email: str, only_target_ids: set[str] | None=None) -> int:
    observations = scan_flow_frames(pipe, email, only_target_ids=only_target_ids)
    targets = {obs['target_id'] for obs in observations if obs.get('stage') in _FLOW_STAGES}
    return sum((1 for target_id in targets if pipe.close_target(target_id)))

def _terminate_enroll(proc) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        if os.name == 'nt':
            from ccpick_enroll import kill_tree
            kill_tree(proc)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

def _enroll_argv(launcher: str, profile: str, email: str, timeout_s: int, config_dir: str | None) -> list[str]:
    argv = [launcher, 'enroll', '--profile', profile, '--email', email, '--timeout', str(timeout_s)]
    if config_dir:
        argv += ['--config-dir', config_dir]
    return argv

def _start_enroll(profile: str, email: str, timeout_s: int, config_dir: str | None):
    from ccpick import launcher_path, validate_launcher
    lp = launcher_path()
    valid, detail = validate_launcher(lp)
    if not valid:
        raise ChromePipeError('ccpick launcher 不可用: %s' % _safe_text(detail))
    argv = _enroll_argv(lp, profile, email, timeout_s, config_dir)
    env = dict(os.environ)
    env[ENV_CDP_ACTIVE] = '1'
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT, 'text': True, 'encoding': 'utf-8', 'errors': 'replace', 'env': env}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(argv, **kwargs)

def _authorize_with_pipe(pipe: ChromePipe, profile: str, email: str, timeout_s: int, max_attempts: int, config_dir: str | None) -> tuple[bool, str]:
    logs = ['[auto] CDP pipe: profile=%s email=%s' % (profile, email)]
    tries = {'signin': 0, 'google': 0, 'confirm': 0, 'authorize': 0}
    clicked_total = 0
    saw_authorize = False
    authorize_clicked = False
    callback_problem = None
    proc = None
    output = []
    done = threading.Event()
    initial_targets = set()
    flow_targets = set()
    not_found = set()
    interrupted = False
    flow_failed = False
    try:
        initial_targets = pipe.target_ids()
        proc = _start_enroll(profile, email, timeout_s, config_dir)

        def pump() -> None:
            try:
                for line in iter(proc.stdout.readline, ''):
                    output.append(redact_child_line(line))
            finally:
                done.set()
        threading.Thread(target=pump, name='ccpick-enroll-output', daemon=True).start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and proc.poll() is None:
            observations = scan_flow_frames(pipe, email, exclude_target_ids=initial_targets)
            flow_targets.update((obs['target_id'] for obs in observations))
            errors = [obs for obs in observations if obs.get('stage') == 'callback_error']
            if errors:
                callback_problem = callback_verdict(errors[0], clicked_total, saw_authorize)
                break
            if any((obs.get('stage') == 'authorize' for obs in observations)):
                saw_authorize = True
            if authorize_clicked:
                time.sleep(0.5)
                continue
            acted = False
            priority = {'authorize': 0, 'google_confirm': 1, 'google_choose': 2, 'signin': 3}
            for obs in sorted(observations, key=lambda value: priority.get(value.get('stage'), 9)):
                action = decide_action(obs, tries, max_attempts)
                if not action:
                    continue
                missing_key = (obs.get('target_id'), obs.get('frame_id'), obs.get('context_id'), action)
                if missing_key in not_found:
                    continue
                clicked, reason = execute_frame_action(pipe, obs, action, email)
                if clicked:
                    tries[action] += 1
                    clicked_total += 1
                    logs.append('[auto] %s：已执行第 %d 次' % (action, tries[action]))
                    if action == 'authorize':
                        authorize_clicked = True
                        logs.append('[auto] 已点 Authorize，等待 enroll 自行收到 localhost 回调并退出…')
                        deadline = max(deadline, time.monotonic() + 120)
                    acted = True
                    break
                if reason == 'target-disabled':
                    logs.append('[auto] %s: target-disabled（本轮不计 attempt，将重试）' % action)
                    acted = True
                    break
                if reason == 'target-not-found':
                    not_found.add(missing_key)
                    logs.append('[auto] %s: target-not-found（该 frame/context 不再重试）' % action)
            time.sleep(0.5 if acted else 0.25)
        if callback_problem is None:
            final = scan_flow_frames(pipe, email, exclude_target_ids=initial_targets)
            flow_targets.update((obs['target_id'] for obs in final))
            final_errors = [obs for obs in final if obs.get('stage') == 'callback_error']
            if final_errors:
                callback_problem = callback_verdict(final_errors[0], clicked_total, saw_authorize)
        if callback_problem:
            verdict, detail = callback_problem
            if verdict == 'account':
                logs.append('[auto] 账号侧直接拒绝（零点击、未出现授权页）：%s' % detail)
            else:
                logs.append('[auto] 点击后收到错误回调，按流程故障处理：%s' % detail)
            if proc.poll() is None:
                _terminate_enroll(proc)
        elif proc.poll() is None:
            logs.append('[auto] 超时；enroll 尚未自行结束，终止本轮进程组。')
            _terminate_enroll(proc)
        else:
            proc.wait(timeout=5)
    except KeyboardInterrupt:
        interrupted = True
        flow_failed = True
        logs.append('[auto] 收到 KeyboardInterrupt，终止本轮进程组。')
        _terminate_enroll(proc)
    except BaseException as e:
        flow_failed = True
        logs.append('[auto] CDP 流程异常: %s: %s' % (type(e).__name__, _safe_text(e)))
        _terminate_enroll(proc)
    finally:
        done.wait(timeout=2)
        if output:
            logs.append('--- enroll 输出 ---')
            logs.extend(('  ' + line for line in output))
        logs.append('[auto] 阶段计数: signin=%d google=%d confirm=%d authorize=%d' % (tries['signin'], tries['google'], tries['confirm'], tries['authorize']))
        if flow_targets:
            try:
                closed = close_confirmed_flow_targets(pipe, email, only_target_ids=flow_targets)
                if closed:
                    logs.append('[auto] 收尾清理了 %d 个本轮 OAuth target' % closed)
            except BaseException as e:
                flow_failed = True
                logs.append('[auto] 收尾 target 清理失败（不改账号判定）: %s' % type(e).__name__)
    detail = '\n'.join(logs)
    if interrupted:
        return (False, '[interrupted] ' + detail)
    if callback_problem:
        prefix = '[account-refusal] ' if callback_problem[0] == 'account' else '[flow-failure] '
        return (False, prefix + detail)
    return (bool(not flow_failed and proc is not None and (proc.returncode == 0)), detail)

def run_authorization(chrome_path: str, profile: str, email: str, timeout_s: int=240, headless: bool=False, user_agent: str | None=None, max_attempts: int=6, config_dir: str | None=None, batch_context: dict | None=None) -> tuple[bool, str]:
    pipe = batch_context.get('pipe') if batch_context is not None else None
    owns_pipe = pipe is None
    started = pipe is not None and pipe.alive
    outcome = None
    try:
        if pipe is None:
            pipe = ChromePipe(chrome_path, profile, headless=headless, user_agent=user_agent, timeout=min(30, max(5, timeout_s)))
            pipe.open()
            started = True
            if batch_context is not None:
                batch_context['pipe'] = pipe
                batch_context['headless'] = headless
                batch_context['user_agent'] = user_agent
                owns_pipe = False
        elif not pipe.alive:
            raise ChromePipeError('批处理 CDP Chrome 已退出')
        elif batch_context.get('headless') != headless or batch_context.get('user_agent') != user_agent:
            raise ChromePipeError('批处理期间不能改变 headless / user-agent')
        outcome = _authorize_with_pipe(pipe, profile, email, timeout_s, max_attempts, config_dir)
    except ChromeAlreadyRunning as e:
        outcome = (False, '[cdp-unavailable] %s\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0' % _safe_text(e))
    except KeyboardInterrupt:
        outcome = (False, '[interrupted] CDP pipe 启动/收口收到 KeyboardInterrupt\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0')
    except (ChromePipeError, OSError, ValueError) as e:
        started = started or (pipe is not None and pipe.process is not None)
        prefix = '[flow-failure]' if started else '[cdp-unavailable]'
        outcome = (False, '%s CDP pipe %s: %s: %s\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0' % (prefix, '流程失败' if started else '启动失败', type(e).__name__, _safe_text(e)))
    except BaseException as e:
        started = started or (pipe is not None and pipe.process is not None)
        outcome = (False, '[flow-failure] CDP pipe 未预期异常: %s: %s\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0' % (type(e).__name__, _safe_text(e)))
    if owns_pipe and pipe is not None:
        try:
            pipe.close()
        except KeyboardInterrupt:
            return (False, '[interrupted] CDP Chrome 收口收到 KeyboardInterrupt\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0')
        except BaseException as e:
            return (False, '[flow-failure] CDP Chrome 收口失败: %s: %s\n[auto] 阶段计数: signin=0 google=0 confirm=0 authorize=0' % (type(e).__name__, _safe_text(e)))
    return outcome or (False, '[flow-failure] CDP pipe 未产生结果')

def close_batch_context(context: dict) -> None:
    pipe = context.get('pipe')

    def clear() -> None:
        context.pop('pipe', None)
        context.pop('headless', None)
        context.pop('user_agent', None)
    if pipe is None:
        clear()
        return
    try:
        pipe.close()
    except KeyboardInterrupt:
        if pipe.process is None:
            clear()
        raise
    except Exception:
        if pipe.process is None:
            clear()
            raise
        try:
            pipe.close()
        except BaseException:
            if pipe.process is None:
                clear()
            raise
    clear()

def cmd_probe_refusal(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog='ccpick_cdp.py --probe-refusal')
    ap.add_argument('--profile', required=True)
    ns = ap.parse_args(args)
    from ccpick import chrome_binary
    exe = chrome_binary()
    if not exe:
        print('找不到 Chrome 可执行文件', file=sys.stderr)
        return 2
    from ccpick import chrome_user_data_dir
    user_data = chrome_user_data_dir()
    running, why = chrome_running(user_data)
    if running is not True:
        print('Chrome 当时未被确认运行（%s）；按规矩不启动它。' % _safe_text(why), file=sys.stderr)
        return 2

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError('running refusal 必须发生在 Popen 前')
    try:
        ChromePipe(exe, ns.profile, popen_factory=forbidden_popen).open()
    except ChromeAlreadyRunning as e:
        print(str(e))
        return 0
    except Exception as e:
        print('未得到预期的 running refusal: %s: %s' % (type(e).__name__, _safe_text(e)), file=sys.stderr)
        return 1
    print('FAIL：ChromePipe 在运行态检查后仍走到了 Popen。', file=sys.stderr)
    return 1
if __name__ == '__main__':
    if sys.argv[1:2] == ['--probe-refusal']:
        raise SystemExit(cmd_probe_refusal(sys.argv[2:]))
    print('ccpick_cdp.py 是 ccpick 内部后端；请用 ccpick auto-enroll。', file=sys.stderr)
    raise SystemExit(2)
