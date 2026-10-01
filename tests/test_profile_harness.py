"""The runtime test runner uses canonical disposable paths on every platform."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ProfileHarnessTests(unittest.TestCase):
    def test_home_and_native_temp_paths_use_resolved_boundary(self):
        script = Path(__file__).resolve().parents[1] / "tools/test_profiles.py"
        spec = importlib.util.spec_from_file_location("ccpick_test_profile_harness", script)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temporary:
            canonical = Path(temporary).resolve()
            alias = canonical.parent / "synthetic-os-path-alias"
            with patch.object(Path, "resolve", return_value=canonical) as resolve, \
                    patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "do-not-inherit", "CCPICK_DATA_DIR": "do-not-inherit"}):
                environment = runner.isolated_environment(alias)
            resolve.assert_called_once_with(strict=True)
            self.assertEqual(environment["HOME"], str(canonical))
            self.assertEqual(environment["USERPROFILE"], str(canonical))
            for key in ("TMPDIR", "TEMP", "TMP"):
                self.assertEqual(environment[key], str(canonical / "tmp"))
                self.assertTrue(Path(environment[key]).is_dir())
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(canonical / "claude"))
            self.assertEqual(environment["CCPICK_DATA_DIR"], str(canonical / "ccpick"))
            self.assertNotIn(str(alias), environment.values())


if __name__ == "__main__":
    unittest.main()
