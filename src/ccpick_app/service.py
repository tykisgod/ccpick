"""Public autoswitch runner; the packaged decision engine remains the authority."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

from . import runtime


def state_dir() -> Path:
    return runtime.data_dir() / "autoswitch"


@contextmanager
def tick_lock(path: Path):
    """An OS lock is released on crashes; no stale PID or recursive deletion needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            # Reading a byte locked by another process fails on Windows; query its size instead.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def _helper():
    path = runtime.bootstrap() / "autoswitch" / "claude-autoswitch-helper.py"
    spec = importlib.util.spec_from_file_location("_ccpick_public_helper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Packaged autoswitch helper is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_status(result: dict, returncode: int, *, error: str = "") -> None:
    """Translate the engine's JSON to the existing tray/menu-bar status schema."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    threshold = str(result.get("consider", os.environ.get("CCSWITCH_THRESHOLD", "90")))
    state = {0: "switched", 2: "ok", 3: "blocked"}.get(returncode, "error")
    message = {"switched": "Account switched", "ok": "Monitoring",
               "blocked": "No account with usable quota", "error": "Autoswitch failed"}[state]
    extra = result.get("why", "") if state != "ok" else ""
    if result.get("switchFailed"):
        message, extra = "Switch failed; current account retained", result.get("why", "")
    if result.get("action") == "would-switch":
        state, message = "ok", "Dry run: would switch accounts"
    if error:
        extra = error
    windows = result.get("windows")
    window_text = ""
    schedule_text = ""
    def token(value):
        return "-" if value is None else str(value).replace("|", "")
    if isinstance(windows, dict):
        model = next((key for key in windows if key not in ("5h", "7d")), "")
        counted = result.get("countedWindows")
        model_counted = "" if counted is None else str(int(model in counted))
        window_text = "|".join([
            token(result.get("used", result.get("toUsed"))), token(windows.get("5h")),
            token(windows.get("7d")), token(windows.get(model)), "0",
            str(result.get("binding") or ""), model, model_counted,
        ])
    if any(result.get(key) is not None for key in ("nextCheckS", "etaS", "burnRate", "activeEmail")):
        schedule_text = "|".join("" if result.get(key) is None else token(result[key])
                                 for key in ("nextCheckS", "etaS", "burnRate", "activeEmail"))
    rc = _helper().cmd_write([str(root / "status.json"), state, message, str(extra)[:240],
                             threshold, window_text, schedule_text])
    if rc:
        raise RuntimeError("Cannot write autoswitch status")
    # The reused Windows tray observes these stamps and performs its own notification throttling.
    marker = root / ".notified"
    failure_marker = root / ".notified.switchfail"
    for path, needed in ((marker, state == "blocked"),
                         (failure_marker, bool(result.get("switchFailed")))):
        if needed:
            try:
                previous = float(path.read_text(encoding="ascii"))
            except (OSError, ValueError):
                previous = 0
            if time.time() - previous >= 3600:
                path.write_text(str(int(time.time())), encoding="ascii")
        elif path.exists():
            path.unlink()


def _log(message: str) -> None:
    path = state_dir() / "autoswitch.log"
    if path.exists() and path.stat().st_size > 2_000_000:
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
        path.write_text("\n".join(tail) + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def tick(*, dry_run: bool = False) -> int:
    from .setup import legacy_services
    runtime.bootstrap()
    with tick_lock(state_dir() / ".tick.lock") as acquired:
        if not acquired:
            return 0
        try:
            conflicts = legacy_services()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = "Cannot verify legacy service status: " + type(exc).__name__
            write_status({}, 1, error=error)
            _log(error)
            print(error, file=sys.stderr)
            return 1
        if conflicts:
            error = "Private autoswitch is still enabled; run setup --autoswitch --replace-legacy."
            write_status({}, 1, error=error)
            _log(error)
            print(error, file=sys.stderr)
            return 1
        command = [sys.executable, "-m", "ccpick_app.service", "_decide"]
        if dry_run:
            command.append("--dry-run")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", stdin=subprocess.DEVNULL, timeout=180, **kwargs)
            try:
                result = json.loads(process.stdout)
            except ValueError as exc:
                diagnostic = runtime.redact_auth_diagnostic(process.stderr or process.stdout)
                raise ValueError("Decision engine did not return JSON: " + diagnostic) from exc
            if not isinstance(result, dict):
                raise ValueError("Decision engine returned a non-object result")
            write_status(result, process.returncode)
            _log(json.dumps(result, ensure_ascii=False))
            print(json.dumps(result, ensure_ascii=False))
            # stay/blocked are successful completed checks, not crashed services.
            return 0 if process.returncode in (0, 2, 3) else 1
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            error = "Decision check timed out" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            write_status({}, 1, error=error)
            _log("ERROR " + error)
            print(error, file=sys.stderr)
            return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("_decide", "_helper"):
        filename = "claude-autoswitch-decide.py" if argv[0] == "_decide" else "claude-autoswitch-helper.py"
        path = runtime.bootstrap() / "autoswitch" / filename
        sys.argv = [str(path), *argv[1:]]
        runpy.run_path(str(path), run_name="__main__")
        return 0
    parser = argparse.ArgumentParser(prog="ccpick autoswitch")
    parser.add_argument("command", choices=["tick"])
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without switching (may refresh usage cache)")
    args = parser.parse_args(argv)
    return tick(dry_run=args.dry_run)


cmd_autoswitch = main


if __name__ == "__main__":
    raise SystemExit(main())
