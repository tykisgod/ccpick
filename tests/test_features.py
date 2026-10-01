"""Portable switching policy regressions with synthetic account data only."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import subprocess

from ccpick_app import account_context, runtime, runtime_decision, service

PUBLIC = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticManager:
    def __init__(self):
        self.state = {"selected": "account-a", "selectedAt": "2026-01-01T00:00:00Z"}
        self.records = [
            {"name": "account-a", "account": {"email": "alpha@example.com"},
             "autoSwitchEnabled": True, "plan": {"scale": 5}},
            {"name": "account-b", "account": {"email": "beta@example.com"},
             "autoSwitchEnabled": True, "plan": {"scale": 5}},
        ]
        self.calls = []
        self.on_refresh = None
        self.after_select = None
        self.bad_receipt = False
        self.missing_window = False

    def selection(self):
        return dict(self.state)

    def profiles(self):
        return [dict(profile) for profile in self.records]

    def find(self, query):
        return next(profile for profile in self.profiles() if query in
                    (profile["name"], profile["account"]["email"]))

    def current(self):
        return {**self.find(self.state["selected"]), "selectedAt": self.state["selectedAt"]}

    def refresh(self, **kwargs):
        if self.on_refresh:
            self.on_refresh(**kwargs)

    def collect(self):
        rows = []
        for index, profile in enumerate(self.records):
            windows = {"5h": {"pct": 98 if index == 0 else 10, "resets_at": "2099-01-01T00:00:00Z"},
                       "7d": {"pct": 20, "resets_at": "2099-01-01T00:00:00Z"}}
            if self.missing_window and index == 0:
                windows["5h"] = {"pct": None, "expired": True}
            rows.append({"slot": profile["name"], "profile": profile["name"],
                         "email": profile["account"]["email"], "active": profile["name"] == self.state["selected"],
                         "windows": windows, "fetched_at": time.time(), "error": None})
        return rows

    def select(self, query, *, allow_login, expected_state):
        self.calls.append((query, allow_login, dict(expected_state)))
        if expected_state != self.state:
            raise RuntimeError("selection_changed")
        profile = self.find(query)
        self.state = {"selected": profile["name"], "selectedAt": "2026-01-01T00:00:01Z"}
        receipt = {"profileId": profile["name"], "generation": self.state["selectedAt"]}
        if self.after_select:
            self.after_select()
        if self.bad_receipt:
            return profile
        return {**profile, "selectionReceipt": receipt}


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ccpick-public-feature-resources-")
        cls.legacy = Path(cls.temporary.name) / "ccpick_app/legacy"
        cls.legacy.mkdir(parents=True)
        source = PUBLIC.parent
        private = (source / "ccpick.py").is_file()
        if not private:
            source = PUBLIC / "src/ccpick_app/legacy"
        names = ["ccpick.py", "ccpick_auto.py", "ccpick_usage.py", "ccpick_enroll.py",
                 "ccpick_coordination.py", "ccpick_auto_authorize.py", "ccpick_cdp.py",
                 "ccpick_js_gate.py", "ccpick_cleanup.py", "auto_authorize.ps1",
                 "autoswitch/claude-autoswitch-decide.py", "autoswitch/claude-autoswitch-helper.py",
                 "autoswitch/menubar-main.swift", "autoswitch/win/claude-autoswitch-tray.ps1"]
        for name in names:
            target = cls.legacy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        if private:
            load("public_prepare_feature_tests", PUBLIC / "prepare_core.py").prepare(cls.legacy)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="ccpick-public-feature-case-")))
        self.stack.enter_context(patch.object(runtime, "data_dir", return_value=self.case / "state"))
        self.stack.enter_context(patch.object(runtime, "backend_data_dir", return_value=self.case / "backend"))
        self.stack.enter_context(patch.object(runtime, "legacy_dir", return_value=self.legacy))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.case / "home"))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No native command allowed")))
        self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("No native command allowed")))
        (self.case / "backend").mkdir()

    def roster(self, disabled=False):
        path = runtime.backend_data_dir() / "sequence.json"
        path.write_text(json.dumps({"accounts": {"1": {"email": "alpha@example.com", "disabled": disabled}}}), encoding="utf-8")
        return path

    def test_automatic_commit_rechecks_manual_only_and_removal(self):
        self.roster()
        self.assertTrue(runtime.automatic_target_enabled("ALPHA@example.com"))
        self.roster(disabled=True)
        self.assertFalse(runtime.automatic_target_enabled("1"))
        self.assertFalse(runtime.automatic_target_enabled("alpha@example.com"))
        self.assertFalse(runtime.automatic_target_enabled("missing@example.com"))
        (runtime.backend_data_dir() / "sequence.json").write_text("broken", encoding="utf-8")
        self.assertFalse(runtime.automatic_target_enabled("1"))

    def test_decider_commit_does_not_call_backend_for_disabled_or_removed_target(self):
        self.roster(disabled=True)
        self.stack.enter_context(patch.object(account_context, "manager", return_value=None))
        decide = load("public_policy_boundary", self.legacy / "autoswitch/claude-autoswitch-decide.py")
        from ccpick_app import backend
        with patch.object(backend, "run", side_effect=AssertionError("disabled target")):
            self.assertIsNone(decide._cswap(["switch", "1"]))
            self.assertIsNone(decide._cswap(["switch", "unknown@example.com"]))

    def test_wait_context_changes_when_policy_or_selected_generation_changes(self):
        managed = SyntheticManager()
        with patch.object(account_context, "manager", return_value=managed):
            original = runtime.wait_context()
            self.assertIsNotNone(original)
            managed.records[1]["autoSwitchEnabled"] = False
            self.assertNotEqual(runtime.wait_context(), original)
            policy = runtime.wait_context()
            managed.state["selectedAt"] = "2026-01-01T00:00:02Z"
            self.assertNotEqual(runtime.wait_context(), policy)

    def policy(self):
        self.stack.enter_context(patch.object(account_context, "manager", return_value=None))
        return runtime_decision.load_policy()

    def test_runtime_predictive_switch_uses_guarded_committed_receipt(self):
        managed = SyntheticManager()
        result = runtime_decision.evaluate(managed, self.policy(), refresh=False)
        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["activeEmail"], "beta@example.com")
        self.assertEqual(managed.calls[0], ("beta@example.com", False,
                         {"selected": "account-a", "selectedAt": "2026-01-01T00:00:00Z"}))

    def test_runtime_cannot_overwrite_manual_selection_during_refresh(self):
        managed = SyntheticManager()
        managed.on_refresh = lambda **_kwargs: managed.state.update(selectedAt="2026-01-01T00:00:03Z")
        with self.assertRaisesRegex(RuntimeError, "account_selection_changed"):
            runtime_decision.evaluate(managed, self.policy())
        self.assertEqual(managed.calls, [])

    def test_runtime_cannot_adopt_later_manual_choice_as_automatic_receipt(self):
        managed = SyntheticManager()
        managed.after_select = lambda: managed.state.update(selected="account-a", selectedAt="2026-01-01T00:00:04Z")
        with self.assertRaisesRegex(RuntimeError, "account_selection_changed"):
            runtime_decision.evaluate(managed, self.policy(), refresh=False)
        self.assertEqual(len(managed.calls), 1)

    def test_runtime_failed_authorization_does_not_publish_or_drop_selection_guard(self):
        managed = SyntheticManager()
        def failed(query, *, allow_login, expected_state):
            managed.calls.append((query, allow_login, dict(expected_state)))
            raise RuntimeError("login_required")
        managed.select = failed
        result = runtime_decision.evaluate(managed, self.policy(), refresh=False)
        self.assertNotEqual(result["action"], "switched")
        self.assertEqual(result["activeEmail"], "alpha@example.com")
        self.assertEqual(result["targetFailures"], ["login_required"])
        self.assertEqual(managed.calls[0][2], {"selected": "account-a", "selectedAt": "2026-01-01T00:00:00Z"})

    def test_runtime_requires_receipt_instead_of_reading_mutable_selection(self):
        managed = SyntheticManager()
        managed.bad_receipt = True
        with self.assertRaisesRegex(RuntimeError, "account_selection_invalid"):
            runtime_decision.evaluate(managed, self.policy(), refresh=False)

    def test_runtime_manual_only_and_dry_run_keep_selection(self):
        managed = SyntheticManager()
        managed.records[1]["autoSwitchEnabled"] = False
        result = runtime_decision.evaluate(managed, self.policy(), refresh=False)
        self.assertNotEqual(result["action"], "switched")
        self.assertEqual(managed.calls, [])
        managed.records[1]["autoSwitchEnabled"] = True
        result = runtime_decision.evaluate(managed, self.policy(), refresh=False, dry_run=True)
        self.assertEqual(result["action"], "would-switch")
        self.assertEqual(result["activeEmail"], "alpha@example.com")
        self.assertEqual(managed.calls, [])

    def test_runtime_missing_window_does_not_infer_zero_usage(self):
        managed = SyntheticManager()
        managed.missing_window = True
        result = runtime_decision.evaluate(managed, self.policy(), refresh=False)
        self.assertEqual(result["action"], "stay")
        self.assertFalse(result["fresh"])
        self.assertEqual(managed.calls, [])
        self.assertNotIn("5h", result["windows"])

    def test_runtime_freshens_only_stale_requested_candidate(self):
        managed = SyntheticManager()
        original = managed.collect
        fetched = {"account-a": time.time(), "account-b": time.time() - 1000}
        refreshes = []
        def collect():
            rows = original()
            for row in rows:
                row["fetched_at"] = fetched[row["profile"]]
                row["nextPollAt"] = time.time() + 3600
            return rows
        def refresh(**kwargs):
            refreshes.append(kwargs)
            for slot in kwargs.get("slots", []):
                fetched[slot] = time.time()
        managed.collect = collect
        managed.on_refresh = refresh
        result = runtime_decision.evaluate(managed, self.policy())
        self.assertEqual(result["action"], "switched")
        self.assertEqual(refreshes, [{"budget_s": 30}, {"force": True, "slots": ["account-b"], "budget_s": 2}])

    def test_runtime_model_override_counts_scoped_window_and_restores_environment(self):
        managed = SyntheticManager()
        original = managed.collect
        def collect():
            rows = original()
            for row in rows:
                row["windows"]["Nimbus"] = {"pct": 100, "resets_at": "2099-01-01T00:00:00Z"}
            return rows
        managed.collect = collect
        output = io.StringIO()
        with patch.dict(os.environ, {"CCSWITCH_MODELS": "none"}), redirect_stdout(output):
            code = runtime_decision.run(managed, ["--model", "all", "--no-refresh", "--json"])
            self.assertEqual(os.environ["CCSWITCH_MODELS"], "none")
        result = json.loads(output.getvalue())
        self.assertIn("Nimbus", result["countedWindows"])
        self.assertEqual(managed.calls, [])
        self.assertEqual(code, 3)

    def test_runtime_force_cannot_bypass_watch_only(self):
        managed = SyntheticManager()
        with patch.dict(os.environ, {"CCSWITCH_WATCH_ONLY": "1"}):
            result = runtime_decision.evaluate(managed, self.policy(), refresh=False, manual=True)
        self.assertTrue(result["watchOnly"])
        self.assertEqual(result["activeEmail"], "alpha@example.com")
        self.assertEqual(managed.calls, [])

    def test_service_wait_skips_decision_without_any_backend_command(self):
        from ccpick_app import setup
        helper = SimpleNamespace(cmd_wait=lambda _args: 0)
        with patch.object(setup, "legacy_services", return_value=[]), \
                patch.object(service, "_helper", return_value=helper), \
                patch.object(service.runtime, "bootstrap", return_value=self.legacy):
            self.assertEqual(service.tick(), 0)

    def test_explicit_service_force_overrides_saved_wait_once(self):
        from ccpick_app import setup
        helper = SimpleNamespace(cmd_wait=lambda _args: (_ for _ in ()).throw(AssertionError("force must not wait")),
                                 cmd_defer=lambda _args: 1, cmd_schedule=lambda _args: 1)
        completed = subprocess.CompletedProcess([], 2, '{"action":"stay","used":20}', "")
        with patch.object(setup, "legacy_services", return_value=[]), \
                patch.object(service, "_helper", return_value=helper), \
                patch.object(service, "write_status"), \
                patch.object(service, "_log"), \
                patch.object(service.runtime, "bootstrap", return_value=self.legacy), \
                patch.object(service.subprocess, "run", return_value=completed) as command, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(service.tick(force_check=True), 0)
        command.assert_called_once()

    def test_prepared_desktop_uses_helper_and_has_no_private_registry(self):
        windows = (self.legacy / "autoswitch/win/claude-autoswitch-tray.ps1").read_text(encoding="utf-8-sig")
        mac = (self.legacy / "autoswitch/menubar-main.swift").read_text(encoding="utf-8")
        for source in (windows, mac):
            self.assertNotIn("account-manager.json", source)
            self.assertIn("autoSwitchEnabled", source)
            self.assertIn("仅手动", source)
        self.assertIn("DarkViolet", windows)
        self.assertIn("systemPurple", mac)
        self.assertIn("$chosen.Count -ne 1", windows)
        self.assertIn("active.count == 1", mac)

    def test_managed_nonnumeric_slots_pass_through_desktop_helper(self):
        managed = SyntheticManager()
        helper = load("public_helper_profile_slots", self.legacy / "autoswitch/claude-autoswitch-helper.py")
        usage = SimpleNamespace(collect=managed.collect, capacity=lambda *_args: None,
            plan_info=lambda: {}, counts_toward_limit=lambda key: key in ("5h", "7d"),
            counted_windows=lambda wins: {key: value for key, value in wins.items() if key in ("5h", "7d")},
            is_usable=lambda _row: (True, "usable"), is_blocked=lambda _row: (False, ""))
        output = io.StringIO()
        with patch.object(helper, "_load_usage", return_value=(usage, "")), \
                patch.object(helper, "_active_email_from_status", return_value=None), redirect_stdout(output):
            self.assertEqual(helper.cmd_accounts([]), 0)
        rows = json.loads(output.getvalue())["accounts"]
        self.assertEqual({row["slot"] for row in rows}, {"account-a", "account-b"})
        self.assertEqual([row["email"] for row in rows if row["active"]], ["alpha@example.com"])
        mac = (self.legacy / "autoswitch/menubar-main.swift").read_text(encoding="utf-8")
        windows = (self.legacy / "autoswitch/win/claude-autoswitch-tray.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('var slot: String', mac)
        self.assertIn('r["slot"] as? String', mac)
        self.assertIn('function Switch-To([string]$email)', windows)
        self.assertIn('$mi.Tag = $a.email', windows)

    def test_context_bare_import_rewrites_with_original_local_name(self):
        import ast
        prepare = load("public_prepare_import_regression", PUBLIC / "prepare_core.py") if (PUBLIC / "prepare_core.py").is_file() else None
        if prepare is None:
            self.skipTest("exported copies are checked by the core selftests")
        tree = prepare.PublicCore("fixture.py").visit(ast.parse("import ccpick_account_context\nimport ccpick_account_context as context\n"))
        ast.fix_missing_locations(tree)
        source = ast.unparse(tree)
        self.assertIn("from ccpick_app import account_context as ccpick_account_context", source)
        self.assertIn("from ccpick_app import account_context as context", source)

    def test_enabled_runtime_failure_never_falls_back_or_echoes_diagnostic(self):
        output = io.StringIO()
        from ccpick_app import runtime_profiles
        with patch.object(runtime_profiles, "manager", side_effect=RuntimeError("https://example.com/?token=hidden")), redirect_stdout(output):
            self.assertEqual(account_context.dispatch("autoswitch", ["auto"]), 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "account_operation_failed")
        self.assertNotIn("hidden", output.getvalue())


if __name__ == "__main__":
    unittest.main()
