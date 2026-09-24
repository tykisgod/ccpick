# Working on ccpick

If the user asks you to install or configure ccpick on their computer, follow [AGENT_SETUP.md](AGENT_SETUP.md). The instructions below concern repository development; a user installation should use the tagged package and the setup playbook, not an editable checkout.

Use Python 3.12 or newer. Install with `python -m pip install -e .`.

Before proposing a change, run:

```sh
python tools/check_public.py
python tools/test_package.py
python tools/test_core.py
```

Expected result: exit 0 for each command. Platform-specific regression skips are printed and are not counted as passing checks. CI also builds the distribution and checks desktop resources.

`ccpick doctor` is read-only. `ccpick setup --dry-run` and `ccpick uninstall --dry-run` describe desktop changes. Do not run login, account switching, setup, or uninstall against a maintainer's real account merely to test a code change. Use a temporary home and mocked backend commands, as in `tests/`.

`src/ccpick_app/backend.py` owns calls to the pinned credential backend. `runtime.py` owns platform paths. `setup.py` owns integration; `service.py` owns background execution. The decision engine is in `legacy/autoswitch/claude-autoswitch-decide.py`.

Keep credentials, OAuth URLs/codes, browser profile data, account exports, local status, and user logs out of this repository. Diagnostic failures should identify a file or field without echoing sensitive values. Only documentation-domain email fixtures belong in tests.

Automatic authorization, batch enrollment, headless mode, and User-Agent overrides are explicitly invoked features. Their tests must use synthetic pages and subprocess mocks; never authorize a real account or change a user's Chrome profile as a test. Preserve manual login and report unsupported browser behavior without claiming success.
