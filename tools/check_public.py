#!/usr/bin/env python3
"""Check a public export for local state and common private data patterns.

Diagnostics contain relative filenames, line numbers, and rule names only.
This conservative check complements a credential scanner such as Gitleaks; it
does not prove that arbitrary personal names or confidential prose are absent.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
from pathlib import Path


SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__"}
STATE_FILES = {
    "account-status.json", "profile-accounts.json", "labels.json",
    "usage-history.jsonl", ".code-inbox", ".login-pending",
    ".credentials.json", "credentials.json", "auth.json", "sequence.json",
    "usage.json", "autoswitch_state.json", "status.json",
    "claude-autoswitch-ledger.json", "claude-autoswitch-samples.json",
    "cookies", "cookies.sqlite", "login data", "local state", "web data",
}
PRIVATE_DIRS = {"credentials", "keychains", "browser-profiles"}
PRIVATE_SUFFIXES = {".jsonl", ".cswap", ".keychain", ".keychain-db"}

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                   r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,})")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]*(?![\w:])")
VERSION_PREFIX = re.compile(
    r"(?:\b(?:version|release|python)[\w-]*\s*[=:]?\s*[\"']?|\bv)$", re.I)
HOME_PATH = re.compile(
    r"(?<![\w])(?:/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]+)"
    r"([A-Za-z0-9_.@+-]+)", re.I)
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
    if address.is_loopback:
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
        if any(not _email_allowed(match.group(1)) for match in EMAIL.finditer(line)):
            rules.add("non-example email")
        for expression in (IPV4, IPV6):
            if any(not _address_allowed(match.group(), line[:match.start()])
                   for match in expression.finditer(line)):
                rules.add("non-example IP address")
        if any(match.group(1).casefold() not in ALLOWED_HOME_USERS
               for match in HOME_PATH.finditer(line)):
            rules.add("personal home path")
        for name, expression in SECRET_RULES.items():
            if expression.search(line):
                rules.add(name)
        if any(not PLACEHOLDER.fullmatch(match.group(1))
               for match in TOKEN_ASSIGNMENT.finditer(line)):
            rules.add("credential assignment")
        findings.extend((number, rule) for rule in sorted(rules))
    return findings


def _skip(parts: tuple[str, ...]) -> bool:
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts)


def check_tree(root: Path) -> list[str]:
    """Return redacted diagnostics for the source files under *root*."""
    root = Path(root).resolve()
    if not root.is_dir():
        return [".:1: source directory not found"]
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _skip(relative.parts):
            continue
        label = relative.as_posix()
        if path.is_symlink():
            findings.append(f"{label}:1: symlink is not permitted in a public export")
            continue
        if not path.is_file():
            continue
        name = path.name.casefold()
        if (name in STATE_FILES or path.suffix.casefold() in PRIVATE_SUFFIXES
                or any(part.casefold() in PRIVATE_DIRS for part in relative.parts[:-1])
                or name == ".env" or (name.startswith(".env.") and name != ".env.example")):
            findings.append(f"{label}:1: local state or credential dump")
        try:
            data = path.read_bytes()
            if b"\x00" in data:
                continue
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue  # Binary artifacts are outside this text-pattern checker.
        except OSError:
            findings.append(f"{label}:1: source file could not be read")
            continue
        findings.extend(f"{label}:{number}: {rule}"
                        for number, rule in _text_findings(content))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    findings = check_tree(args.root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"Public source check failed: {len(findings)} finding(s).")
        return 1
    print("Public source check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
