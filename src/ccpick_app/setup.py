"""Reversible, per-user browser and optional background-service installation.

No setup operation reads, imports, migrates, or removes stored account credentials.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

from . import runtime

PUBLIC_TASKS = ("ccpick-public-tray", "ccpick-public-fallback")
PRIVATE_TASKS = ("ccpick-autoswitch-tray", "ccpick-autoswitch-fallback")
PUBLIC_LABELS = ("io.github.tykisgod.ccpick.menubar", "io.github.tykisgod.ccpick.autoswitch")
PRIVATE_LABELS = ("com.tyk.claude-autoswitch-menubar", "com.tyk.claude-account-autoswitch")
BEGIN_MARKER = "# >>> ccpick public browser hook >>>"
END_MARKER = "# <<< ccpick public browser hook <<<"


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run(argv, *, check=True, timeout=30):
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.run([str(arg) for arg in argv], check=check, capture_output=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=timeout, **kwargs)


def _powershell(script, *, check=True):
    # EncodedCommand avoids an extra shell-quoting layer for spaces and non-ASCII paths.
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    executable = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                     "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    return _run([executable, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], check=check)


def legacy_services() -> list[str]:
    """Return enabled legacy services (or a live orphaned Windows tray).

    Discovery failure raises: it must never silently permit two autoswitchers.
    """
    system = platform.system()
    if system == "Windows":
        names = ",".join(_ps_quote(name) for name in PRIVATE_TASKS)
        result = _powershell(f"""
$ErrorActionPreference = 'Stop'
$enabled = @()
foreach ($name in @({names})) {{
    $task = Get-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
    if ($task -and ($task.State -ne 'Disabled' -or $task.State -eq 'Running')) {{ $enabled += $name }}
}}
try {{
    $mutex = [System.Threading.Mutex]::OpenExisting('Global\\ccpick-autoswitch-tray')
    $mutex.Dispose()
    $enabled += 'private-tray-process'
}} catch [System.Threading.WaitHandleCannotBeOpenedException] {{}}
ConvertTo-Json -InputObject @($enabled) -Compress
""")
        found = json.loads(result.stdout.strip() or "[]")
        return [found] if isinstance(found, str) else list(found)
    if system == "Darwin":
        found = []
        domain = f"gui/{os.getuid()}"
        for label in PRIVATE_LABELS:
            if _run(["launchctl", "print", f"{domain}/{label}"], check=False).returncode == 0:
                found.append(label)
        # A menu bar launched manually can outlive an unloaded LaunchAgent.
        if _run(["pgrep", "-f", r"/claude-autoswitch-menubar( |$)"], check=False).returncode == 0:
            found.append("private-menubar-process")
        return found
    return []


def _legacy_installation() -> bool:
    home = Path.home()
    return (home / ".claude" / "tools" / "ccpick" / "ccpick.py").exists() or (
        home / "bin" / "claude-account-autoswitch.sh").exists()


def _disable_legacy() -> None:
    if platform.system() == "Windows":
        _check_task_ownership(PRIVATE_TASKS)
        names = ",".join(_ps_quote(name) for name in PRIVATE_TASKS)
        _powershell(f"""
