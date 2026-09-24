# Working on ccpick

Use Python 3.12 or newer. Install with `python -m pip install -e .`.

Before proposing a change, run:

```sh
python tools/check_public.py
python -m unittest discover -s tests -v
python tools/test_core.py
```

Expected result: exit 0 for each command. Platform-specific regression skips are printed and are not counted as passing checks. CI also builds the distribution and checks desktop resources.

`ccpick doctor` is read-only. `ccpick setup --dry-run` and `ccpick uninstall --dry-run` describe desktop changes. Do not run login, account switching, setup, or uninstall against a maintainer's real account merely to test a code change. Use a temporary home and mocked backend commands, as in `tests/`.

`src/ccpick_app/backend.py` owns calls to the pinned credential backend. `runtime.py` owns platform paths. `setup.py` owns integration; `service.py` owns background execution. The decision engine is in `legacy/autoswitch/claude-autoswitch-decide.py`.

Keep credentials, OAuth URLs/codes, browser profile data, account exports, local status, and user logs out of this repository. Diagnostic failures should identify a file or field without echoing sensitive values. Only documentation-domain email fixtures belong in tests.

Automatic authorization clickers and headless login are outside this public package's scope.
