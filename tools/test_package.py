"""Run package tests without exposing the current user's account or browser data."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ccpick-package-tests-") as temp:
        home = Path(temp)
        os.environ.update(
            HOME=temp,
            USERPROFILE=temp,
            LOCALAPPDATA=str(home / "local"),
            APPDATA=str(home / "roaming"),
            XDG_DATA_HOME=str(home / "data"),
            CCPICK_DATA_DIR=str(home / "ccpick"),
            CCPICK_CHROME_USER_DATA_DIR=str(home / "chrome"),
            CLAUDE_CONFIG_DIR=str(home / "claude"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        from ccpick_app.runtime import configure_stdio

        configure_stdio()
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parents[1] / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
