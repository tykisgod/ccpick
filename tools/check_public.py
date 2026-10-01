#!/usr/bin/env python3
"""Check a public export for local state and common private data patterns.

Diagnostics contain relative filenames, line numbers, and rule names only.
This conservative check complements a credential scanner such as Gitleaks; it
does not prove that arbitrary personal names or confidential prose are absent.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__"}
STATE_FILES = {
    "account-status.json", "profile-accounts.json", "labels.json",
    "usage-history.jsonl", ".code-inbox", ".login-pending",
    ".credentials.json", "credentials.json", "auth.json", "sequence.json",
    "usage.json", "autoswitch_state.json", "status.json",
    "quota-policy.json", "switch.json", "switch.lock",
    "claude-autoswitch-ledger.json", "claude-autoswitch-samples.json",
    "cookies", "cookies.sqlite", "login data", "local state", "web data",
}
PRIVATE_DIRS = {"credentials", "keychains", "browser-profiles"}
PRIVATE_SUFFIXES = {".jsonl", ".cswap", ".keychain", ".keychain-db"}
PRIVATE_SUFFIXES |= {".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key", ".p12", ".pfx"}
STATE_FILES |= {"state.json", "channel.key", ".runtime-channel-key", "runtime-channel.json",
                "account-vault.json", "leases.json", "selection.json", "switch-policy.json"}
PRIVATE_DIRS |= {"monitoring", "pending-inputs", "leases", "account-vault", "browser-data"}

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                   r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,})")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]*(?![\w:])")
VERSION_PREFIX = re.compile(
    r"(?:\b(?:version|release|python)[\w-]*\s*[=:]?\s*[\"']?|\bv)$", re.I)
HOME_PATH = re.compile(
    r"(?<![\w])(?:/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]+)"
    r"([^\s/\\\"'<>|]+)", re.I)
ALLOWED_HOME_USERS = {"example"}
DOC_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))
SECRET_RULES = {
    "Anthropic credential": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI credential": re.compile(r"sk-(?:proj-|svcacct-)[A-Za-z0-9_-]{20,}"),
    "GitHub credential": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Google credential": re.compile(r"(?:AIza[A-Za-z0-9_-]{35}|ya29\.[A-Za-z0-9_-]{20,})"),
    "Slack credential": re.compile(r"xox[baprs]-[A-Za-z0-9-]{15,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "JWT credential": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
}
TOKEN_ASSIGNMENT = re.compile(
    r"[\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[_-]?key|password)[\"']?\s*[:=]\s*"
    r"[\"']([^\"'\s]{16,})[\"']", re.I)
PLACEHOLDER = re.compile(r"^(?:<[^>]+>|\$\{[^}]+\}|(?:example|dummy|test|redacted|placeholder)[-_].*)$", re.I)

# Only literal values are inspected. Field names and dynamic schema definitions
# can describe secrets without containing one. Explicit fixture markers exempt
# paths/identifiers only; they never exempt credentials.
SYNTHETIC_MARKER = re.compile(r"(?:#|//)\s*synthetic-fixture\b", re.I)
WINDOWS_PATH = re.compile(r"(?<![\w:/\\])([A-Za-z]:[\\/]+)([^\s\"'<>|]+)")
UNIX_WORKSPACE = re.compile(
    r"(?<![\w:/])/(?:Volumes|mnt|media|workspace|workspaces|srv|opt|projects|repos|repo|private|data)/"
    r"([^\s\"'<>]+)")
SAFE_PATH_FIRST = {"windows", "program files", "program files (x86)", "example", "fake", "mock",
                   "test", "tests", "fixture", "fixtures", "temp", "tmp"}
PRIVATE_REPOSITORY = re.compile(
    r"(?:https?://|git@)github\.com[:/][A-Za-z0-9_.-]+/"
    r"(?:private(?:[-_.][A-Za-z0-9_.-]+)?|internal(?:[-_.][A-Za-z0-9_.-]+)?|"
    r"personal(?:[-_.][A-Za-z0-9_.-]+)?)\b", re.I)
SYSTEM_UNIX_PREFIXES = ("/opt/homebrew/", "/opt/google/chrome/", "/opt/microsoft/msedge/")
PUBLIC_PROTOCOL_CLIENT_ID = "-".join(("9d1c250a", "e61b", "44d9", "88ed", "5944d1962f5e"))
PROTOCOL_CLIENT_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?const\s+CLIENT_ID\s*=\s*[\"']([^\"']+)[\"'];\s*"
    r"//\s*public-protocol-id\s*$")
IDENTIFIER_ASSIGNMENT = re.compile(
    r"[\"']?(?:scope|(?:device|machine|session|account)[_-]?(?:id|uuid))[\"']?\s*[:=]\s*"
    r"[\"']([^\"'\s]+)[\"']", re.I)
UUID = re.compile(r"(?<![\w-])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![\w-])", re.I)
ACCOUNT_SCOPE = re.compile(r"^account-\d{4,}$", re.I)
LITERAL_CREDENTIAL = re.compile(
    r"[\"']?(access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|api[_-]?key|"
    r"password|passwd|(?:local|channel|runtime|model[_-]?local)[_-]?(?:key|capability)|"
    r"channel[_-]?capability|capability)[\"']?\s*[:=]\s*[\"']([^\"'\s]+)[\"']"
    r"(?!\s*(?:[+*]|\.))", re.I)
BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/<>{}$=-]{16,})", re.I)
CUSTOM_HEADERS = re.compile(r"\bANTHROPIC_CUSTOM_HEADERS\b[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", re.I)
OAUTH_QUERY = re.compile(r"[?&](?:code|access_token|refresh_token|id_token)=([^&#\s\"'<>]{8,})", re.I)
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SAFE_PNG_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"sRGB",
                   b"pHYs", b"sBIT", b"bKGD"}
EXPORT_RECORD = ".ccpick-export.json"
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)),
                   *(f"lpt{number}" for number in range(1, 10))}


def _synthetic_identifier(value: str) -> bool:
    if _placeholder(value):
        return True
    # Repeated-digit UUIDs are recognizable fixture values, without admitting
    # arbitrary UUIDs merely because they occur in a test file.
    if UUID.fullmatch(value) and len(set(value.replace("-", "").casefold())) == 1:
        return True
    if (re.fullmatch(r"00000000-0000-4000-8000-000000000\d{3}", value)
            and int(value.rsplit("-", 1)[1]) <= 99):
        return True
    return False


def _placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.fullmatch(value)
                or value.casefold().startswith(("fixture-", "invented-", "local-test-only-")))


def _literal_identifier(value: str) -> bool:
    return bool(ACCOUNT_SCOPE.fullmatch(value) or UUID.fullmatch(value)
                or re.fullmatch(r"[0-9a-f]{16,}", value, re.I))


def _private_workspace(line: str) -> bool:
    for match in WINDOWS_PATH.finditer(line):
        tail = match.group(2).replace("\\", "/")
        # Whitespace terminates the expression, so the system Program Files
        # directories are recognized from the full line rather than the tail.
        rest = line[match.start():].casefold()
        if re.match(r"[a-z]:[\\/]+program files(?: \(x86\))?(?:[\\/]|[\"'])", rest):
            continue
        first = tail.split("/", 1)[0].casefold()
        if first == "users":
            continue  # HOME_PATH handles this with its stricter username rule.
        if first in SAFE_PATH_FIRST or first.startswith(("$", "${", "%")):
            continue
        return True
    for match in UNIX_WORKSPACE.finditer(line):
        if match.group().startswith(SYSTEM_UNIX_PREFIXES):
            continue
        first = match.group(1).split("/", 1)[0].casefold()
        if first not in SAFE_PATH_FIRST and not first.startswith(("$", "${", "%")):
            return True
    return False


def _email_allowed(domain: str) -> bool:
    domain = domain.casefold()
    return (domain in {"noreply.github.com", "users.noreply.github.com"}
            or any(domain == base or domain.endswith("." + base)
                   for base in ("example.com", "example.org", "example.net")))


def _address_allowed(value: str, prefix: str) -> bool:
    if ":" in value and not re.search(r"[0-9A-Fa-f]", value):
        return True  # Bare :: is also a Python classifier/slice delimiter.
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True  # Numeric identifiers that are not addresses.
    if address.is_loopback or address.is_unspecified:
        return True
    if any(address.version == net.version and address in net for net in DOC_NETWORKS):
        return True
    # A dotted package version is only exempt when explicitly labelled as one.
    if address.version == 4 and VERSION_PREFIX.search(prefix):
        return True
    return False


def _text_findings(text: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        rules: set[str] = set()
        synthetic = bool(SYNTHETIC_MARKER.search(line))
        protocol = PROTOCOL_CLIENT_ASSIGNMENT.fullmatch(line)
        protocol_id = protocol.group(1) if protocol and protocol.group(1) == PUBLIC_PROTOCOL_CLIENT_ID else None
        if any(not _email_allowed(match.group(1)) for match in EMAIL.finditer(line)):
            rules.add("non-example email")
        for expression in (IPV4, IPV6):
            if any(not _address_allowed(match.group(), line[:match.start()])
                   for match in expression.finditer(line)):
                rules.add("non-example IP address")
        if any(match.group(1).casefold() not in ALLOWED_HOME_USERS
               and not match.group(1).startswith(("$", "%"))
               for match in HOME_PATH.finditer(line)):
            rules.add("personal home path")
        for name, expression in SECRET_RULES.items():
            if expression.search(line):
                rules.add(name)
        if any(not _placeholder(match.group(1))
               for match in TOKEN_ASSIGNMENT.finditer(line)):
            rules.add("credential assignment")
        if not synthetic and _private_workspace(line):
            rules.add("private workspace path")
        if PRIVATE_REPOSITORY.search(line):
            rules.add("private repository link")
        if not synthetic:
            if any(_literal_identifier(match.group(1)) and not _synthetic_identifier(match.group(1))
                   for match in IDENTIFIER_ASSIGNMENT.finditer(line)):
                rules.add("literal device or account identifier")
            if any(not _synthetic_identifier(match.group()) and match.group() != protocol_id
                   for match in UUID.finditer(line)):
                rules.add("literal session UUID")
        if any(not _placeholder(match.group(2))
               and (match.group(1).casefold() in {"password", "passwd"} or len(match.group(2)) >= 16)
               for match in LITERAL_CREDENTIAL.finditer(line)):
            rules.add("credential assignment")
        if any(not _placeholder(match.group(1)) for match in BEARER.finditer(line)):
            rules.add("bearer credential")
        if any(not _placeholder(match.group(1)) for match in CUSTOM_HEADERS.finditer(line)):
            rules.add("literal authentication headers")
        if any(not _placeholder(match.group(1)) for match in OAUTH_QUERY.finditer(line)):
            rules.add("OAuth credential in URL")
        findings.extend((number, rule) for rule in sorted(rules))
    return findings


def _skip(parts: tuple[str, ...], include_build: bool = False) -> bool:
    skipped = SKIP_DIRS - {"build", "dist"} if include_build else SKIP_DIRS
    return any(part in skipped or (part.endswith(".egg-info") and not include_build) for part in parts)


def check_text(text: str, label: str = "input") -> list[str]:
    """Inspect an already-decoded diff or source without echoing matched values."""
    return [f"{label}:{number}: {rule}" for number, rule in _text_findings(text)]


def _record_path(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and name == path.as_posix()
                and "\\" not in name and ":" not in name and name != EXPORT_RECORD
                and not any(part in {".", ".."} or part.casefold() in SKIP_DIRS
                            or part.casefold() == EXPORT_RECORD or part.endswith((".", " "))
                            or part.casefold().endswith(".egg-info")
                            or part.split(".", 1)[0].casefold() in WINDOWS_DEVICES
                            for part in path.parts))


def _record_findings(label: str, data: bytes, read_member) -> list[str]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate receipt field")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_pairs)
        if (not isinstance(value, dict) or set(value) != {"format", "files"}
                or type(value["format"]) is not int or value["format"] != 1
                or not isinstance(value["files"], dict)):
            raise ValueError("receipt schema")
        for name, digest in value["files"].items():
            if not isinstance(name, str) or not _record_path(name):
                return [f"{label}:1: unsafe export receipt path"]
            if not isinstance(digest, str) or not HEX_DIGEST.fullmatch(digest):
                return [f"{label}:1: invalid export receipt digest"]
            if hashlib.sha256(read_member(name)).hexdigest() != digest:
                return [f"{label}:1: export receipt digest does not match source"]
    except (UnicodeError, ValueError, TypeError, OSError, KeyError):
        return [f"{label}:1: export receipt schema or source could not be verified"]
    return []


def check_export_record(root: Path) -> list[str]:
    """Validate only content hashes in the managed export receipt; never trust its values."""
    root = Path(root).resolve()
    record = root / EXPORT_RECORD
    if not record.exists() and not record.is_symlink():
        return []

    def reject_link(path: Path) -> None:
        for item in (path, *path.parents):
            if item == root:
                break
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise OSError("receipt source is linked")

    def read(name: str) -> bytes:
        path = root / name
        reject_link(path)
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise OSError("receipt source is missing or outside root")
        return path.read_bytes()

    try:
        reject_link(record)
        return _record_findings(EXPORT_RECORD, record.read_bytes(), read)
    except OSError:
        return [f"{EXPORT_RECORD}:1: export receipt could not be read safely"]


def _member_findings(label: str, data: bytes) -> list[str]:
    path = PurePosixPath(label)
    name = path.name.casefold()
    findings = []
    relative = PurePosixPath(label.split("!/", 1)[-1])
    if name == EXPORT_RECORD and len(relative.parts) != 1:
        findings.append(f"{label}:1: nested export receipt is not permitted")
    if (name in STATE_FILES or path.suffix.casefold() in PRIVATE_SUFFIXES
            or any(part.casefold() in PRIVATE_DIRS for part in path.parts[:-1])
            or name == ".env" or (name.startswith(".env.") and name != ".env.example")):
        findings.append(f"{label}:1: local state or credential dump")
    if data.startswith(PNG_SIGNATURE):
        offset = len(PNG_SIGNATURE)
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            end = offset + length + 12
            if end > len(data):
                return findings + [f"{label}:1: malformed PNG artifact"]
            if kind not in SAFE_PNG_CHUNKS:
                findings.append(f"{label}:1: image metadata requires removal or separate review")
            offset = end
            if kind == b"IEND":
                if offset != len(data):
                    findings.append(f"{label}:1: appended image data")
                return findings
        return findings + [f"{label}:1: malformed PNG artifact"]
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "binary artifact")
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return findings + [f"{label}:1: binary artifact requires separate review"]
    return findings + check_text(content, label)


def check_tree(root: Path, *, include_build: bool = False) -> list[str]:
    """Return redacted diagnostics for the source files under *root*."""
    root = Path(root).resolve()
    if not root.is_dir():
        return [".:1: source directory not found"]
    findings: list[str] = check_export_record(root)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _skip(relative.parts, include_build):
            continue
        label = relative.as_posix()
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            findings.append(f"{label}:1: symlink is not permitted in a public export")
            continue
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f"{label}:1: source file could not be read")
            continue
        if path.suffix.casefold() in {".whl", ".zip", ".gz", ".tar"}:
            findings.extend(check_archive(path, label=label))
        else:
            findings.extend(_member_findings(label, data))
    return findings


def check_archive(path: Path, *, label: str | None = None) -> list[str]:
    """Scan wheel/ZIP or sdist/tar members in memory, without extracting them."""
    path = Path(path)
    label = label or path.name
    findings: list[str] = []
    seen: set[str] = set()
    total = 0
    member_data: dict[str, bytes] = {}

    def inspect(name: str, size: int, *, is_link: bool, read) -> None:
        nonlocal total
        member = PurePosixPath(name)
        if (not name or member.is_absolute() or ".." in member.parts or "\\" in name
                or ":" in name or name != member.as_posix() or name in seen):
            findings.append(f"{label}:1: unsafe or duplicate archive member")
            return
        seen.add(name)
        target = f"{label}!/{name}"
        if is_link:
            findings.append(f"{target}:1: archive link is not permitted")
            return
        total += size
        if total > MAX_ARCHIVE_BYTES or len(seen) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive size limit")
        data = read()
        member_data[name] = data
        findings.extend(_member_findings(target, data))

    try:
        with path.open("rb") as stream:
            header = stream.read(10)
        if header.startswith(b"\x1f\x8b"):
            if len(header) != 10:
                raise ValueError("incomplete gzip header")
            if header[3] & 0x1c:
                findings.append(f"{label}:1: gzip filename, comment or extra metadata requires removal")
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                if archive.comment:
                    findings.append(f"{label}:1: archive comment requires removal or separate review")
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    if item.comment:
                        findings.append(f"{label}:1: archive member comment requires removal or separate review")
                    mode = (item.external_attr >> 16) & 0o170000
                    inspect(item.filename, item.file_size, is_link=mode == 0o120000,
                            read=lambda item=item: archive.read(item))
        else:
            with tarfile.open(path, "r:*") as archive:
                for item in archive:
                    if item.isdir():
                        continue
                    if item.uname or item.gname or item.uid or item.gid:
                        findings.append(f"{label}:1: archive owner metadata requires removal or separate review")
                    def read(item=item):
                        stream = archive.extractfile(item)
                        if stream is None:
                            raise ValueError("unreadable archive member")
                        with stream:
                            return stream.read()
                    inspect(item.name, item.size, is_link=not item.isfile(), read=read)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, RuntimeError):
        findings.append(f"{label}:1: archive could not be safely inspected")
    for name, data in member_data.items():
        if PurePosixPath(name).name == EXPORT_RECORD:
            parent = PurePosixPath(name).parent
            def read_member(member, parent=parent):
                return member_data[(parent / member).as_posix()]
            findings.extend(_record_findings(f"{label}!/{name}", data, read_member))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", action="store_true", help="Inspect a wheel/ZIP or tar sdist")
    parser.add_argument("--include-build", action="store_true", help="Also inspect build/dist outputs")
    args = parser.parse_args(argv)
    findings = check_archive(args.root) if args.archive else check_tree(args.root, include_build=args.include_build)
    for finding in findings:
        print(finding)
    if findings:
        print(f"Public source check failed: {len(findings)} finding(s).")
        return 1
    print("Public source check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
