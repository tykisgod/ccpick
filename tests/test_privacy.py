"""Privacy checks use synthetic values and isolated temporary directories."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_public", ROOT / "tools" / "check_public.py")
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class PublicPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ccpick-privacy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name: str, value: str = "") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_email_allowlist_and_redacted_diagnostics(self) -> None:
        private = "person" + "@" + "company.invalid"
        self.write("README.md", "public@example.com\n" + private + "\n")
        findings = CHECK.check_tree(self.root)
        self.assertEqual(findings, ["README.md:2: non-example email"])
        self.assertNotIn(private, "\n".join(findings))
        for domain in ("example.com", "sample.example.net", "example.org",
                       "users.noreply.github.com", "noreply.github.com"):
            self.assertTrue(CHECK._email_allowed(domain))
        self.assertFalse(CHECK._email_allowed("example.com.invalid"))

    def test_ip_allowlist_and_version_context(self) -> None:
        self.write("README.md", "\n".join((
            "127.0.0.1 ::1", "192.0.2.20 198.51.100.7 203.0.113.8",
            "2001:db8::1", 'version = "1.2.3.4"', "release v1.2.3.4",
            "Operating System :: Microsoft :: Windows",
        )))
        self.assertEqual(CHECK.check_tree(self.root), [])
        address = ".".join(map(str, (198, 18, 0, 1)))
        self.write("README.md", "server_ip = " + address)
        self.assertEqual(CHECK.check_tree(self.root), ["README.md:1: non-example IP address"])
        self.write("README.md", "endpoint = " + "fd" + "00::1")
        self.assertEqual(CHECK.check_tree(self.root), ["README.md:1: non-example IP address"])

    def test_home_paths_and_templates(self) -> None:
        self.write("README.md", "/Users/example/bin\n/home/example/bin\n"
                   "C:\\Users\\example\\bin\n$HOME/bin\n/Users/${USER}/bin\n"
                   "Path.home() / '.claude'\n")
        self.assertEqual(CHECK.check_tree(self.root), [])
        path = "/Users/" + "personal-user" + "/bin"
        self.write("README.md", path)
        self.assertEqual(CHECK.check_tree(self.root), ["README.md:1: personal home path"])
        self.write("README.md", "C:" + "\\Users\\" + "personal-user" + "\\bin")
        self.assertEqual(CHECK.check_tree(self.root), ["README.md:1: personal home path"])

    def test_local_state_filenames_and_excluded_modules(self) -> None:
        for name in ("nested/account-status.json", "capture.jsonl", "credentials/data.txt",
                     "auth.json", ".env.local", "ccpick_cdp.py", "auto_authorize.ps1"):
            self.write(name)
        findings = CHECK.check_tree(self.root)
        self.assertEqual(len(findings), 7)
        self.assertTrue(all(":1: " in finding for finding in findings))

    def test_secrets_are_reported_without_the_value(self) -> None:
        secret = "sk-" + "ant-" + "X" * 28
        private_key = "-----BEGIN " + "PRIVATE KEY-----"
        github = "gh" + "p_" + "Q" * 30
        self.write("sample.txt", secret + "\n" + private_key + "\n" + github)
        findings = CHECK.check_tree(self.root)
        self.assertEqual(len(findings), 3)
        self.assertNotIn(secret, "\n".join(findings))
        self.assertNotIn(private_key, "\n".join(findings))
        self.assertNotIn(github, "\n".join(findings))

    def test_credential_assignments_and_placeholders(self) -> None:
        self.write("sample.txt", 'api_key = "' + "S" * 28 + '"')
        self.assertEqual(CHECK.check_tree(self.root), ["sample.txt:1: credential assignment"])
        self.write("sample.txt", 'api_key = "<YOUR_API_KEY_HERE>"')
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_build_and_environment_directories_are_ignored(self) -> None:
        for directory in (".git", ".venv", "build", "dist", "__pycache__", "ccpick.egg-info"):
            self.write(directory + "/auth.json", "private")
        self.write(".env.example", "# Fill locally")
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_binary_contents_are_not_decoded(self) -> None:
        path = self.write("image.png")
        path.write_bytes(b"\x89PNG\x00\xff")
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_cli_fails_and_does_not_echo_input(self) -> None:
        secret = "github_" + "pat_" + "R" * 35
        self.write("source.py", secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = CHECK.main([str(self.root)])
        self.assertEqual(result, 1)
        self.assertIn("source.py:1: GitHub credential", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_public_checker_and_tests_are_clean(self) -> None:
        # Scan these maintained sources as an actual tree, including comments.
        for source in (ROOT / "tools" / "check_public.py", Path(__file__)):
            self.write(source.name, source.read_text(encoding="utf-8"))
        self.assertEqual(CHECK.check_tree(self.root), [])


if __name__ == "__main__":
    unittest.main()
