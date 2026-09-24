"""Integration plans are exercised only against temporary homes and mocked OS services."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ccpick_app import service, setup

PACKAGE = Path(setup.__file__).parent
LEGACY = PACKAGE / "legacy"
if not LEGACY.exists():
    # Run directly in the private monorepo before the exporter has copied legacy resources.
    LEGACY = Path(__file__).resolve().parents[2]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccpick-public-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.home / "Application Support" / "ccpick"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(setup.Path, "home", return_value=self.home))
        self.stack.enter_context(patch.object(setup.runtime, "data_dir", return_value=self.data))
        self.stack.enter_context(patch.object(setup.runtime, "launcher_path", return_value=str(self.root / "env" / "ccpick")))
        self.stack.enter_context(patch.object(setup, "_require_installed_runtime"))
        self.stack.enter_context(patch.object(setup, "_legacy_installation", return_value=False))
        self.conflicts = self.stack.enter_context(patch.object(setup, "legacy_services", return_value=[]))
        self.stack.enter_context(patch.dict(os.environ, {
            "SHELL": "/bin/zsh",
            "ZDOTDIR": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
        }, clear=False))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_browser_dry_run_writes_nothing(self):
        with patch.object(setup.platform, "system", return_value="Darwin"):
            self.assertEqual(setup.setup(dry_run=True), 0)
        self.assertFalse(self.data.exists())
        self.assertFalse((self.home / ".zshrc").exists())

    def test_shell_hook_is_idempotent_and_preserves_existing_configuration(self):
        path = self.home / ".zshrc"
        original = "export BROWSER=previous-browser\nexport EDITOR=vim\n"
        path.write_text(original, encoding="utf-8")
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup, "_stop_public") as stop:
            setup.setup()
            setup.setup()
            self.assertEqual(path.read_text(encoding="utf-8").count(setup.BEGIN_MARKER), 1)
            path.write_text(path.read_text(encoding="utf-8") + "# added later\n", encoding="utf-8")
            setup.uninstall()
            self.assertEqual(path.read_text(encoding="utf-8"), original + "# added later\n")
            stop.assert_called_once_with(uninstall=True)

    def test_edited_shell_hook_is_not_removed(self):
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup, "_stop_public"):
            setup.setup()
            path = self.home / ".zshrc"
            content = path.read_text(encoding="utf-8").replace("export BROWSER=", "export BROWSER=user-edited-")
            path.write_text(content, encoding="utf-8")
            setup.uninstall()
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_windows_browser_previous_value_survives_repeated_setup(self):
        browser = {"value": "previous-browser"}
        def set_value(value):
            browser["value"] = value
        with patch.object(setup.platform, "system", return_value="Windows"), \
                patch.object(setup, "_windows_browser", side_effect=lambda: browser["value"]), \
                patch.object(setup, "_set_windows_browser", side_effect=set_value), \
                patch.object(setup, "_stop_public"):
            setup.setup()
            setup.setup()
            self.assertEqual(setup._read_manifest()["browser"]["previous"], "previous-browser")
            setup.uninstall()
            self.assertEqual(browser["value"], "previous-browser")

    def test_windows_uninstall_respects_later_browser_change(self):
        with patch.object(setup.platform, "system", return_value="Windows"), \
                patch.object(setup, "_windows_browser", return_value="previous-browser"), \
                patch.object(setup, "_set_windows_browser") as write, patch.object(setup, "_stop_public"):
            setup.setup()
            write.reset_mock()
            with patch.object(setup, "_windows_browser", return_value="changed-by-user"):
                setup.uninstall()
            write.assert_not_called()

    def test_slash_command_previous_bytes_restored_after_repeated_setup(self):
        path = self.home / ".claude" / "commands" / "best-account.md"
        path.parent.mkdir(parents=True)
        original = b"---\r\ndescription: original\r\n---\r\nPrevious command\r\n"
        path.write_bytes(original)
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup, "_stop_public"):
            setup.setup()
            setup.setup()
            self.assertIn(" auto --json", path.read_text(encoding="utf-8"))
            setup.uninstall()
        self.assertEqual(path.read_bytes(), original)

    def test_modified_slash_command_survives_uninstall(self):
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup, "_stop_public"):
            setup.setup()
            path = setup._command_path()
            path.write_text("user-edited-command", encoding="utf-8")
            setup.uninstall()
        self.assertEqual(path.read_text(encoding="utf-8"), "user-edited-command")

    def test_legacy_service_requires_explicit_replacement_even_in_dry_run(self):
        self.conflicts.return_value = ["ccpick-autoswitch-tray"]
        with patch.object(setup.platform, "system", return_value="Windows"), patch.object(setup, "_disable_legacy") as disable:
            with self.assertRaisesRegex(RuntimeError, "replace-legacy"):
                setup.setup(autoswitch=True, dry_run=True)
            disable.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_replace_legacy_dry_run_does_not_stop_services(self):
        self.conflicts.return_value = ["ccpick-autoswitch-tray"]
        with patch.object(setup.platform, "system", return_value="Windows"), \
                patch.object(setup, "_windows_assets", return_value={"tray.ps1": "# fixture"}), \
                patch.object(setup, "_disable_legacy") as disable, \
                patch.object(setup, "_register_windows") as register:
            self.assertEqual(setup.setup(autoswitch=True, replace_legacy=True, dry_run=True), 0)
            disable.assert_not_called()
            register.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_uninstall_dry_run_preserves_manifest_and_account_files(self):
        with patch.object(setup.platform, "system", return_value="Darwin"), patch.object(setup, "_stop_public") as stop:
            setup.setup()
            account = self.data / "accounts" / "credential-fixture.json"
            account.parent.mkdir()
            account.write_text("fixture", encoding="utf-8")
            manifest = setup._manifest_path().read_bytes()
            setup.uninstall(dry_run=True)
            stop.assert_not_called()
            self.assertEqual(setup._manifest_path().read_bytes(), manifest)
            setup.uninstall()
            self.assertEqual(account.read_text(encoding="utf-8"), "fixture")

    def test_modified_asset_fails_before_installation(self):
        with self.assertRaisesRegex(RuntimeError, "asset changed"):
            setup._replace_once("unexpected", "original", "new")

    def test_windows_assets_pin_interpreter_and_isolate_state(self):
        assets = setup._windows_assets(LEGACY, self.data / "services", self.data / "autoswitch")
        tray = assets["claude-autoswitch-tray.ps1"]
        self.assertIn("Local\\ccpick-public-tray", tray)
        self.assertNotIn('Global\\ccpick-autoswitch-tray', tray)
        self.assertNotIn('Join-Path $env:LOCALAPPDATA "ccpick-autoswitch"', tray)
        self.assertIn(setup._ps_quote(sys.executable), tray)
        self.assertIn("-m ccpick_app switch", tray)
        self.assertNotIn('Join-Path $env:USERPROFILE ".local\\bin\\cswap.exe"', tray)
        self.assertIn("CCPICK_DATA_DIR", tray)

    def test_mac_assets_and_plists_have_no_private_executable_or_state_paths(self):
        root, state = self.data / "services", self.data / "autoswitch"
        assets = setup._mac_assets(LEGACY, root, state)
        swift = assets["menubar-main.swift"]
        self.assertNotIn('p.launchPath = "/usr/bin/python3"', swift)
        self.assertNotIn('let cswap = ("~/.local/bin/cswap"', swift)
        self.assertIn('"-m", "ccpick_app", "switch", email', swift)
        for label, plist in setup._mac_plists(root, state).items():
            self.assertTrue(label.startswith("io.github.tykisgod.ccpick."))
            self.assertEqual(plist["EnvironmentVariables"]["CCPICK_DATA_DIR"], str(self.data))
            self.assertTrue(all(".git" not in Path(arg).parts for arg in plist["ProgramArguments"]))
        self.assertEqual(setup._mac_plists(root, state)[setup.PUBLIC_LABELS[1]]["ProgramArguments"][0], sys.executable)

    @unittest.skipUnless(platform.system() == "Windows", "PowerShell syntax check runs on Windows")
    def test_generated_powershell_and_registration_parse_without_execution(self):
        assets = setup._windows_assets(LEGACY, self.data / "services", self.data / "autoswitch")
        scripts = []
        with patch.object(setup, "_powershell", side_effect=lambda script, **kw: scripts.append(script)):
            setup._register_windows(self.data / "services")
            setup._stop_public(uninstall=True)
        scripts += [value for name, value in assets.items() if name.endswith(".ps1")]
        for i, script in enumerate(scripts):
            path = self.root / f"script-{i}.ps1"
            path.write_text(script, encoding="utf-8-sig")
            result = setup._powershell("$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile(" +
                                       setup._ps_quote(path) + ", [ref]$tokens, [ref]$errors); if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }")
            self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "Swift/AppKit check runs on macOS")
    def test_generated_menubar_swift_typechecks(self):
        source = setup._mac_assets(LEGACY, self.data / "services", self.data / "autoswitch")["menubar-main.swift"]
        path = self.root / "main.swift"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(["swiftc", "-typecheck", str(path)], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccpick-service-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(service.runtime, "data_dir", return_value=self.root))
        self.stack.enter_context(patch.object(service.runtime, "bootstrap", return_value=LEGACY))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_os_lock_excludes_overlapping_checks_and_releases(self):
        path = self.root / "lock"
        with service.tick_lock(path) as first:
            self.assertTrue(first)
            with service.tick_lock(path) as second:
                self.assertFalse(second)
        with service.tick_lock(path) as third:
            self.assertTrue(third)

    def test_status_preserves_live_schedule_and_non_counted_model(self):
        result = {"action": "stay", "used": 42, "windows": {"5h": 42, "7d": 30, "Fable": 99},
                  "binding": "5h", "countedWindows": ["5h", "7d"], "nextCheckS": 25,
                  "etaS": 90, "burnRate": 12, "activeEmail": "person@example.com"}
        service.write_status(result, 2)
        status = json.loads((service.state_dir() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["nextCheckS"], 25)
        self.assertFalse(status["modelCounted"])
        self.assertEqual(status["winModel"], 99)
        service.write_status({}, 1, error="fixture failure")
        status = json.loads((service.state_dir() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["win5h"], 42)
        self.assertIsNone(status["nextCheckS"])

    def test_tick_refuses_competing_private_service(self):
        with patch.object(setup, "legacy_services", return_value=["private-tray"]), \
                patch.object(service.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(service.tick(), 1)
            run.assert_not_called()
        status = json.loads((service.state_dir() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "error")

    def test_completed_stay_check_uses_package_python_and_is_successful(self):
        completed = subprocess.CompletedProcess([], 2, '{"action":"stay","used":30,"windows":{"5h":30},"nextCheckS":40}', "")
        with patch.object(setup, "legacy_services", return_value=[]), \
                patch.object(service.subprocess, "run", return_value=completed) as run:
            self.assertEqual(service.tick(), 0)
        self.assertEqual(run.call_args.args[0][:4], [sys.executable, "-m", "ccpick_app.service", "_decide"])


if __name__ == "__main__":
    unittest.main()
