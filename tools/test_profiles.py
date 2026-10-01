"""Run portable runtime regressions with a disposable home and local fixtures."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("Node.js 22+ is required for runtime regression checks.")
        return 1
    version = subprocess.run([node, "--version"], text=True, capture_output=True, check=True)
    if int(version.stdout.strip().lstrip("v").split(".")[0]) < 22:
        print("Node.js 22+ is required for runtime regression checks.")
        return 1
    import ccpick_app

    runtime = Path(ccpick_app.__file__).parent / "profile_runtime"
    tests = sorted((runtime / "test").glob("*.test.mjs"))
    if not tests:
        print("Portable runtime tests are missing from the package.")
        return 1
    with tempfile.TemporaryDirectory(prefix="ccpick-profiles-tests-") as temporary:
        home = Path(temporary)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(("CLAUDE", "ANTHROPIC", "TYK_", "CCPICK", "CSWAP"))}
        environment.update(HOME=temporary, USERPROFILE=temporary,
                           LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming"),
                           XDG_DATA_HOME=str(home / "data"), CCPICK_DATA_DIR=str(home / "ccpick"),
                           CLAUDE_CONFIG_DIR=str(home / "claude"))
        # Native probes and real Claude execution are deliberately outside this
        # suite. All transport tests use loopback servers or injected mocks.
        return subprocess.run([node, "--test", *map(str, tests)],
                              cwd=runtime, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
