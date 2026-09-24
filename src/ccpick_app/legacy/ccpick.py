from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
APP = 'ccpick'
_AUTH_HOSTS = {'claude.com', 'www.claude.com', 'platform.claude.com', 'claude.ai', 'www.claude.ai', 'console.anthropic.com', 'anthropic.com', 'www.anthropic.com'}
ENV_PROFILE = 'CCPICK_PROFILE'
LABELS_PATH = _runtime.data_dir() / 'labels.json'
ENV_PICKER_MARKER = 'CCPICK_SELFTEST_MARKER'
ENV_PICKER_NONCE = 'CCPICK_SELFTEST_NONCE'
ENV_DETACHED_PICKER = 'CCPICK_DETACHED_PICKER'
OSASCRIPT = '/usr/bin/osascript'
ENV_USER_DATA_DIR = 'CCPICK_CHROME_USER_DATA_DIR'

def _default_user_data_dir() -> Path:
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or ''
        return Path(base) / 'Google' / 'Chrome' / 'User Data'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Google' / 'Chrome'
    return Path.home() / '.config' / 'google-chrome'

def chrome_user_data_dir() -> Path | None:
    override = os.environ.get(ENV_USER_DATA_DIR)
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    p = _default_user_data_dir()
    return p if p.is_dir() else None

