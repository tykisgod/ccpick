"""Synthetic authorization checks: no real browser, profile, account, or consent."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PUBLIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PUBLIC / "src"))
from ccpick_app import backend, cli, runtime


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ccpick-authorization-source-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.legacy = Path(cls.temporary.name) / "ccpick_app" / "legacy"
        source = PUBLIC.parent
        private_source = (source / "ccpick.py").is_file()
        if not private_source:
            source = PUBLIC / "src" / "ccpick_app" / "legacy"
        names = ["ccpick.py", "ccpick_auto.py", "ccpick_usage.py", "ccpick_enroll.py",
                 "ccpick_auto_authorize.py", "ccpick_cdp.py", "ccpick_js_gate.py",
                 "ccpick_cleanup.py", "auto_authorize.ps1",
                 "autoswitch/claude-autoswitch-decide.py", "autoswitch/claude-autoswitch-helper.py"]
        for name in names:
            target = cls.legacy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        if private_source:
            load_module("prepare_authorization_tests", PUBLIC / "prepare_core.py").prepare(cls.legacy)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="ccpick-auth-case-")))
        self.user_data = self.root / "Chrome User Data"
        (self.user_data / "Default").mkdir(parents=True)
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(patch.object(runtime, "data_dir", return_value=self.root / "state"))
        self.stack.enter_context(patch.object(runtime, "backend_data_dir", return_value=self.root / "backend"))
        self.stack.enter_context(patch.object(runtime, "legacy_dir", return_value=self.legacy))
        self.stack.enter_context(patch.object(runtime, "launcher_path", return_value=str(self.root / "ccpick")))
        self.stack.enter_context(patch.object(backend, "executable", return_value=str(self.root / "cswap")))
        self.stack.enter_context(patch.dict(os.environ, {
            "CCPICK_DATA_DIR": str(self.root / "state"),
            "CCPICK_CHROME_USER_DATA_DIR": str(self.user_data),
            "CLAUDE_CONFIG_DIR": str(self.root / "isolated-config"),
            "CCPICK_AUTOPILOT_ALLOW_MANUAL": "0",
        }))
        self.run = self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No real subprocess allowed")))
        self.popen = self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("No real browser allowed")))
        self.stack.enter_context(patch.dict(sys.modules, {}))
        for name in ("ccpick", "ccpick_cleanup", "ccpick_auto_authorize", "ccpick_cdp",
                     "ccpick_js_gate", "ccpick_usage", "ccpick_enroll", "ccpick_auto"):
            module = load_module(name, self.legacy / (name + ".py"))
            sys.modules[name] = module
            setattr(self, name.removeprefix("ccpick_") if name != "ccpick" else "core", module)
        self.profile = {"dir": "Default", "name": "Synthetic profile", "account": "person@example.com"}
        self.stack.enter_context(patch.object(self.core, "list_profiles", return_value=[self.profile]))
        self.stack.enter_context(patch.object(self.core, "load_profile_accounts", return_value={}))
        self.stack.enter_context(patch.object(self.usage, "known_status", return_value={}))
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))

    def test_chrome_defaults_are_visible_with_no_user_agent_override(self):
        pipe = self.cdp.ChromePipe("synthetic-chrome", "Default", user_data_dir=self.user_data)
        self.assertFalse(pipe.headless)
        self.assertIsNone(pipe.user_agent)
        self.assertFalse(any(arg.startswith("--headless") for arg in pipe._chrome_args()))
        self.assertFalse(any(arg.startswith("--user-agent") for arg in pipe._chrome_args()))
        self.popen.assert_not_called()

    def test_explicit_headless_and_user_agent_are_forwarded_unchanged(self):
        pipe = self.cdp.ChromePipe("synthetic-chrome", "Default", user_data_dir=self.user_data,
                                  headless=True, user_agent="SyntheticBrowser/1")
        self.assertIn("--headless=new", pipe._chrome_args())
        self.assertIn("--user-agent=SyntheticBrowser/1", pipe._chrome_args())
        self.popen.assert_not_called()

    def test_invalid_user_agent_and_profile_rejected_before_startup(self):
        with self.assertRaises(ValueError):
            self.cdp.ChromePipe("synthetic-chrome", "Default", user_data_dir=self.user_data,
                                user_agent="SyntheticBrowser/1\nInjected: value")
        with self.assertRaises(ValueError):
            self.cdp.ChromePipe("synthetic-chrome", "../other-profile", user_data_dir=self.user_data)
        self.popen.assert_not_called()

    def test_existing_chrome_refused_before_process_creation(self):
        executable = self.root / "synthetic-chrome"
        executable.write_text("fixture, never executable", encoding="utf-8")
        pipe = self.cdp.ChromePipe(str(executable), "Default", user_data_dir=self.user_data,
                                  running_check=lambda directory: (True, "synthetic running instance"))
        with self.assertRaises(self.cdp.ChromeAlreadyRunning):
            pipe.open()
        self.popen.assert_not_called()

    def test_automation_preflight_does_not_authorize_or_start_chrome(self):
        with patch.object(self.core, "chrome_binary", return_value="synthetic-chrome"), \
                patch.object(self.core, "chrome_user_data_dir", return_value=self.user_data), \
                patch.object(self.cdp, "backend_status", return_value=(True, "synthetic ready")), \
                patch.object(self.cdp, "run_authorization") as authorize:
            self.assertEqual(self.auto.probe_automation_prerequisite("Default"), (True, "synthetic ready"))
            authorize.assert_not_called()
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_batch_cli_dry_run_does_not_login_switch_or_modify_profile(self):
        preferences = self.user_data / "Default" / "Preferences"
        preferences.write_text('{"fixture":true}\n', encoding="utf-8")
        before = preferences.read_bytes()
        with patch.object(self.enroll, "_managed_account_rows", return_value=([], None)), \
                patch.object(self.auto, "probe_automation_prerequisite", return_value=(True, "synthetic ready")) as probe, \
                patch.object(self.auto, "autopilot") as authorize, \
                patch.object(self.enroll, "auth_status") as status, \
                patch.object(self.auto, "switch_and_verify") as switch:
            self.assertEqual(cli.main(["auto-enroll-all", "--dry-run"]), 0)
            probe.assert_called_once_with("Default", headless=False, user_agent=None)
            authorize.assert_not_called()
            status.assert_not_called()
            switch.assert_not_called()
        self.assertEqual(preferences.read_bytes(), before)
        self.assertEqual(list((self.user_data / "Default").iterdir()), [preferences])
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_single_cli_defaults_and_explicit_flags(self):
        for extra, expected_headless, expected_ua in (([], False, None),
                (["--headless", "--user-agent", "SyntheticBrowser/1"], True, "SyntheticBrowser/1")):
            with self.subTest(flags=extra), \
                    patch.object(self.auto, "autopilot", return_value=(True, "synthetic authorization completed")) as authorize, \
                    patch.object(self.enroll, "auth_status", return_value={"loggedIn": True, "email": "person@example.com"}):
                self.assertEqual(cli.main(["auto-enroll", "--profile", "Default", "--email", "person@example.com", "--no-add", *extra]), 0)
                self.assertEqual(authorize.call_args.kwargs["headless"], expected_headless)
                self.assertEqual(authorize.call_args.kwargs["user_agent"], expected_ua)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_consent_failure_cannot_reuse_old_login_as_success(self):
        # An already logged-in same account is not evidence that this failed consent succeeded.
        with patch.object(self.auto, "autopilot", return_value=(False, "[flow-failure] error=access_denied")), \
                patch.object(self.enroll, "auth_status", return_value={"loggedIn": True, "email": "person@example.com"}):
            result = cli.main(["auto-enroll", "--profile", "Default", "--email", "person@example.com", "--no-add"])
        self.assertNotEqual(result, 0)
        self.assertNotIn("enrolled", self.output.getvalue())
        self.run.assert_not_called()

    def test_batch_consent_failure_cannot_reuse_old_login_as_success(self):
        with patch.object(self.enroll, "_managed_account_rows", return_value=([], None)), \
                patch.object(self.auto, "probe_automation_prerequisite", return_value=(True, "synthetic ready")), \
                patch.object(self.auto, "autopilot", return_value=(False, "[account-refusal] error=access_denied")), \
                patch.object(self.enroll, "auth_status", return_value={"loggedIn": True, "email": "person@example.com"}):
            result = cli.main(["auto-enroll-all", "--profiles", "Default", "--no-add"])
        self.assertNotEqual(result, 0)
        self.assertNotIn("enrolled", self.output.getvalue())
        self.run.assert_not_called()

    def test_cdp_error_callback_is_failure_even_if_child_exit_is_zero(self):
        pipe = Mock()
        pipe.target_ids.return_value = {"existing-target"}
        process = Mock()
        process.stdout = io.StringIO("")
        process.returncode = 0
        process.poll.side_effect = [None, 0]
        observation = {"target_id": "new-target", "stage": "callback_error", "error": "access_denied"}
        with patch.object(self.cdp, "_start_enroll", return_value=process), \
                patch.object(self.cdp, "scan_flow_frames", return_value=[observation]), \
                patch.object(self.cdp, "close_confirmed_flow_targets", return_value=1), \
                patch.object(self.cdp, "execute_frame_action") as click:
            success, detail = self.cdp._authorize_with_pipe(pipe, "Default", "person@example.com", 10, 6, None)
        self.assertFalse(success)
        self.assertIn("[account-refusal]", detail)
        click.assert_not_called()

    def test_authorization_diagnostics_and_event_urls_are_redacted(self):
        samples = ["https://claude.ai/oauth/authorize?state=fixture-state&code_challenge=fixture-challenge",
                   "auth code=fixture-code", "state=fixture-state"]
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(self.cdp._safe_text(value), "[redacted]")
                self.assertEqual(self.enroll._redact_auth_diagnostic(value), "[redacted]")
                self.assertNotIn("fixture-", self.auto_authorize.redact_child_line(value))
        event = self.cdp._sanitize_event({"params": {"targetInfo": {"url": samples[0]}}})
        self.assertNotIn("fixture-", json.dumps(event))
        self.assertEqual(event["params"]["targetInfo"]["url"], "[redacted-url]")

    def test_js_gate_report_is_read_only_by_default(self):
        local_state = self.user_data / "Local State"
        local_state.write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": "Synthetic"}}}}), encoding="utf-8")
        preferences = self.user_data / "Default" / "Preferences"
        preferences.write_text('{"browser":{"allow_javascript_apple_events":false}}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (local_state, preferences)}
        with patch.object(sys, "platform", "darwin"), \
                patch.object(self.js_gate, "chrome_user_data_dir", return_value=self.user_data), \
                patch.object(self.js_gate, "chrome_running") as running:
            self.assertEqual(cli.main(["enable-js-gate", "--profiles", "Default"]), 0)
            running.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(list((self.user_data / "Default").iterdir()), [preferences])
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_windows_fallback_accepts_the_pinned_public_launcher(self):
        source = (self.legacy / "auto_authorize.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$CcpickLauncher", source)
        self.assertNotIn('$cc = "$env:USERPROFILE\\.local\\bin\\ccpick.cmd"', source)


if __name__ == "__main__":
    unittest.main()