$ErrorActionPreference = 'Stop'
foreach ($name in @({names})) {{
    $task = Get-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {{
        Stop-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskPath '\\' -TaskName $name | Out-Null
    }}
}}
""")
    elif platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        for label in PRIVATE_LABELS:
            _run(["launchctl", "disable", f"{domain}/{label}"])
            _run(["launchctl", "bootout", f"{domain}/{label}"], check=False)
    remaining = legacy_services()
    if remaining:
        raise RuntimeError("The private tray is still running. Quit it, then repeat setup --autoswitch --replace-legacy.")


def _manifest_path() -> Path:
    return runtime.data_dir() / "services" / "install.json"


def _read_manifest() -> dict:
    path = _manifest_path()
    if not path.exists():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("owner") != "ccpick-public" or record.get("schema") != 1:
        raise RuntimeError("Unrecognized setup manifest; refusing to change installation")
    return record


def _write(path: Path, text: str, *, executable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o700)


def _windows_browser() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "BROWSER")
            return value
    except FileNotFoundError:
        return None


def _set_windows_browser(value: str | None) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        if value is None:
            try:
                winreg.DeleteValue(key, "BROWSER")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "BROWSER", 0, winreg.REG_SZ, value)
    # Let subsequently launched applications inherit the new user environment.
    import ctypes
    result = ctypes.c_size_t()
    notify = ctypes.windll.user32.SendMessageTimeoutW
    notify.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
                       ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
    notify.restype = ctypes.c_ssize_t
    environment = ctypes.create_unicode_buffer("Environment")
    notify(0xFFFF, 0x001A, 0, ctypes.cast(environment, ctypes.c_void_p), 2, 2000,
           ctypes.byref(result))


def _shell_rc() -> Path:
    shell = Path(os.environ.get("SHELL", "/bin/zsh" if platform.system() == "Darwin" else "/bin/bash")).name
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR", str(Path.home()))) / ".zshrc"
    if shell == "bash":
        return Path.home() / ".bashrc"
    raise RuntimeError("Automatic BROWSER setup supports bash and zsh. Use --no-browser and set BROWSER manually for your shell.")


def _hook_block(launcher: str) -> str:
    return BEGIN_MARKER + "\nexport BROWSER=" + shlex.quote(launcher) + "\n" + END_MARKER + "\n"


def _set_hook(record: dict, launcher: str) -> None:
    old = record.get("browser", {})
    if platform.system() == "Windows":
        current = _windows_browser()
        previous = old.get("previous") if old.get("kind") == "windows" and current == old.get("installed") else current
        record["browser"] = {"kind": "windows", "previous": previous, "installed": launcher}
        _set_windows_browser(launcher)
    else:
        path = Path(old["path"]) if old.get("kind") == "shell" else _shell_rc()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        block = _hook_block(launcher)
        if BEGIN_MARKER in content:
            if not old or old.get("block") not in content:
                raise RuntimeError("Existing ccpick BROWSER block was edited; refusing to overwrite it")
            content = content.replace(old["block"], block, 1)
        else:
            content += ("\n" if content and not content.endswith("\n") else "") + block
        _write(path, content)
        record["browser"] = {"kind": "shell", "path": str(path), "block": block,
                             "previous": old.get("previous", os.environ.get("BROWSER")), "installed": launcher}


def _remove_hook(record: dict) -> None:
    hook = record.get("browser", {})
    if hook.get("kind") == "windows":
        if _windows_browser() == hook.get("installed"):
            _set_windows_browser(hook.get("previous"))
    elif hook.get("kind") == "shell":
        path = Path(hook["path"])
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if hook["block"] in content:
                _write(path, content.replace(hook["block"], "", 1))


def _command_path() -> Path:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    return config / "commands" / "best-account.md"


def _set_command(record: dict, launcher: str) -> None:
    path = _command_path()
    old = record.get("command", {})
    current = path.read_bytes() if path.exists() else None
    if old.get("path") == str(path) and current == old.get("installed", "").encode("utf-8"):
        previous = old.get("previous")
    else:
        previous = base64.b64encode(current).decode("ascii") if current is not None else None
    content = _command_content(launcher)
    record["command"] = {"path": str(path), "installed": content, "previous": previous}
    _write(path, content)


def _command_content(launcher: str) -> str:
    content = (Path(__file__).parent / "resources" / "best-account.md").read_text(encoding="utf-8")
    return content.replace("{{CCPICK_COMMAND}}", shlex.quote(launcher) + " auto --json")


def _remove_command(record: dict) -> None:
    command = record.get("command", {})
    if not command:
        return
    path = Path(command["path"])
    if path.exists() and path.read_bytes() == command["installed"].encode("utf-8"):
        if command.get("previous") is None:
            path.unlink()
        else:
            path.write_bytes(base64.b64decode(command["previous"]))


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Packaged service asset changed; expected one occurrence of {old!r}")
    return text.replace(old, new, 1)


def _windows_assets(legacy: Path, service_root: Path, state_root: Path) -> dict[str, str]:
    source = (legacy / "autoswitch" / "win" / "claude-autoswitch-tray.ps1").read_text(encoding="utf-8-sig")
    source = source.replace('Global\\ccpick-autoswitch-tray', 'Local\\ccpick-public-tray')
    source = _replace_once(source, '$Root    = Join-Path $env:LOCALAPPDATA "ccpick-autoswitch"',
                           "$Root    = " + _ps_quote(state_root))
    source = _replace_once(source, '$Shared  = Split-Path -Parent $Here', '$Shared  = $Here')
    start = source.index("function Resolve-Python {")
    end = source.index("$script:PYW = Resolve-Python", start)
    source = source[:start] + "function Resolve-Python { return " + _ps_quote(sys.executable) + " }\n" + source[end:]
    source = _replace_once(source, '$cswap = Join-Path $env:USERPROFILE ".local\\bin\\cswap.exe"',
                           "$cswap = " + _ps_quote(sys.executable))
    source = _replace_once(source, "Start-Detached $cswap ('switch \"{0}\"' -f $email)",
                           "Start-Detached $cswap ('-m ccpick_app switch \"{0}\"' -f $email)")
    # All children, including helper and manual backend actions, share the configured state root.
    source = _replace_once(source, '$ErrorActionPreference = "Continue"',
                           '$ErrorActionPreference = "Continue"\n$env:CCPICK_DATA_DIR = ' + _ps_quote(runtime.data_dir()))
    tick = ("[CmdletBinding()]\nparam([switch]$DryRun)\n$ErrorActionPreference = 'Stop'\n"
            "$env:CCPICK_DATA_DIR = " + _ps_quote(runtime.data_dir()) + "\n"
            "$params = @('-m', 'ccpick_app.service', 'tick')\n"
            "if ($DryRun) { $params += '--dry-run' }\n"
            "& " + _ps_quote(sys.executable) + " @params\nexit $LASTEXITCODE\n")
    helper = ("from ccpick_app.service import main\n"
              "import sys\nraise SystemExit(main(['_helper', *sys.argv[1:]]))\n")
    return {"claude-autoswitch-tray.ps1": source, "claude-account-autoswitch.ps1": tick,
            "claude-autoswitch-helper.py": helper}


def _swift_literal(value: str) -> str:
    # JSON escaping is also valid for the path characters Swift accepts here.
    return json.dumps(str(value), ensure_ascii=False)


def _mac_assets(legacy: Path, service_root: Path, state_root: Path) -> dict[str, str]:
    source = (legacy / "autoswitch" / "menubar-main.swift").read_text(encoding="utf-8")
    replacements = {
        '"~/Library/Logs/claude-autoswitch-status.json"': _swift_literal(state_root / "status.json"),
        '"~/Library/Logs/claude-account-autoswitch.log"': _swift_literal(state_root / "autoswitch.log"),
        '"~/bin/claude-autoswitch-helper.py"': _swift_literal(service_root / "claude-autoswitch-helper.py"),
        'p.launchPath = "/usr/bin/python3"': "p.launchPath = " + _swift_literal(sys.executable),
        'NSHomeDirectory() + "/Library/Logs/claude-autoswitch-menubar.debug.log"': _swift_literal(state_root / "menubar.debug.log"),
        '"~/Library/Logs/claude-autoswitch-menubar.placement"': _swift_literal(state_root / "menubar.placement"),
        'home + "/bin/claude-account-autoswitch.sh"': _swift_literal(service_root / "tick.sh"),
        'home + "/Library/Logs/claude-autoswitch-child.stderr.log"': _swift_literal(state_root / "child.stderr.log"),
        'let cswap = ("~/.local/bin/cswap" as NSString).expandingTildeInPath': "let cswap = " + _swift_literal(sys.executable),
        'p.arguments = ["switch", email]': 'p.arguments = ["-m", "ccpick_app", "switch", email]',
        '$0.executableURL?.lastPathComponent == "claude-autoswitch-menubar"': '$0.executableURL?.lastPathComponent == "ccpick-public-menubar"',
    }
    for old, new in replacements.items():
        source = _replace_once(source, old, new)
    source, count = re.subn(r'item\.autosaveName = "[^"\n]+"',
                            'item.autosaveName = "io.github.tykisgod.ccpick.menubar.item"', source)
    if count != 1:
        raise RuntimeError("Packaged menu-bar autosave identifier changed")
    helper = "from ccpick_app.service import main\nimport sys\nraise SystemExit(main(['_helper', *sys.argv[1:]]))\n"
    tick = ("#!/bin/sh\nexport CCPICK_DATA_DIR=" + shlex.quote(str(runtime.data_dir())) + "\n"
            "exec " + shlex.quote(sys.executable) + " -m ccpick_app.service tick\n")
    return {"menubar-main.swift": source, "claude-autoswitch-helper.py": helper, "tick.sh": tick}


def _register_windows(service_root: Path) -> None:
    _check_task_ownership(PUBLIC_TASKS)
    script = f"""
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$exe = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
$tray = New-ScheduledTaskAction -Execute $exe -Argument {_ps_quote('-NoProfile -NonInteractive -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "' + str(service_root / 'claude-autoswitch-tray.ps1') + '"')}
$tick = New-ScheduledTaskAction -Execute $exe -Argument {_ps_quote('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + str(service_root / 'claude-account-autoswitch.ps1') + '"')}
$trayTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $identity), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)))
$tickTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$traySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$tickSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath '\\' -TaskName '{PUBLIC_TASKS[0]}' -Action $tray -Trigger $trayTriggers -Settings $traySettings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskPath '\\' -TaskName '{PUBLIC_TASKS[1]}' -Action $tick -Trigger $tickTrigger -Settings $tickSettings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskPath '\\' -TaskName '{PUBLIC_TASKS[0]}'
"""
    _powershell(script)


def _check_task_ownership(names) -> None:
    """Fixed task identifiers must never overwrite another Windows user's tasks."""
    task_names = ",".join(_ps_quote(name) for name in names)
    _powershell(f"""
$ErrorActionPreference = 'Stop'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($name in @({task_names})) {{
    $task = Get-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {{
        $owner = [string]$task.Principal.UserId
        if ($owner -notlike 'S-1-*') {{
            $account = New-Object Security.Principal.NTAccount($owner)
            $owner = $account.Translate([Security.Principal.SecurityIdentifier]).Value
        }}
        if ($owner -ne $currentSid) {{ throw "Task $name belongs to a different user; refusing to change it." }}
    }}
}}
""")


