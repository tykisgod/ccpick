"""Public runtime regression tests; all account and service data is synthetic."""

import ast
from contextlib import ExitStack
from contextlib import redirect_stdout
from contextlib import redirect_stderr
import inspect
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PUBLIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PUBLIC / "src"))
from ccpick_app import backend, cli, runtime


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PathsTests(unittest.TestCase):
    def test_linux_paths_match_backend_xdg_rules(self):
        home = Path.home()
        with patch.object(sys, "platform", "linux"), patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}, clear=True):
            self.assertEqual(runtime.backend_data_dir(), home / ".local/share/claude-swap")
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "platform", "linux"), patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"XDG_DATA_HOME": directory}, clear=True):
            self.assertEqual(runtime.backend_data_dir(), Path(directory) / "claude-swap")
            self.assertEqual(runtime.data_dir(), Path(directory) / "ccpick")

    def test_data_override_and_backend_are_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CCPICK_DATA_DIR": directory}), patch.object(sys, "platform", "win32"):
            self.assertEqual(runtime.data_dir(), Path(directory).resolve())
            self.assertEqual(runtime.backend_data_dir(), Path.home() / ".claude-swap-backup")

    def test_backend_is_pinned_to_current_interpreter(self):
        with patch.object(backend, "version", return_value="0.26.0"):
            self.assertEqual(backend.command(["list"]), [sys.executable, "-m", "claude_swap", "list"])
        with patch.object(backend, "version", return_value="99.0"):
            with self.assertRaises(RuntimeError):
                backend.command(["switch", "1"])

    def test_backend_never_uses_path_lookup(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(backend, "version", return_value="0.26.0"), patch.object(backend.sysconfig, "get_path", return_value=directory), patch("shutil.which", side_effect=AssertionError("PATH lookup")):
            self.assertIsNone(backend.executable())

    def test_auth_redaction_checks_before_truncation(self):
        self.assertEqual(runtime.redact_auth_diagnostic("x" * 600 + " https://example.com/callback?code=secret"), "[redacted]")
        self.assertEqual(runtime.redact_auth_diagnostic("Network unavailable"), "Network unavailable")


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.legacy = cls.root / "ccpick_app" / "legacy"
        cls.legacy.mkdir(parents=True)
        source = PUBLIC.parent
        private_source = (source / "ccpick.py").is_file()
        if not private_source:
            source = PUBLIC / "src/ccpick_app/legacy"
        names = ["ccpick.py", "ccpick_auto.py", "ccpick_usage.py", "ccpick_enroll.py",
                 "autoswitch/claude-autoswitch-decide.py", "autoswitch/claude-autoswitch-helper.py"]
        for name in names:
            (cls.legacy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, cls.legacy / name)
        if private_source:
            load("prepare_core_for_tests", PUBLIC / "prepare_core.py").prepare(cls.legacy)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime, "backend_data_dir", return_value=self.case / "backend"))
        self.stack.enter_context(patch.object(runtime, "data_dir", return_value=self.case / "state"))
        self.stack.enter_context(patch.object(runtime, "legacy_dir", return_value=self.legacy))
        self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No real process allowed")))
        self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("No real process allowed")))
        self.usage = load("public_usage_for_tests", self.legacy / "ccpick_usage.py")
        self.stack.enter_context(patch.dict(sys.modules, {"ccpick_usage": self.usage}))
        self.usage.live_identity = lambda: "active@example.com"
        self.usage.CACHE.parent.mkdir(parents=True)
        self.usage.SEQ.write_text(json.dumps({"accounts": {
            "1": {"email": "active@example.com"},
            "2": {"email": "resting@example.com", "disabled": True},
            "3": {"email": "ready@example.com"},
        }}), encoding="utf-8")
        cache = {"accounts": {}}
        for slot, email, pct in [("1", "active@example.com", 90), ("2", "resting@example.com", 1), ("3", "ready@example.com", 20)]:
            cache["accounts"][slot] = {"email": email, "fetchedAt": 1000,
                "lastGood": {"five_hour": {"pct": pct, "resets_at": "2099-01-01T00:00:00Z"}}}
        self.usage.CACHE.write_text(json.dumps(cache), encoding="utf-8")

    def test_auto_keeps_disabled_visible_but_never_selects_it(self):
        auto = load("public_auto_for_tests", self.legacy / "ccpick_auto.py")
        probe, _ = auto.probe_local(None)
        rows = auto.rank(probe, auto.slot_meta())
        self.assertEqual(rows[0]["email"], "ready@example.com")
        disabled = next(row for row in rows if row["slot"] == "2")
        self.assertFalse(disabled["usable"])
        self.assertIn("disabled", disabled["why_bad"])

    def test_autoswitch_never_selects_disabled_account(self):
        decide = load("public_decide_for_tests", self.legacy / "autoswitch/claude-autoswitch-decide.py")
        rows = decide.rows_from_cache()
        self.assertEqual(len(rows), 3)
        self.assertTrue(next(row for row in rows if row["slot"] == "2")["disabled"])
        choice, _ = decide.pick(rows, {"active@example.com"})
        self.assertEqual(choice["email"], "ready@example.com")

    def test_missing_or_changed_roster_fails_closed(self):
        self.assertFalse(runtime.account_enabled("2", "resting@example.com"))
        self.assertFalse(runtime.account_enabled("3", "somebody@example.com"))
        self.usage.SEQ.write_text("invalid json", encoding="utf-8")
        self.assertFalse(runtime.account_enabled("3", "ready@example.com"))
        self.assertTrue(all(row["disabled"] for row in self.usage.collect()))

    def test_state_is_outside_package(self):
        core = load("public_core_for_tests", self.legacy / "ccpick.py")
        core.record_profile_account("Default", "profile@example.com")
        core.note_login_profile("Default")
        self.assertTrue((self.case / "state/profile-accounts.json").is_file())
        self.assertEqual(core.claim_login_profile(), "Default")
        self.assertFalse((self.legacy / "profile-accounts.json").exists())

    def test_identity_lookup_is_bounded_and_cached_helper_never_forks(self):
        fresh_usage = load("bounded_usage_for_tests", self.legacy / "ccpick_usage.py")
        timeout = inspect.signature(fresh_usage.live_identity).parameters["timeout"].default
        self.assertLessEqual(timeout, 5.0)
        status = self.case / "state/autoswitch/status.json"
        status.parent.mkdir(parents=True)
        status.write_text(json.dumps({"ts": time.time(), "activeEmail": "active@example.com"}), encoding="utf-8")
        helper = load("public_helper_for_tests", self.legacy / "autoswitch/claude-autoswitch-helper.py")
        output = io.StringIO()
        with patch("subprocess.run", side_effect=AssertionError("Must not fork")) as process, redirect_stdout(output):
            self.assertEqual(helper.cmd_accounts([]), 0)
        process.assert_not_called()
        rows = json.loads(output.getvalue())["accounts"]
        self.assertEqual(next(row["email"] for row in rows if row["active"]), "active@example.com")

    def test_export_paths_have_no_private_install_fallback(self):
        helper = load("paths_helper_for_tests", self.legacy / "autoswitch/claude-autoswitch-helper.py")
        decide = load("paths_decide_for_tests", self.legacy / "autoswitch/claude-autoswitch-decide.py")
        self.assertEqual(helper.CCPICK, self.legacy)
        self.assertEqual(decide.CCPICK_DIR, self.legacy)
        for filename in ("ccpick.py", "ccpick_usage.py", "ccpick_enroll.py"):
            source = (self.legacy / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.BinOp):
                    self.assertNotEqual(ast.unparse(node), "Path.home() / '.claude' / 'tools' / 'ccpick'")

    def test_export_omits_automated_authorization(self):
        banned = {"ccpick_auto_authorize", "ccpick_cdp", "ccpick_js_gate"}
        for path in self.legacy.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, banned, str(path))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, banned, str(path))
                if isinstance(node, ast.FunctionDef):
                    self.assertNotIn(node.name, {"autopilot", "cmd_auto_enroll", "cmd_auto_enroll_all"})

    def test_removed_commands_cannot_be_interpreted_as_urls(self):
        for command in ("auto-enroll", "auto-enroll-all", "enable-js-gate"):
            self.assertEqual(cli.main([command]), 2)

    def test_public_command_help_imports_without_authorization(self):
        saved = {}
        for name, file in (("ccpick", "ccpick.py"), ("ccpick_auto", "ccpick_auto.py"), ("ccpick_enroll", "ccpick_enroll.py")):
            saved[name] = load("help_" + name, self.legacy / file)
        with patch.dict(sys.modules, saved), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for command in ("usage", "auto", "login", "enroll", "doctor", "setup", "uninstall", "autoswitch"):
                with self.assertRaises(SystemExit) as result:
                    cli.main([command, "--help"])
                self.assertEqual(result.exception.code, 0, command)
            for flags in (("auto", "--enroll"), ("enroll", "--headless"),
                          ("enroll", "--user-agent", "example"),
                          ("enroll", "--config-dir", str(self.case), "--add")):
                with self.assertRaises(SystemExit) as result:
                    cli.main(list(flags))
                self.assertEqual(result.exception.code, 2, flags)

    def test_noninteractive_json_is_parseable_and_dry_run_does_not_switch(self):
        auto = load("json_auto_for_tests", self.legacy / "ccpick_auto.py")
        enroll = load("json_enroll_for_tests", self.legacy / "ccpick_enroll.py")
        enroll.cswap_bin = lambda: None
        with patch.dict(sys.modules, {"ccpick_auto": auto, "ccpick_enroll": enroll}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["auto", "--no-refresh", "--dry-run", "--json"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["action"], "dry-run")
            self.assertEqual(result["to"], "ready@example.com")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["usage", "--json"]), 0)
            rows = json.loads(output.getvalue())
            self.assertTrue(next(row for row in rows if row["slot"] == "2")["disabled"])

    def test_windows_picker_detaches_without_copying_oauth_url_to_argv(self):
        core = load("detached_core_for_tests", self.legacy / "ccpick.py")
        process = type("Child", (), {"stdin": io.StringIO(), "pid": 123})()
        url = "https://claude.ai/oauth/authorize?state=synthetic"
        with patch.object(sys, "platform", "win32"), patch("subprocess.Popen", return_value=process) as spawn:
            self.assertTrue(core._detach_picker(url))
        args, options = spawn.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "ccpick_app", "--url-stdin"])
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertNotIn(url, args[0])


if __name__ == "__main__":
    unittest.main()
