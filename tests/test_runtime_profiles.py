from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from ccpick_app import runtime_profiles as profiles


class RuntimeProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.service = self.base / "profile-runtime"
        self.root = self.service / "private" / "account-profiles"
        self.root.mkdir(parents=True)
        (self.root / "data").mkdir()
        if os.name != "nt":
            for path in (self.service, self.service / "private", self.root, self.root / "data"):
                path.chmod(0o700)
        self.install = {"version": 1, "platform": sys.platform, "seamlessAccounts": True,
                        "serviceRoot": str(self.service), "dataRoot": str(self.root / "data"),
                        "node": str(self.base / "node"), "native": str(self.base / "claude"),
                        "python": sys.executable, "browser": str(self.base / "ccpick"),
                        "networkProfile": "public", "upstreamProxy": "http://127.0.0.1:18999"}
        profiles.write(self.root / "install.json", self.install)
        profiles.write(self.root / "state.json", {"version": 2, "enabled": True,
                       "selected": "account-a", "selectedAt": "generation-a"})
        self.manager = profiles.Manager(self.root)

    def profile(self, name="account-a", *, bound=True, manual=False):
        root = self.root / "data" / name
        directory = root / "claude"
        directory.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            root.chmod(0o700)
        ids = {key: hashlib.sha256((name + key).encode()).hexdigest() for key in ("userID", "machineID")}
        account = {"uuid": name + "-synthetic", "email": name + "@example.com"} if bound else None
        profiles.write(directory / ".claude.json", {**ids, "oauthAccount": {"accountUuid": account["uuid"]} if account else {}})
        profiles.write(root / "profile.json", {"version": 1, "name": name, "label": name,
                        "account": account, "email": name + "@example.com",
                        "identity": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in ids.items()}})
        if manual:
            profiles.write(root / "switch-policy.json", {"version": 1, "autoSwitchEnabled": False})
        return root

    def test_absent_runtime_keeps_legacy_backend_and_corrupt_optin_never_falls_back(self):
        missing = self.base / "missing.json"
        with mock.patch.object(profiles, "installation", return_value=missing):
            self.assertFalse(profiles.enabled())
            self.assertIsNone(profiles.backend())
            self.assertIsNone(profiles.dispatch("ccpick", ["switch", "2"]))
        profiles.write(self.root / "install.json", {"corrupt": True})
        with mock.patch.object(profiles, "installation", return_value=self.root / "install.json"):
            self.assertTrue(profiles.enabled())
            with self.assertRaisesRegex(profiles.ManagerError, "runtime_installation_invalid"):
                profiles.backend()

    def test_native_browser_finds_custom_data_installation_without_data_env(self):
        with mock.patch.dict(os.environ, {"CCPICK_ACCOUNT_RUNTIME": "1",
                             "CCPICK_RUNTIME_INSTALL": str(self.root / "install.json")}):
            self.assertEqual(profiles.installation(), self.root / "install.json")
            self.assertEqual(profiles.Manager().selection()['selected'], 'account-a')
        with mock.patch.dict(os.environ, {"CCPICK_ACCOUNT_RUNTIME": "1", "CCPICK_RUNTIME_INSTALL": "relative.json"}):
            with self.assertRaisesRegex(profiles.ManagerError, "runtime_installation_invalid"):
                profiles.installation()

    def test_loopback_proxy_only(self):
        for value in ("http://127.0.0.1:8123", "http://[::1]:8123/"):
            self.assertTrue(profiles.validate_proxy(value))
        for value in ("https://127.0.0.1:8123", "http://proxy.example.com:8123",
                      "http://user:password@127.0.0.1:8123", "http://127.0.0.1:8123/path",
                      "http://127.0.0.1:8123?token=fixture", "http://127.0.0.1", "http://127.0.0.1:70000"):
            with self.assertRaisesRegex(profiles.ManagerError, "upstream_proxy_invalid"):
                profiles.validate_proxy(value)

    def test_manual_only_roster_and_usage_remain_visible(self):
        root = self.profile(manual=True)
        profiles.write(root / "usage.json", {"fetchedAt": 123, "data": {"five_hour": {"utilization": 25,
                       "resets_at": "2999-01-01T00:00:00Z"}}, "error": None})
        self.assertFalse(self.manager.find("account-a")['autoSwitchEnabled'])
        row = self.manager.collect()[0]
        self.assertFalse(row['autoSwitchEnabled'])
        self.assertTrue(row['active'])
        self.assertEqual(row['windows']['5h']['pct'], 25)
        self.assertEqual(row['error'], 'usage_not_current')

    def test_identity_changes_fail_without_echoing_values(self):
        root = self.profile()
        selected = self.manager.find("account-a")
        self.manager.verify(selected)
        profiles.write(root / "claude" / ".claude.json", {"userID": "fixture"})
        with self.assertRaisesRegex(profiles.ManagerError, "^identity_changed$"):
            self.manager.verify(selected)

    def test_guarded_selection_uses_exact_receipt_not_latest_selection(self):
        self.profile("account-a")
        self.profile("account-b")
        expected = {"selected": "account-a", "selectedAt": "generation-a"}
        with mock.patch.object(self.manager, "_node", return_value={"ok": True,
                               "selectionReceipt": {"profileId": "account-b", "generation": "generation-b"}}) as run:
            result = self.manager.select("account-b", expected_state=expected)
        run.assert_called_once_with("select-guarded", ("account-b", "account-a", "generation-a"))
        self.assertEqual(result['selectionReceipt']['generation'], 'generation-b')
        self.assertEqual(self.manager.selection(), expected)
        for receipt in ({"profileId": "account-a", "generation": "later-manual"}, {"profileId": "account-b"},
                        {"profileId": "account-b", "generation": "b", "token": "must-not-forward"}):
            with mock.patch.object(self.manager, "_node", return_value={"ok": True, "selectionReceipt": receipt}):
                with self.assertRaisesRegex(profiles.ManagerError, "runtime_response_invalid"):
                    self.manager.select("account-b", expected_state=expected)

    def test_node_diagnostic_never_echoes_auth_output(self):
        query = profiles.urllib.parse.urlencode({"code": uuid.uuid4().hex})
        process = subprocess.CompletedProcess([], 1, "", "https://auth.example.com/?" + query)
        with mock.patch.object(profiles.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(profiles.ManagerError, "^runtime_unavailable$"):
                self.manager._node("ready")

    def test_usage_request_sets_connect_proxy_even_with_no_proxy(self):
        self.profile()
        profile = self.manager.find("account-a")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"five_hour":{"utilization":10}}'
        opener = mock.Mock()
        opener.open.return_value = response
        credential = uuid.uuid4().hex
        with mock.patch.object(self.manager, "ensure_ready") as ready, \
                mock.patch.object(self.manager, "credentials", return_value={"claudeAiOauth": {"accessToken": credential}}), \
                mock.patch.object(profiles.urllib.request, "build_opener", return_value=opener), \
                mock.patch.dict(os.environ, {"NO_PROXY": "*"}):
            data = self.manager.request_usage(profile)
        ready.assert_called_once_with(profile, timeout=75)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "127.0.0.1:18999")
        self.assertEqual(request._tunnel_host, "api.anthropic.com")
        self.assertEqual(data['five_hour']['utilization'], 10)

    def test_refresh_error_preserves_successful_cache_and_never_becomes_zero(self):
        root = self.profile()
        original = {"fetchedAt": 123, "data": {"five_hour": {"utilization": 95}}}
        profiles.write(root / "usage.json", original)
        with mock.patch.object(self.manager, "request_usage", side_effect=profiles.ManagerError("network_unavailable")):
            self.manager.refresh(force=True)
        value = profiles.read(root / "usage.json")
        self.assertEqual(value['data'], original['data'])
        self.assertEqual(value['fetchedAt'], 123)
        self.assertEqual(value['error'], 'network_unavailable')

    def test_targeted_candidate_refresh_bypasses_adaptive_schedule_but_honors_429(self):
        import time
        self.profile("account-a")
        root = self.profile("account-b")
        profiles.write(root / "usage.json", {"fetchedAt": 123, "nextPollAt": time.time() + 3600,
                       "data": {"five_hour": {"utilization": 20}}, "error": None})
        data = {"five_hour": {"utilization": 15}}
        with mock.patch.object(self.manager, "request_usage", return_value=data) as request, \
                mock.patch.object(self.manager, "_poll_plan", return_value={"nextPollAt": time.time() + 60}):
            self.manager.refresh(slots=["account-b"], budget_s=10)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0]['name'], 'account-b')
        self.assertLessEqual(request.call_args.kwargs['timeout'], 10)
        profiles.write(root / "usage.json", {"error": "http-429", "nextPollAt": time.time() + 3600,
                       "data": data})
        with mock.patch.object(self.manager, "request_usage") as request:
            self.manager.refresh(force=True, slots=["account-b"], budget_s=10)
        request.assert_not_called()

    def test_setup_dry_run_writes_nothing(self):
        target = self.base / "fresh" / "private" / "account-profiles" / "install.json"
        node, native = self.base / "node", self.base / "claude"
        node.write_text("synthetic executable")
        native.write_text("synthetic executable")
        original = Path.is_file
        def existing(path):
            return True if str(path).replace('\\', '/').endswith('/openssl.exe') or str(path) == '/usr/bin/openssl' else original(path)
        with mock.patch.object(profiles, "installation", return_value=target), \
                mock.patch.object(profiles.runtime, "launcher_path", return_value=str(self.base / "ccpick")), \
                mock.patch.object(profiles.sys, "platform", "win32"), \
                mock.patch.object(Path, "is_file", existing), \
                mock.patch.object(profiles.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "v24.1.0\n", "")), \
                mock.patch.object(profiles, "write") as write:
            self.assertEqual(profiles.setup(["--upstream-proxy", "http://127.0.0.1:18999", "--node", str(node),
                                            "--native", str(native), "--dry-run"]), 0)
        write.assert_not_called()
        self.assertFalse(target.parent.exists())

    def test_natural_add_and_runtime_automation_do_not_call_global_backend(self):
        from ccpick_app import cli, backend
        with mock.patch.object(profiles, "enabled", return_value=True), \
                mock.patch.object(profiles, "Manager", return_value=self.manager), \
                mock.patch.object(self.manager, "native", return_value=0) as native:
            self.assertEqual(profiles.main(["add", "account@example.com"]), 0)
            native.assert_called_once_with(["add", "account@example.com"])
        with mock.patch.object(profiles, "enabled", return_value=True), \
                mock.patch.object(backend, "run") as global_backend:
            self.assertEqual(cli.main(["auto-enroll", "--profile", "Default"]), 1)
            self.assertEqual(cli.main(["auto-enroll-all"]), 1)
            global_backend.assert_not_called()
        with mock.patch.object(profiles, "enabled", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "runtime_backend_mutation_refused"):
                backend.run(["switch", "2"])


if __name__ == '__main__':
    unittest.main()
