"""Public package paths and read-only account eligibility checks."""

import json
import os
from pathlib import Path
import hashlib
import sys
import sysconfig


def configure_stdio() -> None:
    """Keep redirected CLI output and Python child output consistently UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"


def _xdg(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    candidate = Path(value).expanduser() if value else default
    return candidate if candidate.is_absolute() else default


def data_dir() -> Path:
    override = os.environ.get("CCPICK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ccpick"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ccpick"
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / "ccpick"


def backend_data_dir() -> Path:
    # Matches claude_swap.paths.get_backup_root in the pinned 0.26.0 release.
    # Reading this path does not initialize the backend or run its migrations.
    if sys.platform.startswith("linux"):
        return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / "claude-swap"
    return Path.home() / ".claude-swap-backup"


def legacy_dir() -> Path:
    return Path(__file__).resolve().parent / "legacy"


def bootstrap() -> Path:
    root = legacy_dir()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def ensure_data_dir() -> Path:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def launcher_path() -> str | None:
    candidate = Path(sysconfig.get_path("scripts")) / (
        "ccpick.exe" if sys.platform == "win32" else "ccpick"
    )
    if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
        return str(candidate.resolve()).replace("\\", "/")
    return None


def detached_creationflags() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


def powershell_path() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def account_enabled(slot: str, email: str = "", sequence: Path | None = None) -> bool:
    """Fail closed for automated selection when the authoritative roster is unreadable."""
    try:
        accounts = json.loads((sequence or backend_data_dir() / "sequence.json").read_text(
            encoding="utf-8"
        ))["accounts"]
        record = accounts[str(slot)]
        if (not isinstance(record, dict) or record.get("disabled") or
                record.get("autoSwitchEnabled") is False):
            return False
        expected = str(record.get("email") or "").strip().casefold()
        actual = str(email or "").strip().casefold()
        return bool(expected and actual and expected == actual)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def automatic_target_enabled(query: object, sequence: Path | None = None) -> bool:
    """Resolve the current roster again immediately before an automatic switch."""
    try:
        accounts = json.loads((sequence or backend_data_dir() / "sequence.json").read_text(
            encoding="utf-8-sig"))["accounts"]
        if not isinstance(accounts, dict):
            return False
        target = str(query).strip().casefold()
        matches = [(str(slot), record) for slot, record in accounts.items()
                   if isinstance(record, dict) and
                   (target == str(slot).casefold() or
                    target == str(record.get("email") or "").strip().casefold())]
        if len(matches) != 1:
            return False
        slot, record = matches[0]
        return account_enabled(slot, record.get("email"), sequence)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wait_context() -> str | None:
    """Hash only local selection and policy so stale quota waits can be cancelled."""
    try:
        from .account_context import manager
        managed = manager()
        if managed is not None:
            selection = managed.selection()
            roster = [[p["name"], (p.get("account") or {}).get("uuid"),
                       (p.get("account") or {}).get("email"),
                       p.get("autoSwitchEnabled") is not False] for p in managed.profiles()]
            identity = [selection.get("selected"), selection.get("selectedAt")]
        else:
            directory = os.environ.get("CLAUDE_CONFIG_DIR")
            if directory and not Path(directory).is_absolute():
                return None
            source = Path(directory) / ".claude.json" if directory else Path.home() / ".claude.json"
            account = json.loads(source.read_text(encoding="utf-8-sig")).get("oauthAccount") or {}
            identity = [account.get("accountUuid"), account.get("emailAddress")]
            accounts = json.loads((backend_data_dir() / "sequence.json").read_text(
                encoding="utf-8-sig"))["accounts"]
            if not any(identity) or not isinstance(accounts, dict):
                return None
            roster = [[str(slot), record.get("email"), bool(record.get("disabled")),
                       record.get("autoSwitchEnabled") is not False]
                      for slot, record in accounts.items() if isinstance(record, dict)]
        policy_file = data_dir() / "quota-policy.json"
        policy = [os.environ.get(key, "") for key in
                  ("CCSWITCH_MODELS", "CCSWITCH_THRESHOLD", "CCSWITCH_WATCH_ONLY",
                   "CCSWITCH_POLICY", "CCSWITCH_GATE_URL", "CCSWITCH_PROXY_URL")]
        policy += [(data_dir() / "autoswitch" / "claude-autoswitch.watch-only").exists(),
                   policy_file.read_text(encoding="utf-8-sig") if policy_file.exists() else None]
        value = json.dumps([identity, sorted(roster), policy], sort_keys=True).encode("utf-8")
        return hashlib.sha256(value).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None


def redact_auth_diagnostic(value: object, limit: int = 500) -> str:
    import re
    text = " ".join(str(value or "").split())
    if re.search(r"https?://|oauth|(?:state|code|token|challenge)\s*[=:]", text, re.I):
        return "[redacted]"
    return text[:limit]
