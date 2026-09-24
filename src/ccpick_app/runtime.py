"""Public package paths and read-only account eligibility checks."""

import json
import os
from pathlib import Path
import sys
import sysconfig


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


def account_enabled(slot: str, email: str = "", sequence: Path | None = None) -> bool:
    """Fail closed for automated selection when the authoritative roster is unreadable."""
    try:
        accounts = json.loads((sequence or backend_data_dir() / "sequence.json").read_text(
            encoding="utf-8"
        ))["accounts"]
        record = accounts[str(slot)]
        if not isinstance(record, dict) or record.get("disabled"):
            return False
        expected = str(record.get("email") or "").strip().casefold()
        actual = str(email or "").strip().casefold()
        return bool(expected and actual and expected == actual)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def redact_auth_diagnostic(value: object, limit: int = 500) -> str:
    import re
    text = " ".join(str(value or "").split())
    if re.search(r"https?://|oauth|(?:state|code|token|challenge)\s*[=:]", text, re.I):
        return "[redacted]"
    return text[:limit]