def chrome_binary() -> str | None:
    if sys.platform == 'darwin':
        for c in ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', str(Path.home()) + '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'):
            if Path(c).is_file():
                return c
        return None
    if sys.platform == 'win32':
        cands = []
        for var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(var)
            if base:
                cands.append(Path(base) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe')
        for c in cands:
            if c.is_file():
                return str(c)
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe') as k:
                        val = winreg.QueryValueEx(k, None)[0]
                        if val and Path(val).is_file():
                            return val
                except OSError:
                    continue
        except Exception:
            pass
        return None
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        p = shutil.which(name)
        if p:
            return p
    return None

def load_labels() -> dict:
    try:
        return json.loads(LABELS_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}

def profiles_from_state(state: dict, user_data: Path, labels: dict | None=None) -> list[dict]:
    labels = labels or {}
    out = []
    profile = state.get('profile') if isinstance(state, dict) else None
    cache = profile.get('info_cache') if isinstance(profile, dict) else None
    if not isinstance(cache, dict):
        return []
    for d, info in cache.items():
        if not isinstance(d, str) or not isinstance(info, dict):
            continue
        if not (user_data / d).is_dir():
            continue
        out.append({'dir': d, 'name': info.get('name') or d, 'account': info.get('user_name') or info.get('gaia_name') or '', 'label': labels.get(d, '')})
    out.sort(key=lambda p: (p['dir'] != 'Default', p['name']))
    return out

def list_profiles() -> list[dict]:
    ud = chrome_user_data_dir()
    if not ud:
        return []
    try:
        state = json.loads((ud / 'Local State').read_text(encoding='utf-8'))
    except Exception:
        return []
    return profiles_from_state(state, ud, load_labels())

def is_claude_auth_url(url: str) -> bool:
    if not url or url != url.strip() or '\\' in url or any((ord(ch) < 32 or ord(ch) == 127 for ch in url)):
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != 'https':
        return False
    try:
        host = (u.hostname or '').lower().rstrip('.')
    except ValueError:
        return False
    if not host.isascii():
        return False
    if host not in _AUTH_HOSTS:
        return False
    try:
        if u.port not in (None, 443):
            return False
    except ValueError:
        return False
    if u.username is not None or u.password is not None:
        return False
    path = (u.path or '').lower()
    return path.rstrip('/') in ('/oauth/authorize', '/cai/oauth/authorize')

def _system_exe(*rel: str) -> str | None:
    root = os.environ.get('SystemRoot') or 'C:\\Windows'
    p = Path(root).joinpath(*rel)
    if p.is_file():
        return str(p)
    found = shutil.which(rel[-1])
    if found and Path(found).is_absolute() and (Path(root) in Path(found).parents):
        return found
    return None

def _spawn_detached(argv: list[str]) -> bool:
    if not argv or not argv[0] or (not Path(argv[0]).is_absolute()):
        return False
    try:
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if sys.platform == 'win32':
            kwargs['creationflags'] = 8 | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except Exception:
        return False

def _run_osascript(script: str, timeout: int=15) -> subprocess.CompletedProcess | None:
    if not Path(OSASCRIPT).is_file():
        return None
    try:
        return subprocess.run([OSASCRIPT, '-'], input=script, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
    except Exception:
        return None

def _spawn_osascript_detached(script: str) -> bool:
    if not Path(OSASCRIPT).is_file():
        return False
    try:
        proc = subprocess.Popen([OSASCRIPT, '-'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True, encoding='utf-8', errors='replace')
        assert proc.stdin is not None
        proc.stdin.write(script)
        proc.stdin.close()
        return True
    except Exception:
        return False

def _raise_chrome_tab_script(url: str) -> str:
    target = _applescript_str(url)
    return f'on isOAuthFlowURL(u)\n    return (u starts with "https://accounts.google.com/") or ¬\n        (u starts with "https://claude.ai/") or ¬\n        (u starts with "https://www.claude.ai/") or ¬\n        (u starts with "https://claude.com/") or ¬\n        (u starts with "https://www.claude.com/") or ¬\n        (u starts with "https://platform.claude.com/") or ¬\n        (u starts with "https://console.anthropic.com/") or ¬\n        (u starts with "http://localhost:") or ¬\n        (u starts with "http://127.0.0.1:")\nend isOAuthFlowURL\n\nset targetURL to {target}\nset initialWindowIDs to {{}}\nif application "Google Chrome" is running then\n    tell application "Google Chrome" to set initialWindowIDs to id of every window\nend if\nrepeat 80 times\n    if application "Google Chrome" is running then\n        tell application "Google Chrome"\n            repeat with wi from 1 to (count of windows)\n                set w to window wi\n                set isNewWindow to ((id of w) is not in initialWindowIDs)\n                repeat with ti from 1 to (count of tabs of w)\n                    try\n                        set tabURL to URL of tab ti of w\n                        if tabURL is targetURL or (isNewWindow and my isOAuthFlowURL(tabURL)) then\n                            set active tab index of w to ti\n                            set index of w to 1\n                            activate\n                            return "raised"\n                        end if\n                    end try\n                end repeat\n            end repeat\n        end tell\n    end if\n    delay 0.1\nend repeat\nreturn "not-found"\n'

def open_in_profile(url: str, profile_dir: str, new_window: bool=True) -> bool:
    exe = chrome_binary()
    if not exe:
        return False
    argv = [exe, f'--profile-directory={profile_dir}']
    if os.environ.get('CCPICK_CDP_ACTIVE') == '1':
        override = os.environ.get(ENV_USER_DATA_DIR)
        if override:
            argv.append(f'--user-data-dir={Path(override).expanduser()}')
    if new_window:
        argv.append('--new-window')
    argv.append(url)
    if sys.platform == 'darwin' and new_window and (os.environ.get('CCPICK_CDP_ACTIVE') != '1'):
        if not _spawn_osascript_detached(_raise_chrome_tab_script(url)):
            return False
    return _spawn_detached(argv)

def open_default(url: str) -> bool:
    if sys.platform == 'win32':
        exe = _system_exe('System32', 'rundll32.exe')
        return _spawn_detached([exe, 'url,OpenURL', url]) if exe else False
    if sys.platform == 'darwin':
        return _spawn_detached(['/usr/bin/open', url])
    xdg = shutil.which('xdg-open')
    return _spawn_detached([xdg, url]) if xdg else False
PICK_DEFAULT = '__default_browser__'
PICK_CANCEL = None
PROFILE_ACCOUNTS_FILE = _runtime.data_dir() / 'profile-accounts.json'

def load_profile_accounts() -> dict:
    try:
        data = json.loads(PROFILE_ACCOUNTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and key and value.strip():
            out[key] = value.strip().lower()
    return out

def record_profile_account(profile_dir: str, email: str) -> None:
    if not profile_dir or not email or (not str(email).strip()):
        return
    try:
        _runtime.ensure_data_dir()
        current = load_profile_accounts()
        normalized = str(email).strip().lower()
        if current.get(profile_dir) == normalized:
            return
        current[profile_dir] = normalized
        tmp = PROFILE_ACCOUNTS_FILE.with_name(PROFILE_ACCOUNTS_FILE.name + '.tmp')
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, PROFILE_ACCOUNTS_FILE)
    except Exception:
        pass
PENDING_LOGIN_FILE = _runtime.data_dir() / '.login-pending'
PENDING_LOGIN_MAX_AGE_S = 1800.0

def note_login_profile(profile_dir: str) -> None:
    if not profile_dir:
        return
    try:
        _runtime.ensure_data_dir()
        PENDING_LOGIN_FILE.write_text(json.dumps({'profile': profile_dir, 'ts': time.time()}), encoding='utf-8')
    except Exception:
        pass

def claim_login_profile(max_age_s: float=PENDING_LOGIN_MAX_AGE_S) -> str | None:
    try:
        raw = PENDING_LOGIN_FILE.read_text(encoding='utf-8')
    except Exception:
        return None
    try:
        PENDING_LOGIN_FILE.unlink()
    except OSError:
        pass
    try:
        obj = json.loads(raw)
        profile = str(obj.get('profile') or '')
        ts = float(obj.get('ts') or 0)
    except Exception:
        return None
    if not profile or time.time() - ts > max_age_s:
        return None
    return profile

def account_usage_index() -> tuple[dict, float | None]:
    try:
        from ccpick_usage import collect, counted_windows
        rows = collect()
    except Exception:
        return ({}, None)
    index: dict = {}
    newest: float | None = None
    for r in rows or []:
        email = str(r.get('email') or '').strip().lower()
        if not email:
            continue
        windows = r.get('windows') or {}
        remaining: list[float] = []
        binding: tuple[float, str, str] | None = None
        counted = counted_windows(windows)
        for key in ['5h', '7d'] + [k for k in counted if k not in ('5h', '7d')]:
            w = counted.get(key) or {}
            pct = w.get('pct')
            if pct is None:
                continue
            try:
                left = 100.0 - float(pct)
            except (TypeError, ValueError):
                continue
            remaining.append(left)
            if binding is None or left < binding[0]:
                binding = (left, key, str(w.get('at') or w.get('in') or ''))
        fetched = r.get('fetched_at')
        if isinstance(fetched, (int, float)) and (newest is None or fetched > newest):
            newest = float(fetched)
        index[email] = {'headroom': min(remaining) if remaining else None, 'binding': binding[1] if binding else None, 'resets': binding[2] if binding else '', 'error': r.get('error')}
    return (index, newest)

def _usage_age_text(fetched_at: float | None) -> str:
    if fetched_at is None:
        return '额度数据：无（还没有已入库的账号，或缓存读不到）'
    age = max(0.0, time.time() - float(fetched_at))
    if age < 90:
        when = '刚刚'
    elif age < 3600:
        when = '%d 分钟前' % int(age // 60)
    elif age < 86400:
        when = '%.1f 小时前' % (age / 3600.0)
    else:
        when = '%.1f 天前' % (age / 86400.0)
    warn = '\u3000⚠️ 可能已经不准，仅供参考' if age >= 1800 else ''
    return '额度数据：%s采集（本地缓存，不联网）%s' % (when, warn)

def _annotate_profiles(profiles: list[dict]) -> tuple[list[dict], str]:
    index, fetched = account_usage_index()
    mapping = load_profile_accounts()
    rows = []
    for i, p in enumerate(profiles):
        google = str(p.get('account') or '').strip().lower()
        email = mapping.get(p.get('dir') or '') or google
        info = index.get(email) if email else None
        hr = info.get('headroom') if info else None
        if info is None:
            note = '未入库'
        elif hr is None:
            note = '额度未知'
        else:
            note = '余量 %.0f%%' % hr
            if info.get('resets'):
                note += '（%s 满 %s）' % (info['binding'], info['resets'])
        claude_note = 'Claude: %s' % email if email and email != google else None
        rows.append({'profile': p, 'headroom': hr, 'note': note, 'claude_account': claude_note, 'order': i})
    rows.sort(key=lambda r: (r['headroom'] is None, -(r['headroom'] or 0.0), r['order']))
    return (rows, _usage_age_text(fetched))

def pick_profile_tk(profiles: list[dict], timeout_s: int=180) -> str | None:
    import tkinter as tk
    from tkinter import ttk
    chosen: list[str | None] = [PICK_CANCEL]
    root = tk.Tk()
    root.title('Claude Code 登录 — 选一个 Chrome 配置文件')
    root.geometry('560x420')
    root.minsize(460, 320)
    root.attributes('-topmost', True)
    root.after(400, lambda: root.attributes('-topmost', False))
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    ordered, age_text = _annotate_profiles(profiles)
    profiles = [r['profile'] for r in ordered]
    ttk.Label(root, text='用哪个 Chrome 配置文件去授权？\n每个配置文件有独立的登录状态，等于一个常驻登录的 Claude 账号。', justify='left').pack(anchor='w', padx=12, pady=(12, 2))
    ttk.Label(root, text=age_text, justify='left', foreground='#8a6d00' if '⚠️' in age_text else '#555555').pack(anchor='w', padx=12, pady=(0, 6))
    frame = ttk.Frame(root)
    frame.pack(fill='both', expand=True, padx=12)
    sb = ttk.Scrollbar(frame, orient='vertical')
    lb = tk.Listbox(frame, activestyle='dotbox', yscrollcommand=sb.set, exportselection=False)
    sb.config(command=lb.yview)
    sb.pack(side='right', fill='y')
    lb.pack(side='left', fill='both', expand=True)
    for row in ordered:
        p = row['profile']
        bits = [p['name']]
        if p['account']:
            bits.append(p['account'])
        if p['label']:
            bits.append(p['label'])
        if row.get('claude_account'):
            bits.append(row['claude_account'])
        bits.append(row['note'])
        lb.insert('end', '   ·   '.join(bits) + f"      [{p['dir']}]")
    if profiles:
        lb.selection_set(0)
        lb.activate(0)
    lb.focus_set()

    def confirm(_evt=None):
        sel = lb.curselection()
        if sel:
            chosen[0] = profiles[sel[0]]['dir']
            root.destroy()

    def use_default(_evt=None):
        chosen[0] = PICK_DEFAULT
        root.destroy()

    def cancel(_evt=None):
        chosen[0] = PICK_CANCEL
        root.destroy()
    lb.bind('<Double-Button-1>', confirm)
    root.bind('<Return>', confirm)
    root.bind('<Escape>', cancel)
    root.protocol('WM_DELETE_WINDOW', cancel)
    bar = ttk.Frame(root)
    bar.pack(fill='x', padx=12, pady=10)
    ttk.Button(bar, text='用这个配置文件打开', command=confirm).pack(side='right')
    ttk.Button(bar, text='用默认浏览器', command=use_default).pack(side='right', padx=(0, 8))
    ttk.Button(bar, text='取消', command=cancel).pack(side='left')
    marker = os.environ.get(ENV_PICKER_MARKER)
    marker_created = False
    if marker:
        try:
            root.update_idletasks()
            root.update()
            if root.winfo_ismapped() and root.winfo_viewable():
                payload = json.dumps({'pid': os.getpid(), 'nonce': os.environ.get(ENV_PICKER_NONCE, ''), 'backend': 'tk', 'mapped': True, 'viewable': True, 'geometry': root.winfo_geometry()}, ensure_ascii=False)
                fd = os.open(marker, os.O_WRONLY | os.O_TRUNC, 384)
                with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    fh.write(payload)
                marker_created = True
        except Exception:
            pass
    root.after(timeout_s * 1000, use_default)
    try:
        root.mainloop()
    finally:
        if marker_created:
            try:
                Path(marker).unlink()
            except OSError:
                pass
    return chosen[0]

def _applescript_str(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

def pick_profile_native(profiles: list[dict]) -> str | None:
    items = [f"{p['name']} — {p['account'] or '(未登录 Google)'}  [{p['dir']}]" for p in profiles]
    if sys.platform == 'darwin':
        numbered = ['%d. %s' % (n + 1, it) for n, it in enumerate(items)]
        lst = ', '.join((_applescript_str(i) for i in numbered))
        script = f'set r to choose from list {{{lst}}} with title "Claude Code 登录" with prompt "选一个 Chrome 配置文件" without multiple selections allowed\nif r is false then return ""\nreturn item 1 of r'
        osa = '/usr/bin/osascript'
        if not Path(osa).is_file():
            return PICK_CANCEL
        try:
            out = subprocess.run([osa, '-'], input=script, capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace').stdout.strip()
        except Exception:
            return PICK_CANCEL
        head = out.split('.', 1)[0].strip()
        if head.isdigit():
            idx = int(head) - 1
            if 0 <= idx < len(profiles):
                return profiles[idx]['dir']
        return PICK_CANCEL
    if sys.platform == 'win32':
        import tempfile
        ps_items = ','.join(("'" + i.replace("'", "''") + "'" for i in items))
        ps = f"\nAdd-Type -AssemblyName System.Windows.Forms\nAdd-Type -AssemblyName System.Drawing\n$items = @({ps_items})\n$f = New-Object System.Windows.Forms.Form\n$f.Text = 'Claude Code 登录 — 选一个 Chrome 配置文件'\n$f.Size = New-Object System.Drawing.Size(600,440)\n$f.TopMost = $true\n$lb = New-Object System.Windows.Forms.ListBox\n$lb.Dock = 'Fill'\nforeach ($i in $items) {{ [void]$lb.Items.Add($i) }}\nif ($lb.Items.Count -gt 0) {{ $lb.SelectedIndex = 0 }}\n$ok = New-Object System.Windows.Forms.Button\n$ok.Text = 'OK'; $ok.Dock = 'Bottom'; $ok.DialogResult = 'OK'\n$f.Controls.Add($lb); $f.Controls.Add($ok); $f.AcceptButton = $ok\nif ($f.ShowDialog() -eq 'OK' -and $lb.SelectedIndex -ge 0) {{ Write-Output $lb.SelectedIndex }}\n"
        psexe = _system_exe('System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        if not psexe:
            return PICK_CANCEL
        fd, path = tempfile.mkstemp(suffix='.ps1')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8-sig') as fh:
                fh.write(ps)
            out = subprocess.run([psexe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path], capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace').stdout.strip()
            if out.isdigit() and 0 <= int(out) < len(profiles):
                return profiles[int(out)]['dir']
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return PICK_CANCEL

def pick_profile(profiles: list[dict]) -> str | None:
    try:
        return pick_profile_tk(profiles)
    except Exception:
        return pick_profile_native(profiles)

def _detach_picker(url: str) -> bool:
    env = dict(os.environ)
    env[ENV_DETACHED_PICKER] = '1'
    try:
        proc = subprocess.Popen([sys.executable, '-m', 'ccpick_app', '--url-stdin'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True, creationflags=_runtime.detached_creationflags(), encoding='utf-8', errors='replace')
        assert proc.stdin is not None
        marker = env.get(ENV_PICKER_MARKER)
        if marker:
            payload = json.dumps({'pid': proc.pid, 'nonce': env.get(ENV_PICKER_NONCE, ''), 'backend': 'pending', 'mapped': False, 'viewable': False})
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
        proc.stdin.write(url + '\n')
        proc.stdin.close()
        return True
    except Exception:
        try:
            if 'proc' in locals():
                os.killpg(proc.pid, 15)
        except (OSError, AttributeError):
            pass
        return False

def handle_url(url: str) -> int:
    if not is_claude_auth_url(url):
        return 0 if open_default(url) else 1
    forced = os.environ.get(ENV_PROFILE)
    if forced:
        return 0 if open_in_profile(url, forced) else 1
    profiles = list_profiles()
    if not profiles:
        return 0 if open_default(url) else 1
    choice = pick_profile(profiles)
    if choice is PICK_CANCEL:
        return 1
    if choice == PICK_DEFAULT:
        return 0 if open_default(url) else 1
    note_login_profile(choice)
    return 0 if open_in_profile(url, choice) else 1

def cmd_list(_args: list[str]) -> int:
    profiles = list_profiles()
    if not profiles:
        print('没找到任何 Chrome 配置文件。', file=sys.stderr)
        print(f'  User Data: {chrome_user_data_dir()}', file=sys.stderr)
        return 1
    w1 = max((len(p['dir']) for p in profiles))
    w2 = max((len(p['name']) for p in profiles))
    for p in profiles:
        extra = f"  ({p['label']})" if p['label'] else ''
        print(f"{p['dir']:<{w1}}  {p['name']:<{w2}}  {p['account'] or '(未登录 Google)'}{extra}")
    return 0

def _python_candidates() -> list[str]:
    raw = [os.environ.get('CCPICK_PYTHON', ''), '/opt/homebrew/opt/python@3.13/libexec/bin/python3', '/opt/homebrew/opt/python@3.13/bin/python3.13', '/opt/homebrew/bin/python3', '/usr/local/bin/python3', shutil.which('python3') or '']
    out = []
    seen = set()
    for item in raw:
        resolved = os.path.realpath(item) if item else ''
        if item and Path(item).is_absolute() and os.access(item, os.X_OK) and (resolved not in seen):
            out.append(item)
            seen.add(resolved)
    return out

def _probe_tkinter(executable: str) -> tuple[bool, str]:
    code = "import tkinter as tk; r=tk.Tk(); r.withdraw(); r.update_idletasks(); print('Tk '+str(tk.TkVersion)); r.destroy()"
    try:
        r = subprocess.run([executable, '-c', code], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, encoding='utf-8', errors='replace')
    except Exception as e:
        return (False, f'{executable} — {type(e).__name__}')
    if r.returncode == 0:
        return (True, f"{executable} — {(r.stdout or '').strip() or '初始化成功'}")
    detail = (r.stderr or r.stdout or f'退出码 {r.returncode}').strip().splitlines()[-1]
    return (False, f'{executable} — {detail[:240]}')

def _select_tk_python() -> tuple[str | None, str]:
    failures = []
    for executable in _python_candidates():
        good, detail = _probe_tkinter(executable)
        if good:
            return (executable, detail)
        failures.append(detail)
    return (None, '；'.join(failures) if failures else '没有绝对路径的 Python 候选')

def validate_launcher(path: str | None) -> tuple[bool, str]:
    if not path:
        return (False, '未安装 ~/.local/bin/ccpick')
    p = Path(path)
    if not p.is_absolute() or not p.is_file():
        return (False, f'不是有效文件: {path}')
    if not os.access(p, os.X_OK):
        return (False, f'没有执行权限: {path}')
    return (True, str(p))
JS_GATE_REMEDIATION = '这个开关按 Chrome profile 单独保存，且菜单项不能由工具代点。'

def _probe_chrome_applescript() -> tuple[bool, str, int | None]:
    script = '-- ★AppleScript 的 tab 常量在 tell application "Google Chrome" 块内会被\n-- Chrome 字典的 tab(标签页)类遮蔽，拼出的是字面量 "tab" 而不是制表符。\n-- 所以分隔符一律在块外求值成 SEP 再用。实测: 块内 "A"&tab&"B" -> A t a b B。\nset SEP to tab\nif application "Google Chrome" is running then\n    tell application "Google Chrome"\n        return (name as text) & SEP & (version as text) & SEP & (count of windows as text)\n    end tell\nelse\n    return "__NOT_RUNNING__"\nend if\n'
    r = _run_osascript(script)
    if r is None:
        return (False, 'osascript 不存在或执行超时', None)
    if r.returncode != 0:
        return (False, (r.stderr or 'AppleScript 调用失败').strip()[-300:], None)
    out = r.stdout.strip()
    if out == '__NOT_RUNNING__':
        return (False, 'Chrome 未运行；未启动它，避免 doctor 抢屏', 0)
    parts = out.split('\t')
    if len(parts) != 3 or not parts[2].isdigit():
        return (False, f'AppleScript 返回不可识别: {out[:200]}', None)
    return (True, f'{parts[0]} {parts[1]}，{parts[2]} 个窗口', int(parts[2]))

def _js_gate_remediation(profile_dir: str) -> str:
    return '%s 请完全退出 Chrome 后运行 ccpick enable-js-gate --profiles %s --apply；也可在目标 profile 中手动打开 View > Developer > Allow JavaScript from Apple Events。' % (JS_GATE_REMEDIATION, json.dumps(profile_dir, ensure_ascii=False))

def _probe_chrome_js_gate(profile_dir: str, user_data_dir: Path | None=None) -> tuple[bool | None, str]:
    user_data = user_data_dir if user_data_dir is not None else chrome_user_data_dir()
    if user_data is None:
        return (None, '找不到 Chrome User Data，无法读取 profile 开关')
    prefs = user_data / profile_dir / 'Preferences'
    try:
        data = json.loads(prefs.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return (None, '%s 不存在' % prefs)
    except Exception as e:
        return (None, '%s 无法解析（%s）' % (prefs, type(e).__name__))
    if not isinstance(data, dict):
        return (None, '%s 顶层不是对象' % prefs)

    def value_at(*keys):
        value = data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value
    direct = value_at('browser', 'allow_javascript_apple_events')
    mirror = value_at('account_values', 'browser', 'allow_javascript_apple_events')
    values = [value for value in (direct, mirror) if value is not None]
    if any((not isinstance(value, bool) for value in values)):
        return (None, '%s 的开关值不是布尔值' % profile_dir)
    if len(values) == 2 and values[0] != values[1]:
        return (None, '%s 的 browser/account_values 镜像不一致' % profile_dir)
    enabled = any(values)
    if enabled:
        return (True, '%s 已启用（profile 专属设置）' % profile_dir)
    return (False, _js_gate_remediation(profile_dir))

def _cswap_managed_count() -> tuple[bool, int | None, str]:
    seq = _runtime.backend_data_dir() / 'sequence.json'
    if not seq.exists():
        return (True, 0, f'{seq} 不存在（新安装，0 个）')
    try:
        data = json.loads(seq.read_text(encoding='utf-8'))
        accounts = data.get('accounts') or {}
        if not isinstance(accounts, dict):
            raise ValueError('accounts 不是对象')
        return (True, len(accounts), f'{len(accounts)} 个')
    except Exception as e:
        return (False, None, f'sequence.json 无法解析（{type(e).__name__}）')
URL_ROUTING_CASES = [('https://claude.com/cai/oauth/authorize?client_id=x', True), ('https://platform.claude.com/oauth/authorize?client_id=x', True), ('https://claude.ai/oauth/authorize', True), ('https://claude.com./oauth/authorize', True), ('HTTPS://CLAUDE.COM/oauth/authorize/', True), ('https://claude.com/settings/usage', False), ('https://mcp.atlassian.com/v1/authorize', False), ('https://github.com/anthropics/claude-code', False), ('https://evil.example.com/oauth/authorize', False), ('https://evil.com\\@claude.com/oauth/authorize', False), ('https://account-0027@example.com/oauth/authorize', False), ('https://claude.com:8080/oauth/authorize', False), ('http://claude.com/oauth/authorize', False), ('https://claude.com.evil.com/oauth/authorize', False), ('https://xn--claude-9za.com/oauth/authorize', False), ('https://claude。com/oauth/authorize', False), ('https://claude.com/oauth/authorize.evil', False), ('https://claude.com/other/oauth/authorize', False), (' https://claude.com/oauth/authorize', False), ('https://claude.com/oauth/authorize ', False), ('https://claude.com/oauth/author\nize', False), ('https://claude.com\t.evil.com/oauth/authorize', False), ('https://claude.com:bad/oauth/authorize', False)]

def _autopilot_available() -> tuple[bool, str]:
    here = Path(__file__).resolve().parent
    try:
        from ccpick_cdp import backend_status
        cdp_ok, cdp_why = backend_status(chrome_binary(), chrome_user_data_dir())
    except Exception as e:
        cdp_ok, cdp_why = (False, 'CDP pipe 探测失败（%s）' % type(e).__name__)
    if cdp_ok:
        return (True, 'CDP pipe（当前首选） — %s' % cdp_why)
    if sys.platform == 'win32':
        ps1 = here / 'auto_authorize.ps1'
        return (True, 'Windows UIA（当前回退） — %s；%s' % (ps1, cdp_why)) if ps1.is_file() else (False, '缺 auto_authorize.ps1；%s' % cdp_why)
    if sys.platform == 'darwin':
        script = here / 'ccpick_auto_authorize.py'
        return (True, 'macOS AppleScript（当前回退） — %s；%s' % (script, cdp_why)) if script.is_file() else (False, '缺 ccpick_auto_authorize.py；%s' % cdp_why)
    return (False, '%s 尚未实现 → ccpick_auto.autopilot()' % sys.platform)

def launcher_path() -> str | None:
    return _runtime.launcher_path()

def main(argv: list[str]) -> int:
    from ccpick_app.cli import main
    return main(argv)
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
