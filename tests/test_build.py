"""Check distribution metadata cleanup without installing or publishing."""
from __future__ import annotations

import gzip
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_dist", ROOT / "tools/build_dist.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class BuildPrivacyTests(unittest.TestCase):
    def test_sdist_cleanup_preserves_payload_and_removes_owner_and_gzip_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "package.tar.gz"
            payload = b"synthetic public source\n"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("package/src/module.py")
                member.size = len(payload)
                member.uname, member.gname, member.uid, member.gid = "synthetic-user", "synthetic-group", 123, 456
                member.pax_headers = {"mtime": "123.5", "comment": "synthetic local metadata"}
                archive.addfile(member, io.BytesIO(payload))
            BUILD.normalize_sdist(path)
            header = path.read_bytes()
            self.assertEqual(header[3] & 0x1C, 0)  # no filename, comment, or extra header
            with tarfile.open(path, "r:gz") as archive:
                member = archive.getmember("package/src/module.py")
                self.assertEqual((member.uname, member.gname, member.uid, member.gid, member.mtime), ("", "", 0, 0, 0))
                self.assertEqual(member.pax_headers, {})
                self.assertEqual(archive.extractfile(member).read(), payload)

    def test_unexpected_link_keeps_original_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "package.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("package/link")
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                archive.addfile(member)
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                BUILD.normalize_sdist(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
