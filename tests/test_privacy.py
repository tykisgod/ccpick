"""Privacy checks use synthetic values and isolated temporary directories."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import hashlib
import json
import tempfile
import tarfile
import unittest
import zipfile
import zlib
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
            "127.0.0.1 ::1 0.0.0.0", "192.0.2.20 198.51.100.7 203.0.113.8",
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
        unicode_user = chr(0x7528) + chr(0x6237)
        self.write("README.md", "/Users/" + unicode_user + "/bin")
        self.assertEqual(CHECK.check_tree(self.root), ["README.md:1: personal home path"])

    def test_local_state_filenames(self) -> None:
        for name in ("nested/account-status.json", "capture.jsonl", "credentials/data.txt",
                       "auth.json", ".env.local", "quota-policy.json", "switch.json", "switch.lock"):
            self.write(name)
        findings = CHECK.check_tree(self.root)
        self.assertEqual(len(findings), 8)
        self.assertTrue(all(":1: " in finding for finding in findings))

    def test_authorization_source_is_allowed_but_private_data_is_not(self) -> None:
        for name in ("ccpick_auto_authorize.py", "ccpick_cdp.py", "ccpick_js_gate.py",
                     "auto_authorize.ps1"):
            self.write("src/legacy/" + name, "# Public authorization implementation\n")
        self.assertEqual(CHECK.check_tree(self.root), [])
        private = "person" + "@" + "company.invalid"
        self.write("src/legacy/ccpick_cdp.py", "# " + private)
        self.assertEqual(CHECK.check_tree(self.root),
                         ["src/legacy/ccpick_cdp.py:1: non-example email"])

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

    def test_unknown_binary_contents_require_review(self) -> None:
        path = self.write("image.png")
        path.write_bytes(b"\x89PNG\x00\xff")
        self.assertEqual(CHECK.check_tree(self.root),
                         ["image.png:1: binary artifact requires separate review"])

    def test_private_workspaces_and_repository_links(self) -> None:
        workspace = "D:" + "/personal-work/checkout/file.py"
        repository = "https://github.com/" + "example/private-infrastructure"
        self.write("README.md", workspace + "\n" + repository + "\n/srv/personal-work/config")
        self.assertEqual(CHECK.check_tree(self.root), [
            "README.md:1: private workspace path",
            "README.md:2: private repository link",
            "README.md:3: private workspace path",
        ])
        self.write("README.md", "C:/Windows/System32\nC:/Program Files/nodejs\n"
                   "D:/example/checkout\n/opt/example/app\n$HOME/project\n/usr/bin/python3\n"
                   "/opt/homebrew/bin/chromium\n/opt/homebrew/Caskroom/google-chrome/current/bin\n"
                   "https://github.com/example/public-tool\n")
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_identifiers_require_explicit_synthetic_values(self) -> None:
        identifier = "a1b2c3d4" + "-a1b2-c3d4-a1b2-a1b2c3d4e5f6"
        scope = "account-" + "0098"
        self.write("fixture.py", 'scope = "' + scope + '"\n'
                   'session_id = "' + identifier + '"\n'
                   'deviceId = "' + "ab" * 32 + '"\n')
        findings = CHECK.check_tree(self.root)
        self.assertEqual(len(findings), 4)
        self.assertNotIn(identifier, "\n".join(findings))
        self.write("fixture.py", 'scope = "' + scope + '" # synthetic-fixture\n'
                   'session_id = "' + identifier + '" // synthetic-fixture\n'
                   'scope = "default"\nsession_id = "00000000-0000-0000-0000-000000000000"\n'
                   'device_id = "fixture-device"\n'
                   'scope = runtime_scope()\n'
                   'pattern = r"account-[0-9]{4}"\n')
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_clear_runtime_fixtures_are_allowed_but_real_secret_shapes_are_not(self) -> None:
        self.write("source.py", '\n'.join((
            'access_token = "INVENTED-ACCOUNT-A-ACCESS"',
            'refreshToken = "INVENTED-ACCOUNT-A-REFRESH"',
            'channel_key = "LOCAL-TEST-ONLY-RUNTIME-CHANNEL-SECRET"',
            'Authorization: Bearer INVENTED-ACCOUNT-A',
            'sessionId = "00000000-0000-4000-8000-000000000001"',
            'sessionId = "00000000-0000-4000-8000-000000000099"',
            "channelKey = 'a'.repeat(64)",
        )))
        self.assertEqual(CHECK.check_tree(self.root), [])
        self.write("source.py", 'api_key = "INVENTED-' + 'sk-' + 'ant-' + 'X' * 28 + '"')
        findings = CHECK.check_tree(self.root)
        self.assertEqual(findings, ["source.py:1: Anthropic credential"])

    def test_public_protocol_marker_only_allows_the_known_client_constant(self) -> None:
        known = CHECK.PUBLIC_PROTOCOL_CLIENT_ID
        self.write("vault.mjs", "const CLIENT_ID = '" + known + "'; // public-protocol-id")
        self.assertEqual(CHECK.check_tree(self.root), [])
        for source in ("const CLIENT_ID = '" + known + "';",
                       "const SESSION_ID = '" + known + "'; // public-protocol-id",
                       "const CLIENT_ID = '" + known.replace("9d1c", "8d1c") + "'; // public-protocol-id"):
            self.write("vault.mjs", source)
            self.assertTrue(CHECK.check_tree(self.root))

    def test_local_credentials_headers_and_authorization_urls(self) -> None:
        opaque = "q" * 48
        capability = "ab" * 32
        self.write("source.txt", '\n'.join((
            'password = "' + 'short123' + '"',
            'channelKey = "' + capability + '"',
            'Authorization: ' + 'Bearer ' + opaque,
            'ANTHROPIC_' + 'CUSTOM_HEADERS="x-local-channel: ' + capability + '"',
            'https://example.com/callback?' + 'code=' + opaque,
            'runtime_key = "' + capability + '" # synthetic-fixture',
        )))
        findings = CHECK.check_tree(self.root)
        self.assertEqual(len(findings), 6)
        self.assertTrue(all(value not in "\n".join(findings) for value in (opaque, capability)))
        self.write("source.txt", '\n'.join((
            'password = "<PASSWORD>"', 'channel_key = "fixture-local-key"',
            'Authorization: Bearer <TOKEN_VALUE>',
            'ANTHROPIC_CUSTOM_HEADERS = "${PRIVATE_HEADERS}"',
            'https://example.com/callback?code=fixture-code',
            'headers = {"Authorization": build_authorization()}',
            'ANTHROPIC_CUSTOM_HEADERS = serialize_headers(scope)',
            'password = read_password()',
        )))
        self.assertEqual(CHECK.check_tree(self.root), [])

    def test_additional_runtime_state_names_are_rejected(self) -> None:
        for name in ("state.json", "channel.key", "runtime-channel.json", "monitoring/report.json",
                     "leases/one.json", "pending-inputs/one.json", "capture.log", "oauth.sqlite"):
            self.write(name)
        self.assertEqual(len(CHECK.check_tree(self.root)), 8)

    @staticmethod
    def png(*extra: tuple[bytes, bytes]) -> bytes:
        def chunk(kind: bytes, value: bytes) -> bytes:
            return (len(value).to_bytes(4, "big") + kind + value
                    + zlib.crc32(kind + value).to_bytes(4, "big"))
        header = (1).to_bytes(4, "big") * 2 + bytes((8, 6, 0, 0, 0))
        return (CHECK.PNG_SIGNATURE + chunk(b"IHDR", header)
                + b"".join(chunk(*item) for item in extra)
                + chunk(b"IDAT", zlib.compress(bytes((0, 0, 0, 0, 255)))) + chunk(b"IEND", b""))

    def test_image_metadata_is_not_silently_skipped(self) -> None:
        path = self.write("image.png")
        path.write_bytes(self.png())
        self.assertEqual(CHECK.check_tree(self.root), [])
        for kind in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"caBX"):
            path.write_bytes(self.png((kind, b"opaque metadata")))
            self.assertEqual(CHECK.check_tree(self.root),
                             ["image.png:1: image metadata requires removal or separate review"])

    def test_wheel_members_and_unsafe_paths_are_inspected(self) -> None:
        path = self.root / "sample.whl"
        value = "S" * 28
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("package/source.py", 'api_key = "' + value + '"')
            archive.writestr("package/state.json", "{}")
            archive.writestr("../outside.py", "safe")
        findings = CHECK.check_archive(path)
        self.assertEqual(findings, [
            "sample.whl!/package/source.py:1: credential assignment",
            "sample.whl!/package/state.json:1: local state or credential dump",
            "sample.whl:1: unsafe or duplicate archive member",
        ])
        self.assertNotIn(value, "\n".join(findings))
        self.assertFalse((self.root.parent / "outside.py").exists())

    def test_sdist_inspection_rejects_links_without_extraction(self) -> None:
        import io
        path = self.root / "sample.tar.gz"
        with tarfile.open(path, "w") as archive:
            member = tarfile.TarInfo("package/module.py")
            payload = b"print('safe')\n"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
            link = tarfile.TarInfo("package/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "outside"
            archive.addfile(link)
        self.assertEqual(CHECK.check_archive(path),
                         ["sample.tar.gz!/package/link:1: archive link is not permitted"])

    def test_archive_container_metadata_is_not_ignored(self) -> None:
        path = self.root / "sample.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.comment = b"private metadata"
            archive.writestr("module.py", "print('safe')")
        self.assertEqual(CHECK.check_archive(path),
                         ["sample.whl:1: archive comment requires removal or separate review"])
        path = self.root / "sample.tar.gz"
        with tarfile.open(path, "w") as archive:
            member = tarfile.TarInfo("module.py")
            member.uname = "personal-owner"
            member.gname = "personal-group"
            member.size = 0
            archive.addfile(member, io.BytesIO())
        self.assertEqual(CHECK.check_archive(path),
                         ["sample.tar.gz:1: archive owner metadata requires removal or separate review"])

    def test_gzip_original_filename_and_comment_are_not_ignored(self) -> None:
        import gzip
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            member = tarfile.TarInfo("module.py")
            member.size = 0
            archive.addfile(member, io.BytesIO())
        path = self.root / "sample.tar.gz"
        with path.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, filename="", mode="wb", mtime=0) as output:
                output.write(payload.getvalue())
        self.assertEqual(CHECK.check_archive(path), [])
        with path.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, filename="original-personal-name.tar", mode="wb", mtime=0) as output:
                output.write(payload.getvalue())
        self.assertEqual(CHECK.check_archive(path),
                         ["sample.tar.gz:1: gzip filename, comment or extra metadata requires removal"])

    def test_build_inclusion_and_archive_cli(self) -> None:
        self.write("dist/state.json", "{}")
        self.assertEqual(CHECK.check_tree(self.root), [])
        self.assertEqual(CHECK.check_tree(self.root, include_build=True),
                         ["dist/state.json:1: local state or credential dump"])
        path = self.root / "sample.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("package/module.py", "print('safe')")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(CHECK.main([str(path), "--archive"]), 0)

    def receipt(self, files: dict[str, str], **extra) -> Path:
        return self.write(CHECK.EXPORT_RECORD, json.dumps({"format": 1, "files": files, **extra}))

    def test_export_receipt_matches_only_source_content_hashes(self) -> None:
        source = self.write("source.py", "print('safe')\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.receipt({"source.py": digest})
        self.assertEqual(CHECK.check_export_record(self.root), [])
        self.assertEqual(CHECK.check_tree(self.root), [])
        self.assertEqual(CHECK.check_export_record(self.root / "missing"), [])
        source.write_text("changed", encoding="utf-8")
        self.assertEqual(CHECK.check_export_record(self.root),
                         [".ccpick-export.json:1: export receipt digest does not match source"])

    def test_export_receipt_schema_and_duplicate_fields_are_rejected(self) -> None:
        variants = ({"format": True, "files": {}}, {"format": 2, "files": {}},
                    {"format": 1, "files": {}, "extra": "not a digest"},
                    {"format": 1, "files": []}, ["not an object"])
        for value in variants:
            self.write(CHECK.EXPORT_RECORD, json.dumps(value))
            self.assertEqual(CHECK.check_export_record(self.root),
                             [".ccpick-export.json:1: export receipt schema or source could not be verified"])
        self.write(CHECK.EXPORT_RECORD, '{"format": 1, "format": 1, "files": {}}')
        self.assertTrue(CHECK.check_export_record(self.root))
        self.write(CHECK.EXPORT_RECORD, '{"format": 1, "files": {"a": "b", "a": "c"}}')
        self.assertTrue(CHECK.check_export_record(self.root))

    def test_export_receipt_paths_and_digest_shapes_are_rejected(self) -> None:
        for name in ("../outside", "/absolute", "a//b", "a\\b", "C:" + "/outside", ".git/config",
                     "dist/output.whl", "source.py.", "nul/file", CHECK.EXPORT_RECORD):
            self.receipt({name: "a" * 64})
            self.assertEqual(CHECK.check_export_record(self.root),
                             [".ccpick-export.json:1: unsafe export receipt path"])
        self.write("source.py", "safe")
        for digest in ("a" * 63, "g" * 64, "A" * 64, 123):
            self.receipt({"source.py": digest})
            self.assertEqual(CHECK.check_export_record(self.root),
                             [".ccpick-export.json:1: invalid export receipt digest"])

    def test_export_receipt_missing_or_linked_sources_are_rejected(self) -> None:
        from unittest import mock
        self.receipt({"missing.py": "a" * 64})
        self.assertTrue(CHECK.check_export_record(self.root))
        # The checker canonicalizes its root. macOS temporary directories and
        # Windows short-name TEMP paths can have a different lexical spelling.
        path = self.write("source.py", "safe").resolve()
        self.receipt({"source.py": hashlib.sha256(path.read_bytes()).hexdigest()})
        with mock.patch.object(Path, "is_symlink", lambda value: value == path):
            self.assertTrue(CHECK.check_export_record(self.root))
        nested = self.write("nested/source.py", "safe").resolve()
        self.receipt({"nested/source.py": hashlib.sha256(nested.read_bytes()).hexdigest()})
        with mock.patch.object(Path, "is_symlink", lambda value: value == nested.parent):
            self.assertTrue(CHECK.check_export_record(self.root))

    def test_export_receipt_rejects_links_after_root_alias_resolution(self) -> None:
        from unittest import mock
        canonical = (self.root / "canonical").resolve()
        source = canonical / "nested" / "source.py"
        source.parent.mkdir(parents=True)
        source.write_text("safe", encoding="utf-8")
        (canonical / CHECK.EXPORT_RECORD).write_text(json.dumps({
            "format": 1, "files": {"nested/source.py": hashlib.sha256(source.read_bytes()).hexdigest()}
        }), encoding="utf-8")
        alias = self.root / "lexical-alias"
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return canonical if path == alias else original_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve):
            self.assertEqual(CHECK.check_export_record(alias), [])
            for method, target in (("is_symlink", source), ("is_symlink", source.parent),
                                   ("is_junction", source.parent)):
                with self.subTest(method=method, parent=target == source.parent):
                    with mock.patch.object(Path, method, lambda value, target=target: value == target,
                                           create=True):
                        self.assertTrue(CHECK.check_export_record(alias))

    def test_packed_export_receipt_uses_its_own_members(self) -> None:
        path = self.root / "sample.whl"
        data = b"print('safe')\n"
        digest = hashlib.sha256(data).hexdigest()
        for valid in (True, False):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("source.py", data)
                archive.writestr(CHECK.EXPORT_RECORD, json.dumps({
                    "format": 1, "files": {"source.py": digest if valid else "a" * 64}}))
            self.assertEqual(CHECK.check_archive(path), [] if valid else [
                "sample.whl!/.ccpick-export.json:1: export receipt digest does not match source"])

    def test_nested_export_receipts_are_rejected_even_with_valid_content_digests(self) -> None:
        source = self.write("nested/source.py", "safe")
        value = {"format": 1, "files": {"source.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
        self.write("nested/" + CHECK.EXPORT_RECORD, json.dumps(value))
        self.assertEqual(CHECK.check_tree(self.root),
                         ["nested/.ccpick-export.json:1: nested export receipt is not permitted"])
        path = self.root / "sample.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("nested/source.py", source.read_bytes())
            archive.writestr("nested/" + CHECK.EXPORT_RECORD, json.dumps(value))
        self.assertEqual(CHECK.check_archive(path),
                         ["sample.whl!/nested/.ccpick-export.json:1: nested export receipt is not permitted"])

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
