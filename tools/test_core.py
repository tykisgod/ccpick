"""Run the exported decision regression suite using a disposable user home."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ccpick-tests-") as temp:
        root = Path(temp)
        os.environ.update(HOME=temp, USERPROFILE=temp, LOCALAPPDATA=str(root / "local"),
                          APPDATA=str(root / "roaming"), XDG_DATA_HOME=str(root / "data"),
                          CCPICK_DATA_DIR=str(root / "ccpick"),
                          CCPICK_CHROME_USER_DATA_DIR=str(root / "chrome"),
                          CLAUDE_CONFIG_DIR=str(root / "claude"),
                          PYTHONDONTWRITEBYTECODE="1")
        from ccpick_app.runtime import bootstrap, configure_stdio
        import ccpick_app
        configure_stdio()
        bootstrap()
        test = Path(ccpick_app.__file__).parent / "legacy" / "autoswitch" / "selftest_pace.py"
        spec = importlib.util.spec_from_file_location("ccpick_pace_tests", test)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The public package intentionally has no loose-file installation or
        # fallback to a private checkout. Tests/test_runtime.py verifies the
        # package paths and cached identity instead of those retired layouts.
        retired_layout_checks = (
            "check_load_usage_rejects_stale_module",
            "check_load_usage_from_home_bin_copy",
            "check_helper_loads_sibling_usage",
        )
        for name in retired_layout_checks:
            def skip(_temp, name=name):
                module._SKIPPED.append(name + ": private loose-file layout replaced by package regression tests")
                return 0
            setattr(module, name, skip)
        return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