def _mac_plists(service_root: Path, state_root: Path) -> dict[str, dict]:
    environment = {"HOME": str(Path.home()), "CCPICK_DATA_DIR": str(runtime.data_dir()),
                   "LANG": "en_US.UTF-8", "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    for key in ("CCSWITCH_THRESHOLD", "CCSWITCH_MODELS", "CCPICK_CHROME_USER_DATA_DIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    common = {"EnvironmentVariables": environment, "RunAtLoad": True,
              "StandardOutPath": str(state_root / "service.stdout.log"),
              "StandardErrorPath": str(state_root / "service.stderr.log")}
    return {
        PUBLIC_LABELS[0]: {**common, "Label": PUBLIC_LABELS[0],
                           "ProgramArguments": [str(service_root / "ccpick-public-menubar")],
                           "KeepAlive": True, "ThrottleInterval": 15},
        PUBLIC_LABELS[1]: {**common, "Label": PUBLIC_LABELS[1],
                           "ProgramArguments": [sys.executable, "-m", "ccpick_app.service", "tick"],
                           "StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)],
                           "ProcessType": "Background"},
    }


def _register_mac(service_root: Path, state_root: Path) -> None:
    _run(["swiftc", "-O", "-o", str(service_root / "ccpick-public-menubar.new"),
          str(service_root / "menubar-main.swift"), "-framework", "Cocoa"], timeout=180)
    _run(["codesign", "--force", "--sign", "-", str(service_root / "ccpick-public-menubar.new")])
    (service_root / "ccpick-public-menubar.new").replace(service_root / "ccpick-public-menubar")
    launch_root = Path.home() / "Library" / "LaunchAgents"
    launch_root.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label, plist in _mac_plists(service_root, state_root).items():
        path = launch_root / (label + ".plist")
        path.write_bytes(plistlib.dumps(plist))
        _run(["launchctl", "enable", f"{domain}/{label}"])
        _run(["launchctl", "bootstrap", domain, str(path)])


def _stop_public(*, uninstall=False) -> None:
    if platform.system() == "Windows":
        _check_task_ownership(PUBLIC_TASKS)
        names = ",".join(_ps_quote(name) for name in PUBLIC_TASKS)
        remove = "Unregister-ScheduledTask -TaskPath '\\' -TaskName $name -Confirm:$false" if uninstall else ""
        _powershell(f"""
$ErrorActionPreference = 'Stop'
foreach ($name in @({names})) {{
    $task = Get-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {{
        Stop-ScheduledTask -TaskPath '\\' -TaskName $name -ErrorAction SilentlyContinue
        {remove}
    }}
}}
""")
    elif platform.system() == "Darwin":
        domain = f"gui/{os.getuid()}"
        for label in PUBLIC_LABELS:
            _run(["launchctl", "bootout", f"{domain}/{label}"], check=False)
            path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
            if uninstall and path.exists():
                path.unlink()


def _require_installed_runtime() -> None:
    # Editable checkouts can vanish on branch switches. pip/pipx/uv tool installs are stable.
    for location in (Path(__file__).resolve(), Path(sys.executable).resolve()):
        if any((parent / ".git").exists() for parent in location.parents):
            raise RuntimeError("Install the public package with pipx/uv tool (not editable) before setup; background services must not depend on a Git checkout.")
    from .backend import require_backend
    require_backend()


def setup(*, autoswitch=False, replace_legacy=False, dry_run=False, no_browser=False) -> int:
    system = platform.system()
    if autoswitch and system not in ("Windows", "Darwin"):
        raise RuntimeError("Background installation supports Windows and macOS. On Linux use ccpick autoswitch tick with your own scheduler.")
    launcher = runtime.launcher_path()
    if not launcher:
        raise RuntimeError("Cannot find the ccpick executable in this Python environment. Install the package first.")
    launcher = str(launcher or "").replace("\\", "/") if system == "Windows" else str(launcher or "")
    record = _read_manifest()
    conflicts = legacy_services() if autoswitch else []
    installed_legacy = autoswitch and _legacy_installation()
    if (conflicts or installed_legacy) and not replace_legacy:
        raise RuntimeError("A private ccpick installation/service exists. Use --replace-legacy with --autoswitch to explicitly disable its startup services; account data is never migrated.")
    service_root, state_root = runtime.data_dir() / "services", runtime.data_dir() / "autoswitch"
    legacy = Path(__file__).parent / "legacy"
    # Render and validate every source substitution before making any machine changes.
    assets = (_windows_assets(legacy, service_root, state_root) if system == "Windows" else
              _mac_assets(legacy, service_root, state_root)) if autoswitch else {}
    _command_content(launcher)
    if not no_browser and system != "Windows":
        _shell_rc()
    actions = []
    if autoswitch and replace_legacy and (conflicts or installed_legacy):
        actions.append("Disable private ccpick startup services (retain all private files and accounts)")
    if not no_browser:
        actions.append("Set user BROWSER to " + launcher + "; preserve the previous hook")
    actions.append("Install /best-account at " + str(_command_path()) + "; preserve any previous command")
    if autoswitch:
        actions += [f"Copy {len(assets)} service resources to {service_root}",
                    "Pin background Python to " + sys.executable,
                    "Register user services: " + ", ".join(PUBLIC_TASKS if system == "Windows" else PUBLIC_LABELS),
                    "Start the tray/menu bar; it will begin checking and may switch accounts"]
    actions.append("Keep ccpick state and logs under " + str(runtime.data_dir()) + "; retain the existing claude-swap account store")
    for action in actions:
        print(("[dry-run] " if dry_run else "[setup] ") + action)
    if dry_run:
        return 0
    _require_installed_runtime()
    if autoswitch and system == "Darwin" and not shutil.which("swiftc"):
        raise RuntimeError("macOS menu bar requires Apple's Command Line Tools (swiftc). Install them with xcode-select --install, then repeat setup.")
    if autoswitch and replace_legacy and (conflicts or installed_legacy):
        _disable_legacy()
    service_root.mkdir(parents=True, exist_ok=True)
    record.update({"schema": 1, "owner": "ccpick-public", "python": sys.executable, "platform": system})
    if not no_browser:
        _set_hook(record, launcher)
    _set_command(record, launcher)
    # Save rollback information before registering anything that can outlive this process.
    _write(_manifest_path(), json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    if autoswitch:
        _stop_public()
        state_root.mkdir(parents=True, exist_ok=True)
        for name, text in assets.items():
            # UTF-8 BOM is needed for non-ASCII Windows PowerShell 5.1 source files.
            _write(service_root / name, ("\ufeff" if name.endswith(".ps1") else "") + text,
                   executable=name.endswith(".sh"))
        record["autoswitch"] = True
        _write(_manifest_path(), json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        if system == "Windows":
            _register_windows(service_root)
        else:
            _register_mac(service_root, state_root)
    print("Setup complete. Restart your terminal and Claude Code for BROWSER to take effect.")
    return 0


def uninstall(*, dry_run=False) -> int:
    record = _read_manifest()
    if not record:
        print("No public ccpick setup manifest found; no changes made.")
        return 0
    for action in ("Stop and unregister only ccpick public startup services",
                   "Restore the previous BROWSER hook only if the installed hook is still ours",
                   "Restore the previous /best-account command only if the installed file is still ours",
                   "Keep all credentials, account data, logs and copied resources"):
        print(("[dry-run] " if dry_run else "[uninstall] ") + action)
    if dry_run:
        return 0
    _stop_public(uninstall=True)
    _remove_hook(record)
    _remove_command(record)
    _manifest_path().unlink()
    print("Public integration removed. The Python package and all account data are retained.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ccpick setup")
    parser.add_argument("--autoswitch", action="store_true", help="Enable automatic switching and tray/menu bar")
    parser.add_argument("--replace-legacy", action="store_true", help="Explicitly disable existing private startup services")
    parser.add_argument("--no-browser", action="store_true", help="Leave BROWSER unchanged")
    parser.add_argument("--dry-run", action="store_true", help="Print the proposed integration without changing files or services")
    args = parser.parse_args(argv)
    if args.replace_legacy and not args.autoswitch:
        parser.error("--replace-legacy requires --autoswitch")
    try:
        return setup(**vars(args))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print("ccpick setup: " + str(exc), file=sys.stderr)
        return 1


def cmd_uninstall(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ccpick uninstall")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return uninstall(dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print("ccpick uninstall: " + str(exc), file=sys.stderr)
        return 1


cmd_setup = main
