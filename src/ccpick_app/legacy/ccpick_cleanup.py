from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from ccpick import OSASCRIPT, _applescript_str, chrome_user_data_dir, _default_user_data_dir

def profile_reference_paths(state: dict) -> dict[str, set[str]]:
    profile_state = state.get('profile') if isinstance(state, dict) else None
    cache = profile_state.get('info_cache') if isinstance(profile_state, dict) else None
    profiles = set(cache.keys()) if isinstance(cache, dict) else set()
    found: dict[str, set[str]] = {p: set() for p in profiles}

    def walk(value, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in profiles:
                    found[key].add('.'.join(path + ('<key>',)))
                walk(child, path + (key,))
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, str) and child in profiles:
                    found[child].add('.'.join(path + ('[]',)))
                else:
                    walk(child, path + ('[]',))
        elif isinstance(value, str) and value in profiles:
            found[value].add('.'.join(path))
    walk(state, ())
    return found

def remove_profile_references(state: dict, profile: str) -> tuple[dict, list[str]]:
    out = deepcopy(state)
    changed: list[str] = []

    def walk(value, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key in list(value):
                child = value[key]
                if key == profile:
                    changed.append('.'.join(path + ('<key>',)))
                    del value[key]
                elif child == profile:
                    changed.append('.'.join(path + (key,)))
                    del value[key]
                else:
                    walk(child, path + (key,))
        elif isinstance(value, list):
            kept = []
            for child in value:
                if child == profile:
                    changed.append('.'.join(path + ('[]',)))
                else:
                    walk(child, path + ('[]',))
                    kept.append(child)
            value[:] = kept
    walk(out, ())
    return (out, sorted(set(changed)))

def validate_profile_target(user_data: Path, state: dict, profile: str) -> Path:
    if not profile or '..' in profile or '/' in profile or ('\\' in profile) or any((ord(c) < 32 or ord(c) == 127 for c in profile)) or (Path(profile).name != profile):
        raise ValueError('profile 目录名含路径成分')
    profile_state = state.get('profile') if isinstance(state, dict) else None
    known = profile_state.get('info_cache') if isinstance(profile_state, dict) else None
    if not isinstance(known, dict):
        raise ValueError('Local State 的 profile.info_cache 不是对象')
    if profile not in known:
        raise ValueError('Local State 的 profile.info_cache 不含该目录')
    target = user_data / profile
    if target.is_symlink():
        raise ValueError('profile 目录是 symlink，拒绝')
    if not target.is_dir():
        raise ValueError('profile 目录不存在或不是目录')
    ud_real = user_data.resolve(strict=True)
    target_real = target.resolve(strict=True)
    if target_real.parent != ud_real:
        raise ValueError('profile 解析后不在 Chrome User Data 的直接子级')
    return target_real

def _singleton_lock_pid(user_data: Path) -> tuple[int | None, str]:
    lock = user_data / 'SingletonLock'
    try:
        if not lock.is_symlink():
            return (None, '无 SingletonLock')
        tail = os.readlink(lock).rsplit('-', 1)[-1]
        return (int(tail), 'SingletonLock -> pid %s' % tail) if tail.isdigit() else (None, 'SingletonLock 目标无法解析')
    except OSError as e:
        return (None, '读 SingletonLock 失败: %s' % type(e).__name__)

def _pid_alive(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None

def _windows_chrome_running(user_data: Path) -> tuple[bool | None, str]:
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        find = user32.FindWindowExW
        find.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        find.restype = wintypes.HWND
        bits = ctypes.sizeof(ctypes.c_void_p) * 8
        hwnd_message = wintypes.HWND(-3 & (1 << bits) - 1)
        title = os.path.abspath(os.path.normpath(str(user_data)))
        hwnd = find(hwnd_message, None, 'Chrome_MessageWindow', title)
        return (bool(hwnd), 'Chrome_MessageWindow 匹配该 User Data' if hwnd else '无匹配该 User Data 的 Chrome_MessageWindow')
    except Exception as e:
        return (None, 'Windows singleton window 查询失败: %s' % type(e).__name__)

def chrome_running(user_data: Path | None=None) -> tuple[bool | None, str]:
    if os.name == 'nt':
        target = user_data or _default_user_data_dir()
        return _windows_chrome_running(target)
    if user_data is not None:
        try:
            is_default = user_data.resolve() == _default_user_data_dir().resolve()
        except OSError:
            is_default = True
        if not is_default:
            pid, why = _singleton_lock_pid(user_data)
            if pid is None:
                return (False, why + '（该目录未被任何 Chrome 实例占用）')
            alive = _pid_alive(pid)
            if alive is None:
                return (None, why + '，但无法判断该进程是否存活')
            return (alive, why + ('（进程存活）' if alive else '（陈旧锁，进程已退出）'))
    if not Path(OSASCRIPT).is_file():
        return (None, '找不到 /usr/bin/osascript')
    script = 'if application "Google Chrome" is running then\n    return "yes"\nelse\n    return "no"\nend if\n'
    try:
        r = subprocess.run([OSASCRIPT, '-'], input=script, capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
    except Exception as e:
        return (None, type(e).__name__)
    if r.returncode != 0:
        return (None, (r.stderr or 'AppleScript 失败').strip()[-240:])
    answer = r.stdout.strip()
    return (answer == 'yes', answer)

def _tree_stats(path: Path) -> tuple[int, int]:
    count = total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            p = Path(root) / name
            try:
                if not p.is_symlink():
                    total += p.stat().st_size
                    count += 1
            except OSError:
                pass
    return (count, total)

def _bookmark_count(path: Path) -> int:
    try:
        data = json.loads((path / 'Bookmarks').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 0
    except Exception:
        return -1
    count = 0

    def walk(value) -> None:
        nonlocal count
        if isinstance(value, dict):
            if value.get('type') == 'url':
                count += 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)
    return count

def move_to_trash(path: Path) -> tuple[bool, str]:
    script = f"""set targetPath to {_applescript_str(str(path))}\ntry\n    -- ★必须 `as alias`★ —— 裸 `POSIX file p` 只是个文件说明符, Finder 的 delete 要的是\n    -- 对象引用, 直接给会报 -1728 "Can't get POSIX file ..."。实测四种写法:\n    --   delete POSIX file p              -> -1728 失败(这就是原来的写法)\n    --   delete (POSIX file p as alias)   -> 成功, 落在 ~/.Trash        ← 用它\n    --   move (POSIX file p as alias) to trash / delete (item (... as text)) 也成功\n    -- Finder 的 delete 语义就是移入废纸篓, Put Back 可用(第 7 条铁律)。\n    -- as alias 要求路径此刻存在; 调用方已先做过存在性与归属校验。\n    tell application "Finder" to delete (POSIX file targetPath as alias)\n    return "trashed"\non error errMsg number errNum\n    return "error" & tab & (errNum as text) & tab & errMsg\nend try\n"""
    try:
        r = subprocess.run([OSASCRIPT, '-'], input=script, capture_output=True, text=True, timeout=180, encoding='utf-8', errors='replace')
    except Exception as e:
        return (False, type(e).__name__)
    out = (r.stdout or r.stderr or '').strip()
    return (r.returncode == 0 and out == 'trashed', out)

def _atomic_write_local_state(path: Path, state: dict, backup_label: str | None=None) -> Path | None:
    from ccpick_js_gate import _keep_backup, _set_mode
    backup = _keep_backup(path, backup_label) if backup_label else None
    encoded = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix='.ccpick-local-state-', dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as fh:
            _set_mode(fh, temp, mode)
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return backup

def _split_profiles(values: list[str]) -> list[str]:
    out = []
    for value in values:
        for item in value.split(','):
            profile = item.strip()
            if profile and profile not in out:
                out.append(profile)
    return out

def cmd_cleanup_profile(args: list[str]) -> int:
    if sys.platform != 'darwin':
        print('ccpick cleanup-profile 目前只用于 macOS；Windows 用 finish-profile-cleanup.ps1。', file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog='ccpick cleanup-profile')
    ap.add_argument('--profiles', nargs='+', required=True, help='目录名，可空格分隔或逗号分隔')
    ap.add_argument('--keep', nargs='*', default=[])
    ap.add_argument('--apply', action='store_true', help='真的执行；默认只演练并报告 Local State 引用')
    ns = ap.parse_args(args)
    requested = _split_profiles(ns.profiles)
    keep = set(_split_profiles(ns.keep))
    if not requested:
        print('--profiles 没有给出有效目录名。', file=sys.stderr)
        return 2
    ud = chrome_user_data_dir()
    if not ud:
        print('找不到 Chrome User Data。', file=sys.stderr)
        return 1
    local_state = ud / 'Local State'
    if local_state.is_symlink() or not local_state.is_file():
        print('Local State 不存在、不是普通文件或是 symlink，拒绝。', file=sys.stderr)
        return 1
    try:
        state = json.loads(local_state.read_text(encoding='utf-8'))
    except Exception as e:
        print('Local State 解析失败: %s' % type(e).__name__, file=sys.stderr)
        return 1
    refs = profile_reference_paths(state)
    plans = []
    for profile in requested:
        if profile in keep:
            print('[跳过] %s 在保留清单' % profile)
            continue
        try:
            target = validate_profile_target(ud, state, profile)
        except ValueError as e:
            print('[拒绝] %s: %s' % (profile, e), file=sys.stderr)
            return 1
        files, size = _tree_stats(target)
        bookmarks = _bookmark_count(target)
        paths = sorted(refs.get(profile, set()))
        bookmark_text = '约 %d 个书签' % bookmarks if bookmarks >= 0 else '书签文件无法解析'
        print('[计划] %s  %.1f MB  %d 个文件  %s' % (profile, size / (1024 * 1024), files, bookmark_text))
        print('       Local State 引用: %s' % (', '.join(paths) if paths else '无'))
        plans.append((profile, target))
    if not ns.apply:
        print('仅演练；确认后加 --apply。目录会进 Finder 废纸篓，可使用 Put Back。')
        return 0
    running, detail = chrome_running(ud)
    if running is not False:
        why = 'Chrome 仍在运行' if running else '无法确认 Chrome 已退出: %s' % detail
        print(why + '。请完全退出 Chrome 后重跑；运行中改 Local State 会被覆盖。', file=sys.stderr)
        return 1
    trashed: list[str] = []
    failed = False
    for profile, target in plans:
        running, detail = chrome_running(ud)
        if running is not False:
            print('[停止] Chrome 运行态变化，未继续处理。', file=sys.stderr)
            failed = True
            break
        ok, why = move_to_trash(target)
        if not ok:
            print('[失败] %s 未移入废纸篓: %s' % (profile, why), file=sys.stderr)
            failed = True
            break
        trashed.append(profile)
        print('[已移入废纸篓] %s（Finder Put Back 可还原）' % profile)
    if trashed:
        running, detail = chrome_running(ud)
        if running is not False:
            print('Chrome 在清理中启动；为避免覆盖，未改 Local State。目录已在废纸篓，可 Put Back。', file=sys.stderr)
            return 1
        new_state = state
        changed: list[str] = []
        for profile in trashed:
            new_state, paths = remove_profile_references(new_state, profile)
            changed.extend(('%s:%s' % (profile, p) for p in paths))
        try:
            backup = _atomic_write_local_state(local_state, new_state, backup_label='Local State')
        except Exception as e:
            print('Local State 写入失败（目录仍可从废纸篓 Put Back）: %s' % type(e).__name__, file=sys.stderr)
            return 1
        print('[已更新 Local State] 移除 %d 处引用；原文件备份在 %s' % (len(set(changed)), backup))
    return 1 if failed else 0
if __name__ == '__main__':
    raise SystemExit(cmd_cleanup_profile(sys.argv[1:]))
