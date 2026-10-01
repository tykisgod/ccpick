"""Opt-in local account runtime. No runtime configuration means legacy mode.

Installation does not import existing credentials or alter the global Claude
launcher. Only ``ccpick run`` and its native process tree use this runtime.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import runtime

NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
ERROR = re.compile(r"[a-z_]{1,80}\Z")
_UNGUARDED = object()


class ManagerError(RuntimeError):
    pass


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _private_tree(path: Path, boundary: Path) -> None:
    """Reject redirects inside the owned tree before resolving or writing it."""
    current = path
    while True:
        if _linked(current):
            raise ManagerError("unsafe_path")
        if current == boundary:
            return
        if current == current.parent:
            raise ManagerError("unsafe_path")
        current = current.parent


def _install_path(candidate: Path) -> Path:
    if not candidate.is_absolute() or len(candidate.parents) < 4 or ".." in candidate.parts:
        raise ManagerError("runtime_installation_invalid")
    service = candidate.parent.parent.parent
    _private_tree(candidate, service)
    # OS/user aliases are allowed outside our runtime namespace. Resolve that
    # anchor only; account/config links inside it must never be normalized away.
    anchor = service.parent
    canonical = anchor.resolve() / candidate.relative_to(anchor)
    _private_tree(canonical, canonical.parent.parent.parent)
    return canonical


def installation() -> Path:
    hint = os.environ.get("CCPICK_RUNTIME_INSTALL")
    if os.environ.get("CCPICK_ACCOUNT_RUNTIME") == "1" and hint:
        candidate = Path(hint)
        return _install_path(candidate)
    return _install_path(runtime.data_dir() / "profile-runtime" / "private" / "account-profiles" / "install.json")


def resources() -> Path:
    return Path(__file__).resolve().parent / "profile_runtime"


def read(path: Path):
    if _linked(path) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ManagerError("unsafe_file")
    if os.name != "nt" and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ManagerError("private_permissions_required")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_proxy(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or
                not parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or
                parsed.query or parsed.fragment):
            raise ValueError()
    except (ValueError, TypeError):
        raise ManagerError("upstream_proxy_invalid") from None
    return value.rstrip("/")


def enabled() -> bool:
    return os.path.lexists(installation())


def backend():
    if not enabled():
        return None
    # Validate before returning a compatibility module: corrupt opt-in state
    # must not fall back to writing the legacy global credential store.
    Manager()
    return sys.modules[__name__]


def manager():
    return Manager() if enabled() else None


def _secure_directory(path: Path) -> None:
    for current in (path, *path.parents):
        if _linked(current):
            raise ManagerError("unsafe_path")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for current in (path, *path.parents):
        if _linked(current):
            raise ManagerError("unsafe_path")
    if os.name != "nt":
        path.chmod(0o700)


def _windows_permissions(root: Path) -> None:
    # Local account names can contain non-ASCII characters; use the current SID.
    if os.name != "nt":
        return
    query = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True,
                           encoding="utf-8", errors="replace", check=True)
    match = re.search(r"S-1-5-[0-9-]+", query.stdout)
    if not match:
        raise ManagerError("private_permissions_unavailable")
    result = subprocess.run(["icacls.exe", str(root), "/inheritance:r", "/grant:r",
                             "*" + match[0] + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ManagerError("private_permissions_unavailable")


def setup(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ccpick runtime setup")
    parser.add_argument("--upstream-proxy", required=True, help="User-owned loopback HTTP CONNECT proxy")
    parser.add_argument("--native", help="Absolute Claude native executable path")
    parser.add_argument("--node", help="Absolute Node.js executable path")
    parser.add_argument("--dry-run", action="store_true")
    ns = parser.parse_args(args)
    if sys.platform not in ("win32", "darwin"):
        raise ManagerError("runtime_platform_unsupported")
    proxy = validate_proxy(ns.upstream_proxy)
    node = ns.node or shutil.which("node")
    native = ns.native or shutil.which("claude")
    launcher = runtime.launcher_path()
    if not all((node, native, launcher)):
        raise ManagerError("runtime_prerequisites_missing")
    node_path, native_path = Path(node).absolute(), Path(native).absolute()
    if (not node_path.is_file() or not native_path.is_file() or
            native_path.suffix.lower() in (".cmd", ".bat", ".ps1", ".py", ".sh")):
        raise ManagerError("native_executable_required")
    try:
        probe = subprocess.run([str(node_path), "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10)
        version = re.fullmatch(r"v([0-9]+)\.[0-9]+\.[0-9]+\s*", probe.stdout)
        if probe.returncode or not version or int(version[1]) < 22:
            raise ValueError()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ManagerError("node_22_required") from None
    openssl = (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
               if sys.platform == "win32" else Path("/usr/bin/openssl"))
    if not openssl.is_file():
        raise ManagerError("runtime_openssl_required")
    file = installation()
    if file.exists():
        current = Manager()
        if (current.install["upstreamProxy"] != proxy or Path(current.install["native"]) != native_path or
                Path(current.install["node"]) != node_path):
            raise ManagerError("runtime_installation_conflict")
        print(json.dumps({"ok": True, "action": "already_configured", "dryRun": ns.dry_run}))
        return 0
    root = file.parent
    service = root.parent.parent
    config = {"version": 1, "platform": sys.platform, "serviceRoot": str(service),
              "dataRoot": str(root / "data"), "native": str(native_path), "node": str(node_path),
              "python": sys.executable, "browser": launcher, "accountBrowser": launcher,
              "networkProfile": "public", "upstreamProxy": proxy, "seamlessAccounts": True,
              "legacyInputHistory": True}
    if ns.dry_run:
        print(json.dumps({"ok": True, "action": "configure_runtime", "dryRun": True,
                          "dataDirectory": str(root), "globalBackendModified": False,
                          "systemTrustModified": False, "accountsImported": 0}))
        return 0
    from .backend import require_backend
    require_backend()
    for directory in (service, service / "private", root, root / "data"):
        _secure_directory(directory)
    _windows_permissions(service)
    if file.exists() or (root / "state.json").exists() or (root / "template.json").exists() or any((root / "data").iterdir()):
        raise ManagerError("runtime_installation_conflict")
    claim = root / ".setup.lock"
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ManagerError("runtime_setup_busy") from None
    os.close(descriptor)
    try:
        if file.exists() or (root / "state.json").exists() or (root / "template.json").exists() or any((root / "data").iterdir()):
            raise ManagerError("runtime_installation_conflict")
        state = {"version": 2, "enabled": True, "selected": None, "selectedAt": None, "maintenance": False}
        write(root / "state.json", state)
        try:
            base = Path.home() / ".claude"
            shared = [name for name in ("CLAUDE.md", "skills", "commands", "agents")
                      if (base / name).exists() and not (base / name).is_symlink()]
            if shared:
                write(root / "template.json", {"version": 1, "sharedResources": shared})
            write(file, config)
        except Exception:
            if read(root / "state.json") == state:
                (root / "state.json").unlink()
            (root / "template.json").unlink(missing_ok=True)
            raise
    finally:
        claim.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "action": "configured", "accountsImported": 0,
                      "next": "ccpick runtime add --email account@example.com"}))
    return 0


def usage_windows(data):
    if not isinstance(data, dict):
        return {}
    result = {}
    for item in data.get("limits", []) if isinstance(data.get("limits"), list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind in ("session", "weekly_all") and item.get("scope") is not None:
            continue
        name = {"session": "5h", "weekly_all": "7d"}.get(kind)
        if kind == "weekly_scoped":
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            if (not isinstance(name, str) or not name.strip() or name.lower() in ("5h", "7d") or
                    not all(character.isprintable() for character in name)):
                continue
        pct = item.get("percent")
        if name and type(pct) in (int, float) and math.isfinite(pct):
            result.setdefault(name, {"utilization": pct, "resets_at": item.get("resets_at")})
    for key, name in (("five_hour", "5h"), ("seven_day", "7d"), ("seven_day_opus", "opus"),
                      ("seven_day_sonnet", "sonnet"), ("seven_day_fable", "fable")):
        value = data.get(key)
        pct = value.get("utilization") if isinstance(value, dict) else None
        if type(pct) in (int, float) and math.isfinite(pct) and not any(key.lower() == name for key in result):
            result[name] = {"utilization": pct, "resets_at": value.get("resets_at")}
    return result


class Manager:
    def __init__(self, root: Path | None = None, home: Path | None = None):
        self.root = Path(root) if root else installation().parent
        if not self.root.is_absolute() or self.root.resolve() != self.root or not self.root.is_dir():
            raise ManagerError("unsafe_path")
        _private_tree(self.root / "install.json", self.root.parent.parent)
        _private_tree(self.root / "data", self.root)
        self.home = home or Path.home()
        self.install = read(self.root / "install.json")
        service = self.root.parent.parent
        if (self.install.get("version") != 1 or self.install.get("platform") != sys.platform or
                self.install.get("seamlessAccounts") is not True or
                Path(self.install.get("dataRoot", "")) != self.root / "data" or
                Path(self.install.get("serviceRoot", "")) != service or
                not all(isinstance(self.install.get(key), str) and Path(self.install[key]).is_absolute()
                        for key in ("node", "native", "python", "browser"))):
            raise ManagerError("runtime_installation_invalid")
        validate_proxy(self.install.get("upstreamProxy"))
        self.state = read(self.root / "state.json")
        if self.state.get("version") != 2 or self.state.get("enabled") is not True:
            raise ManagerError("registry_not_enabled")

    def selection(self):
        state = read(self.root / "state.json")
        return {"selected": state.get("selected"), "selectedAt": state.get("selectedAt")}

    def profiles(self):
        result = []
        for directory in sorted((self.root / "data").iterdir()):
            if not NAME.fullmatch(directory.name):
                continue
            if _linked(directory) or not directory.is_dir() or directory.resolve() != directory:
                raise ManagerError("unsafe_profile")
            _private_tree(directory / "claude", self.root)
            if not (directory / "profile.json").exists():
                continue
            profile = read(directory / "profile.json")
            if (profile.get("name") != directory.name or profile.get("version") != 1 or
                    profile.get("storage") is not None):
                raise ManagerError("profile_invalid")
            policy_file = directory / "switch-policy.json"
            policy = read(policy_file) if policy_file.exists() else {"version": 1, "autoSwitchEnabled": True}
            if (not isinstance(policy, dict) or set(policy) != {"version", "autoSwitchEnabled"} or
                    type(policy["version"]) is not int or policy["version"] != 1 or type(policy["autoSwitchEnabled"]) is not bool):
                raise ManagerError("profile_invalid")
            profile.update(root=directory, directory=directory / "claude", config=directory / "claude" / ".claude.json",
                           autoSwitchEnabled=policy["autoSwitchEnabled"])
            if profile.get("account"):
                ids = read(profile["config"])
                profile["account"] = {**profile["account"],
                    "organizationUuid": ids.get("oauthAccount", {}).get("organizationUuid")}
            result.append(profile)
        return result

    def find(self, query):
        matches = [profile for profile in self.profiles() if str(query).casefold() in
                   {str(value).casefold() for value in (profile["name"], profile.get("legacySlot"),
                     profile.get("email"), profile.get("label"), (profile.get("account") or {}).get("email"))
                    if value is not None}]
        if len(matches) != 1:
            raise ManagerError("account_not_found" if not matches else "ambiguous_account")
        return matches[0]

    def current(self, context=False):
        selection = self.selection()
        if not selection["selected"]:
            raise ManagerError("login_required")
        if context and os.environ.get("CCPICK_ACCOUNT_RUNTIME") == "1":
            value = self._node("context")
            profile = self.find(value["profile"]["name"])
        else:
            profile = self.find(selection["selected"])
        return {**profile, "selectedAt": selection["selectedAt"]}

    def _node(self, command, args=(), *, capture=True, payload=None, timeout=180):
        argv = [self.install["node"], str(resources() / "portable.mjs"), str(self.root / "install.json"),
                command, *map(str, args)]
        kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"} if capture else {}
        if payload is not None:
            kwargs["input"] = json.dumps(payload)
        try:
            process = subprocess.run(argv, timeout=timeout, **kwargs)
        except (OSError, subprocess.TimeoutExpired):
            raise ManagerError("runtime_unavailable") from None
        if not capture:
            return process.returncode
        if process.returncode:
            reason = (process.stderr or "").strip()
            raise ManagerError(reason if ERROR.fullmatch(reason) else "runtime_unavailable")
        try:
            value = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise ManagerError("runtime_response_invalid") from None
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ManagerError("runtime_response_invalid")
        return value

    def select(self, query, cached_only=False, allow_login=False, expected_state=_UNGUARDED):
        profile = self.find(query)
        if expected_state is not _UNGUARDED:
            if not isinstance(expected_state, dict) or set(expected_state) != {"selected", "selectedAt"}:
                raise ManagerError("selection_changed")
            reply = self._node("select-guarded", (profile["name"], expected_state["selected"], expected_state["selectedAt"]))
            receipt = reply.get("selectionReceipt")
            if (not isinstance(receipt, dict) or set(receipt) != {"profileId", "generation"} or
                    receipt["profileId"] != profile["name"] or not isinstance(receipt["generation"], str)):
                raise ManagerError("runtime_response_invalid")
            return {**profile, "selectionReceipt": receipt}
        self._node("select", (profile["name"],))
        return self.find(profile["name"])

    def native(self, args):
        if not args:
            return 1
        return self._node(args[0], args[1:], capture=False, timeout=None)

    def ensure_ready(self, profile, timeout=75):
        return self._node("ready", (profile["name"],), timeout=timeout)

    def verify(self, profile):
        ids = read(profile["config"])
        for key in ("userID", "machineID"):
            value = ids.get(key)
            if (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) or
                    hashlib.sha256(value.encode()).hexdigest() != profile.get("identity", {}).get(key)):
                raise ManagerError("identity_changed")
        account = profile.get("account")
        if account and ids.get("oauthAccount", {}).get("accountUuid") != account.get("uuid"):
            raise ManagerError("wrong_account")

    def credentials(self, profile):
        self.verify(profile)
        if sys.platform == "darwin":
            from claude_swap.session import read_config_dir_credentials, keychain_service_name
            raw = read_config_dir_credentials(str(profile["directory"]), strict_keychain=True,
                                              keychain_service=keychain_service_name(str(profile["directory"])))
            return json.loads(raw or "{}")
        file = profile["directory"] / ".credentials.json"
        return read(file) if file.exists() else {}

    def request_usage(self, profile, timeout=90):
        started = time.monotonic()
        self.ensure_ready(profile, timeout=max(1, timeout - 15))  # The one runtime service owns refresh.
        token = self.credentials(profile).get("claudeAiOauth", {}).get("accessToken")
        if not isinstance(token, str) or not token or re.search(r"[\x00-\x20\x7f]", token):
            raise ManagerError("login_required")
        proxy = urllib.parse.urlsplit(validate_proxy(self.install["upstreamProxy"]))
        request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ccpick-runtime/1", "Accept": "application/json"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ManagerError("redirect_refused")
        request.add_unredirected_header("Host", request.host)
        request.set_proxy(proxy.netloc, "http")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=max(1, min(15, timeout - (time.monotonic() - started)))) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ManagerError("usage_response_invalid")
            return json.loads(raw)

    def _poll_plan(self, profile, old, data, now):
        from claude_swap import poll_policy
        def normalized(raw):
            result = {"scoped": []}
            for name, window in usage_windows(raw).items():
                item = {"pct": window["utilization"], "resets_at": window.get("resets_at")}
                if name in ("5h", "7d"):
                    result["five_hour" if name == "5h" else "seven_day"] = item
                else:
                    result["scoped"].append({"name": name, **item})
            return result
        models = tuple(name.strip().lower() for name in os.environ.get("CCSWITCH_MODELS", "").split(",")
                       if name.strip() and name.strip().lower() != "none")
        last429 = old.get("last429At")
        recent429 = type(last429) in (int, float) and math.isfinite(last429) and 0 <= now - last429 <= poll_policy.RECENT_429_WINDOW_S
        previous_interval = old.get("pollIntervalS")
        if type(previous_interval) not in (int, float) or not math.isfinite(previous_interval) or previous_interval <= 0:
            previous_interval = None
        next_poll, interval = poll_policy.plan_after_fetch(
            prev_interval_s=previous_interval, prev_usage=normalized(old.get("data")), new_usage=normalized(data),
            is_active=self.selection()["selected"] == profile["name"],
            threshold=float(os.environ.get("CCSWITCH_THRESHOLD", "90")), models=models,
            recent_429=recent429, now=now)
        return {"nextPollAt": next_poll, "pollIntervalS": interval}

    def refresh(self, force=False, slots=None, budget_s=None):
        from .service import tick_lock
        requested = {str(value) for value in slots} if slots is not None else None
        budget = budget_s if type(budget_s) in (int, float) and math.isfinite(budget_s) and budget_s > 0 else 180
        deadline = time.monotonic() + budget
        with tick_lock(self.root / "usage.lock") as acquired:
            if not acquired:
                return
            for profile in self.profiles():
                if not profile.get("account"):
                    continue
                if requested is not None and profile["name"] not in requested and str(profile.get("legacySlot", profile["name"])) not in requested:
                    continue
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    break
                file = profile["root"] / "usage.json"
                old = read(file) if file.exists() else {}
                now = time.time()
                next_poll = old.get("nextPollAt", 0)
                waiting = type(next_poll) in (int, float) and math.isfinite(next_poll) and next_poll > now
                if old.get("error") == "http-429" and waiting:
                    continue
                if not force and requested is None and waiting:
                    continue
                try:
                    data = self.request_usage(profile, timeout=min(90, remaining))
                    if not any(key in usage_windows(data) for key in ("5h", "7d")):
                        raise ManagerError("usage_schema_changed")
                    now = time.time()
                    result = {"fetchedAt": now, "attemptAt": now, "data": data, "error": None,
                              **self._poll_plan(profile, old, data, now)}
                    if old.get("fetchedAt") and old.get("data"):
                        result["previousSample"] = {"fetchedAt": old["fetchedAt"], "data": old["data"]}
                except urllib.error.HTTPError as error:
                    result = {**old, "error": "http-" + str(error.code), "errorAt": now,
                              "attemptAt": now, "nextPollAt": now + (1800 if error.code == 429 else 300)}
                    if error.code == 429:
                        result["last429At"] = now
                except Exception as error:
                    reason = str(error) if isinstance(error, ManagerError) and ERROR.fullmatch(str(error)) else "network_unavailable"
                    result = {**old, "error": reason, "errorAt": now, "attemptAt": now, "nextPollAt": now + 300}
                write(file, result)

    def collect(self):
        selected = self.selection()["selected"]
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for profile in self.profiles():
            file = profile["root"] / "usage.json"
            cache = read(file) if file.exists() else {}
            row = {"slot": profile.get("legacySlot", profile["name"]), "profile": profile["name"],
                   "email": (profile.get("account") or {}).get("email", profile.get("email", "")),
                   "active": profile["name"] == selected, "autoSwitchEnabled": profile["autoSwitchEnabled"],
                   "error": cache.get("error"), "fetched_at": cache.get("fetchedAt"), "windows": {},
                   "denied": cache.get("errorAt") if cache.get("error") == "http-403" else None,
                   "nextPollAt": cache.get("nextPollAt")}
            if not profile.get("account"):
                row["error"] = "login_required"
            elif not row["error"]:
                fetched = row["fetched_at"]
                next_poll = cache.get("nextPollAt")
                valid_until = fetched + 600 if type(fetched) in (int, float) and math.isfinite(fetched) else 0
                if type(next_poll) in (int, float) and math.isfinite(next_poll):
                    valid_until = max(valid_until, next_poll)
                if not fetched or time.time() > valid_until:
                    row["error"] = "usage_not_current"
            if isinstance(cache.get("previousSample"), dict):
                row["previousSample"] = cache["previousSample"]
            for name, window in usage_windows(cache.get("data")).items():
                try:
                    reset = dt.datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00"))
                    if reset.tzinfo is None:
                        reset = None
                except (ValueError, KeyError, TypeError, AttributeError):
                    reset = None
                expired = bool(reset and reset <= now)
                pct = window["utilization"]
                row["windows"][name] = {"pct": None if expired else pct, "expired": expired,
                    "stale_pct": pct if expired else None, "resets_at": window.get("resets_at"),
                    "at": reset.astimezone().strftime("%m-%d %H:%M") if reset else "—",
                    "in": str(max(0, round((reset - now).total_seconds() / 60))) + " min" if reset else "—"}
            rows.append(row)
        return rows


def collect():
    return Manager().collect()


def browser(url: str) -> int:
    value = Manager()
    value._node("browser-register", payload={"url": url})
    runtime.bootstrap()
    import ccpick
    # The registration above is complete. Call the picker directly instead of
    # invoking BROWSER again, which would recurse through this same command.
    return ccpick.handle_url(url)


def dispatch(tool, args):
    if not enabled():
        return None
    value = Manager()
    args = list(args)
    command = args[0] if args else "accounts"
    if command == "auto":
        return None  # The compatibility bridge delegates to runtime_decision.
    if command in ("accounts", "list", "usage", "quota"):
        if "--refresh" in args or "--force" in args:
            value.refresh(force="--force" in args)
        rows = value.collect()
        if "--json" in args:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                windows = "  ".join(name + ": " + str(window["pct"]) + "%" for name, window in row["windows"].items())
                print(("▶ " if row["active"] else "  ") + str(row["slot"]) + "  " + row["email"] +
                      ("  [manual only]" if not row["autoSwitchEnabled"] else "") + "  " + windows +
                      ("  " + row["error"] if row["error"] else ""))
        return 0
    if command == "status":
        print(json.dumps({"ok": True, **value.selection(), "managed": True}))
        return 0
    if command in ("disable", "enable", "auto-policy"):
        query = args[1] if len(args) > 1 else ""
        policy = args[2] if command == "auto-policy" and len(args) > 2 else ("off" if command == "disable" else "on")
        print(json.dumps(value._node("auto-policy", (query, policy))))
        return 0
    if command == "switch" and len(args) in (2, 3):
        value.select(args[1], allow_login=False)
        print(json.dumps({"ok": True, **value.selection()}))
        return 0
    if command in ("guard", "session-event", "login-finished") and os.environ.get("CCPICK_ACCOUNT_RUNTIME") == "1":
        return 0
    if command == "doctor":
        print(json.dumps(value._node("doctor")))
        return 0
    raise ManagerError("runtime_command_unsupported")


def main(args: list[str]) -> int:
    if not args or args[0] in ("--help", "-h"):
        print("ccpick runtime setup --upstream-proxy http://127.0.0.1:PORT [--dry-run]\n"
              "ccpick runtime add --email ACCOUNT [--label NAME]\n"
              "ccpick runtime login [ACCOUNT] | switch ACCOUNT | status | usage [--refresh] | doctor\n"
              "ccpick run -- CLAUDE_ARGUMENTS")
        return 0
    command, tail = args[0], args[1:]
    if command == "setup":
        return setup(tail)
    if not enabled():
        raise ManagerError("runtime_not_configured")
    value = Manager()
    if command == "run":
        return value.native(["run", *(tail[1:] if tail[:1] == ["--"] else tail)])
    if command == "add":
        parser = argparse.ArgumentParser(prog="ccpick runtime add")
        parser.add_argument("email_positional", nargs="?")
        parser.add_argument("--email")
        parser.add_argument("--label")
        ns = parser.parse_args(tail)
        if bool(ns.email_positional) == bool(ns.email):
            parser.error("provide one email, either EMAIL or --email EMAIL")
        return value.native(["add", ns.email or ns.email_positional, *([ns.label] if ns.label else [])])
    if command == "login":
        parser = argparse.ArgumentParser(prog="ccpick runtime login",
                                        description="Reauthorize an enrolled account; choose Chrome's profile in the browser picker.")
        parser.add_argument("account", nargs="?")
        ns = parser.parse_args(tail)
        return value.native(["login", *([ns.account] if ns.account else [])])
    if command == "auto":
        from .runtime_decision import run
        return run(value, tail)
    return dispatch("ccpick", [command, *tail])
