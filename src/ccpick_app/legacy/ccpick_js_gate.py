from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from ccpick import chrome_user_data_dir
from ccpick_cleanup import _atomic_write_local_state, _split_profiles, chrome_running, validate_profile_target
_PREF_PATH = 'browser.allow_javascript_apple_events'
_MIRROR_PATH = 'account_values.browser.allow_javascript_apple_events'

def _read_preferences(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Preferences 不存在、不是普通文件或是 symlink')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        raise ValueError('Preferences 解析失败: %s' % type(e).__name__) from e
    if not isinstance(data, dict):
        raise ValueError('Preferences 顶层不是对象')
    return data

def _gate_value(data: dict) -> bool | None:
    if 'browser' not in data:
        return None
    browser = data['browser']
    if not isinstance(browser, dict):
        raise ValueError('Preferences.browser 不是对象')
    if 'allow_javascript_apple_events' not in browser:
        return None
    value = browser['allow_javascript_apple_events']
    if not isinstance(value, bool):
        raise ValueError('%s 不是布尔值' % _PREF_PATH)
    return value

def _set_gate(data: dict, value: bool) -> None:
    if 'browser' not in data:
        browser = {}
        data['browser'] = browser
    else:
        browser = data['browser']
    if not isinstance(browser, dict):
        raise ValueError('Preferences.browser 不是对象')
    browser['allow_javascript_apple_events'] = value
    if 'account_values' not in data:
        account_values = {}
        data['account_values'] = account_values
    else:
        account_values = data['account_values']
    if not isinstance(account_values, dict):
        raise ValueError('Preferences.account_values 不是对象')
    if 'browser' not in account_values:
        mirror_browser = {}
        account_values['browser'] = mirror_browser
    else:
        mirror_browser = account_values['browser']
    if not isinstance(mirror_browser, dict):
        raise ValueError('Preferences.account_values.browser 不是对象')
    mirror_browser['allow_javascript_apple_events'] = value

def _keep_backup(path: Path, label: str='Preferences') -> Path:
    original = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix='.ccpick-backup-', dir=path.parent)
    temp = Path(temp_name)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = path.with_name('%s.ccpick-backup-%s-%d.json' % (label, stamp, os.getpid()))
    suffix = 1
    while backup.exists():
        backup = path.with_name('%s.ccpick-backup-%s-%d-%d.json' % (label, stamp, os.getpid(), suffix))
        suffix += 1
    try:
        with os.fdopen(fd, 'wb') as fh:
            _set_mode(fh, temp, mode)
            fh.write(original)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, backup)
        return backup
    finally:
        temp.unlink(missing_ok=True)

def _set_mode(fh, path: Path, mode: int) -> None:
    if hasattr(os, 'fchmod'):
        os.fchmod(fh.fileno(), mode)
    else:
        os.chmod(path, mode)

def _display_value(value: bool | None) -> str:
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return 'key absent'

def cmd_enable_js_gate(args: list[str]) -> int:
    if sys.platform != 'darwin':
        print('ccpick enable-js-gate 目前只用于 macOS。', file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog='ccpick enable-js-gate')
    ap.add_argument('--profiles', nargs='+', required=True, help='目录名，可空格分隔或逗号分隔')
    ap.add_argument('--apply', action='store_true', help='真的写入；默认只读报告')
    ap.add_argument('--disable', action='store_true', help='反方向：把闸门关掉。开过多余的 profile 用这个收回来')
    ns = ap.parse_args(args)
    target_value = not ns.disable
    verb = '启用' if target_value else '关闭'
    requested = _split_profiles(ns.profiles)
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
    plans: list[tuple[str, Path]] = []
    for profile in requested:
        try:
            target = validate_profile_target(ud, state, profile)
            prefs = target / 'Preferences'
            data = _read_preferences(prefs)
            value = _gate_value(data)
            proposed = json.loads(json.dumps(data))
            _set_gate(proposed, target_value)
        except ValueError as e:
            print('[拒绝] %s: %s' % (profile, e), file=sys.stderr)
            return 1
        print('[当前] %s: %s = %s' % (profile, _PREF_PATH, _display_value(value)))
        if value is target_value:
            print('[计划] %s: 已经是 %s，--apply 只会原样重写一遍（无害）' % (profile, _display_value(target_value)))
        else:
            print('[计划] %s: --apply 将写入 %s = %s；%s = %s' % (profile, _PREF_PATH, _display_value(target_value), _MIRROR_PATH, _display_value(target_value)))
        plans.append((profile, prefs))
    if not ns.apply:
        print('仅演练（%s 方向）；没有写文件。完全退出 Chrome 后加 --apply 才会执行。' % verb)
        return 0
    if target_value:
        print('安全提醒：启用后，任何已获 macOS“自动化”权限控制 Chrome 的应用，都能在这些 profile 的标签页中执行 JavaScript。若要撤销，请在对应 profile 中打开 View > Developer > Allow JavaScript from Apple Events，再关闭该菜单项，或重跑本命令并加 --disable。')
    else:
        print('正在关闭这些 profile 的 Apple Events JavaScript 闸门。关掉之后，auto-authorize 在这些 profile 上会失效（需要人工点授权），这是预期的 —— 只给真正要入库的账号开闸，是把授权面收窄。')
    running, detail = chrome_running(ud)
    if running is not False:
        why = 'Chrome 正在使用这个 User Data 目录' if running else '无法确认 Chrome 已退出'
        print('[拒绝] %s：%s。运行中写 Preferences 会在 Chrome 退出时被覆盖。' % (why, detail), file=sys.stderr)
        return 1
    for profile, prefs in plans:
        running, detail = chrome_running(ud)
        if running is not False:
            print('[停止] 写 %s 前 Chrome 运行态变化：%s。' % (profile, detail), file=sys.stderr)
            return 1
        try:
            current = _read_preferences(prefs)
            _set_gate(current, target_value)
            backup = _keep_backup(prefs)
            running, detail = chrome_running(ud)
            if running is not False:
                print('[停止] 备份后、写 %s 前 Chrome 运行态变化：%s。原文件未改。' % (profile, detail), file=sys.stderr)
                return 1
            _atomic_write_local_state(prefs, current)
            persisted = _read_preferences(prefs)
            direct = _gate_value(persisted)
            mirror = persisted.get('account_values', {}).get('browser', {}).get('allow_javascript_apple_events')
            if direct is not target_value or mirror is not target_value:
                print('[失败] %s 写后复读未得到 %s（实得 %r / 镜像 %r）；不能报告成功。备份=%s' % (profile, _display_value(target_value), direct, mirror, backup), file=sys.stderr)
                return 1
        except Exception as e:
            print('[失败] %s: %s。' % (profile, type(e).__name__), file=sys.stderr)
            return 1
        print('[已确认] %s: %s = %s（写后复读）；备份=%s' % (profile, _PREF_PATH, _display_value(target_value), backup))
    return 0
if __name__ == '__main__':
    raise SystemExit(cmd_enable_js_gate(sys.argv[1:]))
