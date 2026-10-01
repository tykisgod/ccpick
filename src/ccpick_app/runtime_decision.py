"""Run the shared predictive policy against the optional portable runtime.

Only account I/O is replaced. Each switch supplies the original selection
generation and uses its committed receipt to advance a possible multi-hop chain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import re
from pathlib import Path
from types import SimpleNamespace
import time

from . import runtime


def load_policy():
    source = runtime.bootstrap() / "autoswitch/claude-autoswitch-decide.py"
    spec = importlib.util.spec_from_file_location("_ccpick_portable_policy", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("automatic_policy_unavailable")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    # Keep changes scoped to this check, rather than changing shared usage imports.
    policy.usage = SimpleNamespace(**vars(policy.usage))
    return policy


def _selection(value):
    if (not isinstance(value, dict) or not isinstance(value.get("selected"), str) or
            not isinstance(value.get("selectedAt"), str) or not value["selectedAt"]):
        raise RuntimeError("account_selection_invalid")
    return {"selected": value["selected"], "selectedAt": value["selectedAt"]}


def _receipt(value, name):
    receipt = value.get("selectionReceipt") if isinstance(value, dict) else None
    if (not isinstance(receipt, dict) or set(receipt) != {"profileId", "generation"} or
            receipt.get("profileId") != name or not isinstance(receipt.get("generation"), str) or
            not receipt["generation"]):
        raise RuntimeError("account_selection_invalid")
    return {"selected": receipt["profileId"], "selectedAt": receipt["generation"]}


def evaluate(managed, policy, *, dry_run=False, refresh=True, manual=False):
    selection = _selection(managed.selection())
    active = managed.current()
    if active.get("name") != selection["selected"]:
        raise RuntimeError("account_selection_changed")
    email_of = lambda p: (p.get("account") or {}).get("email", p.get("email", ""))
    watch_only = policy.watch_only
    if refresh:
        # Keep a scheduled check bounded even with a large account pool or an
        # unavailable proxy; later decisions freshen their needed candidates.
        managed.refresh(budget_s=30)
    profiles = {}
    source_rows = {}
    failures = []

    def rows_from_cache():
        profiles.clear()
        profiles.update({p["name"]: p for p in managed.profiles()})
        rows = []
        for row in managed.collect():
            name = row.get("profile") or str(row.get("slot") or "")
            profile = profiles.get(name)
            if profile is None:
                profile = next((p for p in profiles.values()
                                if str(p.get("legacySlot", p["name"])) == str(row.get("slot"))), None)
            if profile is None or email_of(profile) != row.get("email"):
                raise RuntimeError("account_roster_changed")
            name = profile["name"]
            source_rows[name] = row
            windows = row.get("windows") or {}
            used = {key: window["pct"] for key, window in windows.items()
                    if isinstance(window, dict) and not window.get("expired") and
                    type(window.get("pct")) in (int, float) and
                    math.isfinite(window["pct"]) and 0 <= window["pct"] <= 100}
            counted = policy.usage.counted_windows(used)
            binding = max(counted, key=counted.get) if counted else ""
            fetched = row.get("fetched_at")
            age = time.time() - fetched if type(fetched) in (int, float) and math.isfinite(fetched) else math.inf
            complete = all(key in counted for key in ("5h", "7d")) and 0 <= age <= 600
            plan = profile.get("plan") or {}
            account = profile.get("account") or {}
            rows.append({
                "slot": str(row["slot"]), "profile": name, "email": row["email"],
                "autoSwitchEnabled": profile.get("autoSwitchEnabled") is not False,
                "used": used, "counted": counted,
                "worstUsed": counted.get(binding) if complete else None,
                "binding": binding, "resetsAt": (windows.get(binding) or {}).get("resets_at") or "",
                "resets": {key: window.get("resets_at") or "" for key, window in windows.items()
                           if isinstance(window, dict)},
                "age": age, "error": row.get("error") if complete else "usage_not_current",
                "denied": bool(row.get("denied")), "deniedAt": row.get("deniedAt") or row.get("denied"),
                "deniedLast": row.get("deniedLast") or row.get("denied"),
                "attemptAt": row.get("attemptAt") or fetched or 0, "fetchedAt": fetched or 0,
                "scale": plan.get("scale") or policy.usage.DEFAULT_SCALE, "plan": plan.get("plan") or "?",
                "org": plan.get("org") or account.get("organizationUuid") or "",
                "cap": policy.usage.capacity(counted, plan.get("scale")) if complete else None,
            })
        return rows

    policy.rows_from_cache = rows_from_cache
    policy.active_email = lambda: email_of(managed.current())
    policy.enroll_active = lambda: (False, "managed_profile_required")
    policy.usage.collect = lambda **_kwargs: managed.collect()
    policy.usage.switch_in_times = lambda: {
        str(row["slot"]): policy._iso_ts(managed.selection()["selectedAt"])
        for row in rows_from_cache() if row["profile"] == managed.current()["name"]}
    policy.usage.recent_403_times = lambda: {
        str(row["slot"]): [row["deniedLast"]] for row in rows_from_cache() if row.get("deniedLast")}

    def refresh_cache(slots, budget, **_kwargs):
        before = rows_from_cache()
        stale = [str(slot) for slot in slots if any(
            row["slot"] == str(slot) and (row["age"] > policy.FRESH_S or row["error"])
            for row in before)]
        if refresh and stale:
            # Scheduled polling can defer inactive accounts for hours. The
            # decision's explicit freshness request must refresh just its
            # stale candidates, while the manager preserves 429 backoff.
            managed.refresh(force=True, slots=stale, budget_s=budget)
        rows = rows_from_cache()
        return all(any(row["slot"] == str(slot) and row["age"] <= policy.FRESH_S and
                       not row["error"] for row in rows) for slot in slots)

    policy.refresh = refresh_cache
    original_sample = policy.record_sample

    def same_reset(left, right):
        try:
            values = [dt.datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (left, right)]
            return all(value.tzinfo is not None for value in values) and abs((values[0] - values[1]).total_seconds()) <= 1
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def record_sample(email, used, data_ts):
        row = next((row for row in source_rows.values() if row.get("email") == email), {})
        windows = row.get("windows") or {}
        resets = {key: value.get("resets_at") for key, value in windows.items() if isinstance(value, dict)}
        saved = policy._read(policy.SAMPLES)
        previous = saved.get("resets") or {}
        if (saved.get("email") != email or set(previous) != set(resets) or
                any(not same_reset(value, previous.get(key)) for key, value in resets.items())):
            saved = {"email": email, "samples": [], "resets": resets}
        saved["resets"] = resets
        if not resets or not all(isinstance(value, str) and value for value in resets.values()):
            saved["samples"] = []
        policy._write_atomic(policy.SAMPLES, saved)
        original_sample(email, used, data_ts)

    policy.record_sample = record_sample

    def switch(args, timeout=60):
        nonlocal selection
        if len(args) != 2 or args[0] != "switch":
            raise RuntimeError("automatic_policy_invalid_operation")
        if _selection(managed.selection()) != selection:
            raise RuntimeError("account_selection_changed")
        if dry_run or watch_only():
            raise RuntimeError("watch_only_enabled")
        target = managed.find(args[1])
        if target.get("autoSwitchEnabled") is False:
            raise RuntimeError("account_manual_only")
        try:
            chosen = managed.select(args[1], allow_login=False, expected_state=selection)
        except RuntimeError as error:
            reason = str(error)
            if reason in ("selection_changed", "account_selection_changed", "selection_guard_unsupported",
                          "watch_only_enabled", "account_manual_only", "migration_in_progress"):
                raise
            # An unusable grant is a failed target, not permission to discard
            # selection protection or replay a request. The shared policy may
            # try another eligible account within its existing attempt limit.
            failures.append(reason if re.fullmatch(r"[a-z_]{1,80}", reason) else "account_operation_failed")
            return SimpleNamespace(returncode=1)
        selection = _receipt(chosen, target["name"])
        if _selection(managed.selection()) != selection:
            raise RuntimeError("account_selection_changed")
        return SimpleNamespace(returncode=0)

    policy._cswap = switch
    policy.time = SimpleNamespace(time=time.time, sleep=lambda _seconds: None)
    rows = rows_from_cache()
    current = next((row for row in rows if row["profile"] == active["name"]), None)
    if current and current["worstUsed"] is None and not current["denied"]:
        refresh_cache([current["slot"]], budget=2)
        current = next((row for row in rows_from_cache() if row["profile"] == active["name"]), None)
    if not current or (current["worstUsed"] is None and not current["denied"]):
        result = {"action": "stay", "why": "usage_not_current", "fresh": False}
    else:
        result = policy.decide_predictive(dry_run, allow_switch=not watch_only(), manual=manual)
    actual = managed.current()
    displayed = next((row for row in rows_from_cache() if row["profile"] == actual["name"]), {})
    result.update(active=email_of(actual), activeEmail=email_of(actual), windows=displayed.get("used", {}),
                  used=displayed.get("worstUsed"), countedWindows=list(displayed.get("counted", {})),
                  scope="following_sessions", threshold=policy.CONSIDER_AT, consider=policy.CONSIDER_AT,
                  watchOnly=watch_only(),
                  fresh=bool(displayed and displayed.get("worstUsed") is not None and not displayed.get("error")))
    if result.get("action") == "stay" and result.get("wouldSwitchTo"):
        result["action"] = "would-switch"
    if result.get("action") == "would-switch":
        result.setdefault("wouldSwitchTo", result.get("to"))
    result.setdefault("nextCheckS", 60)
    if failures:
        result["targetFailures"] = failures
    if result.get("soonestAt"):
        if any(row["worstUsed"] is None or row["error"] or
               not policy.soonest_recovery([row], time.time()) for row in rows_from_cache()):
            result.pop("soonestAt", None)
            result.pop("soonest", None)
    return result


def run(managed, args):
    parser = argparse.ArgumentParser(prog="ccpick auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force", action="store_true", help="Evaluate a manual request; automatic-only rules still apply")
    parser.add_argument("--model", help="Count these model windows too, comma-separated, or all")
    ns = parser.parse_args(args)
    if ns.model is not None and (len(ns.model) > 256 or any(not c.isprintable() for c in ns.model)):
        parser.error("invalid model window")
    previous_model = os.environ.get("CCSWITCH_MODELS")
    try:
        if ns.model is not None:
            os.environ["CCSWITCH_MODELS"] = ns.model
        result = evaluate(managed, load_policy(), dry_run=ns.dry_run, refresh=not ns.no_refresh, manual=ns.force)
    finally:
        if ns.model is not None:
            if previous_model is None:
                os.environ.pop("CCSWITCH_MODELS", None)
            else:
                os.environ["CCSWITCH_MODELS"] = previous_model
    print(json.dumps(result, ensure_ascii=False))
    return {"stay": 2, "switched": 0, "would-switch": 0, "blocked": 3}.get(result.get("action"), 1)
