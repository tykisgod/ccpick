"""Invoke the pinned backend in the same Python environment as ccpick."""

from importlib import metadata
from pathlib import Path
import os
import subprocess
import sys
import sysconfig

REQUIRED_VERSION = "0.26.0"


def version() -> str | None:
    try:
        return metadata.version("claude-swap")
    except metadata.PackageNotFoundError:
        return None


def require_backend() -> None:
    installed = version()
    if installed != REQUIRED_VERSION:
        raise RuntimeError(
            f"ccpick requires claude-swap=={REQUIRED_VERSION} in this Python "
            f"environment (found {installed or 'none'}). Reinstall ccpick."
        )


def command(args: list[str]) -> list[str]:
    require_backend()
    return [sys.executable, "-m", "claude_swap", *args]


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command(args), **kwargs)


def executable() -> str | None:
    """Legacy helpers need a single executable; never resolve cswap via PATH."""
    if version() != REQUIRED_VERSION:
        return None
    suffix = ".exe" if sys.platform == "win32" else ""
    scripts = Path(sysconfig.get_path("scripts"))
    for name in ("cswap", "claude-swap"):
        candidate = scripts / (name + suffix)
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate.resolve())
    return None
