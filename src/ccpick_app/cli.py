"""The supported public command surface."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from urllib.parse import urlparse

from . import __version__, backend, runtime

HELP = """ccpick — Claude account switching and Chrome profile selection

  ccpick setup [--autoswitch]       Configure Claude's browser and optional service
  ccpick uninstall                 Remove ccpick's integration (keep account data)
  ccpick doctor                    Check installation without logging in or switching
  ccpick list                      List Chrome profiles
  ccpick accounts [--json]          Refresh and list managed accounts
  ccpick status                    Show the backend's current account
  ccpick usage [--json|--history]   Show cached quota and reset times
  ccpick auto [--dry-run] [--json]  Choose among enabled managed accounts
  ccpick login [--profile NAME]    Sign in with manual browser authorization
  ccpick enroll --profile NAME --add  Sign in and save the account
  ccpick auto-enroll --profile NAME --email EMAIL  Automate browser authorization
  ccpick auto-enroll-all [--dry-run]  Authorize selected profiles in sequence
  ccpick enable-js-gate [...]      Configure macOS Apple Events JavaScript access
  ccpick switch [ACCOUNT]          Switch accounts
  ccpick add [--slot N]            Save the current account
  ccpick disable ACCOUNT          Exclude an account from automatic rotation
  ccpick enable ACCOUNT           Return an account to automatic rotation
  ccpick remove ACCOUNT           Remove a saved account
  ccpick autoswitch tick          Run one background switching check

Use COMMAND --help for command-specific options.
An HTTP(S) URL is routed to the browser.
Automatic authorization is opt-in. auto-enroll commands accept --headless and
--user-agent; compatibility depends on Chrome and the account's login flow.
"""


def doctor(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ccpick doctor")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(args)
    runtime.bootstrap()
    import ccpick
    import ccpick_enroll

    facts = {
        "ccpick": __version__,
        "python": sys.executable,
        "backend_version": backend.version(),
        "backend_required": backend.REQUIRED_VERSION,
        "backend_executable": backend.executable(),
        "launcher": runtime.launcher_path(),
        "chrome": ccpick.chrome_binary(),
        "chrome_profiles": len(ccpick.list_profiles()),
        "tkinter_module": importlib.util.find_spec("tkinter") is not None,
        "claude": ccpick_enroll.claude_bin(),
        "data_dir": str(runtime.data_dir()),
        "backend_data_dir": str(runtime.backend_data_dir()),
        "browser": runtime.redact_auth_diagnostic(os.environ.get("BROWSER", "")),
        "autoswitch_status_present": (runtime.data_dir() / "autoswitch" / "status.json").is_file(),
    }
    ok = all(facts[key] for key in ("backend_executable", "launcher", "chrome", "claude"))
    if sys.platform != "darwin":
        ok = ok and facts["tkinter_module"]
    if ns.json:
        print(json.dumps({"ok": ok, **facts}, ensure_ascii=False, indent=2))
    else:
        for key, value in facts.items():
            print(f"{key}: {value if value is not None else '(not found)'}")
        print("Ready." if ok else "Some prerequisites are missing; see the installation guide.")
    return 0 if ok else 1


def _login(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ccpick login")
    parser.add_argument("--profile", help="Chrome profile directory, e.g. Default")
    parser.add_argument("--email", help="Optional Claude login hint")
    ns = parser.parse_args(args)
    import ccpick
    import ccpick_enroll

    executable = ccpick_enroll.claude_bin()
    launcher = runtime.launcher_path()
    if not executable or not launcher:
        raise RuntimeError("Claude Code and the installed ccpick console command are required.")
    if ns.profile and ns.profile not in {p["dir"] for p in ccpick.list_profiles()}:
        parser.error("Unknown Chrome profile; run ccpick list.")
    env = dict(os.environ, BROWSER=launcher)
    if ns.profile:
        env["CCPICK_PROFILE"] = ns.profile
    argv = [executable, "auth", "login"]
    if ns.email:
        argv += ["--email", ns.email]
    return subprocess.run(argv, env=env).returncode


def main(argv: list[str] | None = None) -> int:
    runtime.configure_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if args[0] == "--version":
        print(__version__)
        return 0
    cmd, tail = args[0], args[1:]
    try:
        if cmd == "setup":
            from .setup import cmd_setup
            return cmd_setup(tail)
        if cmd == "uninstall":
            from .setup import cmd_uninstall
            return cmd_uninstall(tail)
        if cmd == "autoswitch":
            from .service import cmd_autoswitch
            return cmd_autoswitch(tail)
        if cmd == "doctor":
            return doctor(tail)
        if cmd in ("accounts", "status", "switch", "add", "disable", "enable", "remove"):
            return backend.run(["list" if cmd == "accounts" else cmd, *tail]).returncode
        runtime.bootstrap()
        if cmd == "list":
            import ccpick
            return ccpick.cmd_list(tail)
        if cmd in ("usage", "quota"):
            from ccpick_usage import cmd_usage
            return cmd_usage(tail)
        if cmd == "auto":
            from ccpick_auto import cmd_auto
            return cmd_auto(tail)
        if cmd == "login":
            return _login(tail)
        if cmd == "enroll":
            from ccpick_enroll import cmd_enroll
            return cmd_enroll(tail)
        if cmd in ("auto-enroll", "autoenroll"):
            from ccpick_enroll import cmd_auto_enroll
            return cmd_auto_enroll(tail)
        if cmd in ("auto-enroll-all", "autoenrollall"):
            from ccpick_enroll import cmd_auto_enroll_all
            return cmd_auto_enroll_all(tail)
        if cmd == "enable-js-gate":
            from ccpick_js_gate import cmd_enable_js_gate
            return cmd_enable_js_gate(tail)
        if cmd == "--url-stdin":
            import ccpick
            return ccpick.handle_url(sys.stdin.readline().rstrip("\n"))
        if urlparse(cmd).scheme.lower() in ("http", "https") and len(args) == 1:
            import ccpick
            if (sys.platform in ("darwin", "win32") and not os.environ.get(ccpick.ENV_DETACHED_PICKER)
                    and not os.environ.get(ccpick.ENV_PROFILE) and ccpick.is_claude_auth_url(cmd)
                    and ccpick.list_profiles() and ccpick.chrome_binary()):
                return 0 if ccpick._detach_picker(cmd) else 1
            return ccpick.handle_url(cmd)
        print("Unknown command. Run ccpick --help.", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as exc:
        print(f"ccpick: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
