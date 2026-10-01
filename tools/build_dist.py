"""Build the package without local owner or gzip source-name metadata."""
from __future__ import annotations

import copy
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def normalize_sdist(path: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".ccpick-dist-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(path, "r:gz") as source, tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                for member in source:
                    if not (member.isdir() or member.isfile()):
                        raise ValueError("Unexpected distribution member type")
                    public = copy.copy(member)
                    public.uid = public.gid = public.mtime = 0
                    public.uname = public.gname = ""
                    public.pax_headers = {}
                    data = source.extractfile(member) if member.isfile() else None
                    try:
                        target.addfile(public, data)
                    finally:
                        if data is not None:
                            data.close()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("Install uv before building the distribution.")
        return 1
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([uv, "build"], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        print("Distribution build failed; inspect the build locally.")
        return result.returncode
    for path in (root / "dist").glob("*.tar.gz"):
        normalize_sdist(path)
    print("Built wheel and source distribution; source owner metadata removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
