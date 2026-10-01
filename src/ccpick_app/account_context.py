"""Optional portable account adapter; absent configuration keeps the pinned backend.

An enabled adapter's failure never falls back to the shared credential store.
Only stable reason codes are displayed, never subprocess output or login material.
"""
from __future__ import annotations

import json
import re
import sys


def backend():
    from . import runtime_profiles
    return runtime_profiles.backend()


def manager():
    from . import runtime_profiles
    return runtime_profiles.manager()


def dispatch(tool, args):
    try:
        managed = manager()
        if managed is None:
            return None
        if tool == "autoswitch" or (tool in ("ccpick", "cswap") and args[:1] == ["auto"]):
            from .runtime_decision import run
            return run(managed, args[1:] if args[:1] == ["auto"] else args)
        from . import runtime_profiles
        return runtime_profiles.dispatch(tool, args)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        reason = str(error)
        if not re.fullmatch(r"[a-z_]{1,80}", reason):
            reason = "account_operation_failed"
        if tool == "autoswitch":
            print(json.dumps({"action": "error", "why": reason, "reason": reason}))
        else:
            print("Account operation failed [" + reason + "]", file=sys.stderr)
        return 1
