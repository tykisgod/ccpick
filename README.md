# ccpick

[简体中文](README.zh-CN.md) · [Releases](https://github.com/tykisgod/ccpick/releases)

Chrome profile selection, usage visibility, and predictive account switching for people who use Claude Code with multiple accounts of their own.

ccpick provides a command line, a Windows tray, and a macOS menu bar. It uses [claude-swap](https://github.com/realiti4/claude-swap) for credential storage and switching; the tested version is installed automatically in the same Python environment.

**Initial alpha release.** The package and decision logic run in CI on Windows, macOS, and Linux. Browser login and desktop startup still depend on your local Claude Code, Chrome, and OS permissions. Linux supports the command line; desktop services are Windows/macOS only.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv tool install --python 3.12 git+https://github.com/tykisgod/ccpick.git@v0.1.0
ccpick --help
ccpick doctor
```

Claude Code and Chrome must be installed separately. The Chrome profile picker also needs Python's Tk support; if your Python lacks it, use an interpreter with Tk via `uv tool install --python /path/to/python ...`, or select a profile explicitly with `CCPICK_PROFILE`.

The package is distributed from this GitHub repository. It is not currently published to PyPI.

## Start with your existing accounts

```sh
ccpick add                    # save the account currently logged into Claude Code
ccpick accounts               # managed account dashboard
ccpick usage                  # cached usage and reset times
ccpick usage --json
ccpick auto --dry-run          # rank accounts without switching
ccpick auto                   # select and verify an account
ccpick switch 2               # explicitly select a saved account
ccpick disable 2              # exclude an account from automatic selection
ccpick enable 2
```

To add another account, log in using Claude Code's `/login`, finish authorization yourself, then run `ccpick add`. After browser-hook setup, `/login` offers a Chrome profile picker. `ccpick list` lists Chrome profiles; `ccpick accounts` lists saved Claude accounts.

Authorization uses the official login page. You can also explicitly run automatic authorization and batch enrollment:

```sh
ccpick auto-enroll --profile "Profile 1" --email account@example.com
ccpick auto-enroll-all --dry-run
ccpick auto-enroll-all --profiles "Profile 1" "Profile 2"
ccpick auto-enroll --profile "Profile 1" --email account@example.com --headless
ccpick auto-enroll --profile "Profile 1" --email account@example.com --headless --user-agent "YOUR_USER_AGENT"
```

These commands may click login/authorization buttons and update the active Claude account. Use Chrome profiles you control and read the dry-run output before a batch. Normal commands do not enable headless mode or change the browser's User-Agent.

**Browser automation is experimental.** The existing CDP backend requires Chrome to be fully closed before it launches a browser. Chrome 136+ disables remote debugging of the default user-data directory. For CDP, set `CCPICK_CHROME_USER_DATA_DIR` to a dedicated non-default Chrome data directory that you have created and signed into. See [Chrome's official explanation](https://developer.chrome.com/blog/remote-debugging-port). Headless OAuth and a custom User-Agent are available options, not a guarantee that the login provider accepts the flow. A real login is not exercised in CI.

On macOS the AppleScript fallback needs Chrome's per-profile JavaScript setting. `ccpick enable-js-gate --profiles "Profile 1"` previews it; add `--apply` only after exiting Chrome. This setting permits applications already authorized to automate Chrome to run JavaScript in that profile; use `--disable --apply` to turn it back off. Windows retains its UI Automation fallback. If automation cannot proceed, use the manual `/login` flow above.

## Desktop integration

Review the setup first:

```sh
ccpick setup --autoswitch --dry-run
ccpick setup --autoswitch
```

Setup configures the browser hook and `/best-account` command. With `--autoswitch`, Windows gets a tray and scheduled fallback; macOS gets a menu bar and launchd fallback. Omit `--autoswitch` to install just the manual integration. macOS needs the Xcode Command Line Tools (`xcode-select --install`) to compile the menu bar. Open a new terminal after setup so environment changes take effect.

If an older ccpick installation is running, setup refuses to start a second automatic switcher. Use the explicit replacement option reported by setup only when you intend to replace it. Installing this Python package by itself does not change your browser hook or register background tasks.

```sh
ccpick autoswitch tick --dry-run
ccpick uninstall --dry-run
ccpick uninstall
uv tool uninstall ccpick
```

Uninstall removes ccpick's desktop integration and restores the browser hook when it still belongs to ccpick. Account credentials and usage history are retained.

## How selection works

The decision engine considers both usage and the recent consumption rate. It checks sooner when an account is approaching exhaustion, avoids recently exhausted accounts until their reset, and verifies the account after switching. Stale or failed usage reads do not become zero usage. Disabled and removed accounts are excluded from automatic selection.

By default, account-wide 5-hour and 7-day windows drive selection. Per-model windows remain visible; include them explicitly when needed:

```sh
ccpick auto --model Fable --dry-run
```

For background services, set the user environment variable `CCSWITCH_MODELS=Fable` (or `all`) before setup. Use the same setting across your shell and services.

## Data and privacy

ccpick runs locally. It uses the installed Claude Code and claude-swap commands; it has no hosted service or telemetry.

| Data | Location |
| --- | --- |
| ccpick state on Windows | `%LOCALAPPDATA%/ccpick/` |
| ccpick state on macOS | `~/Library/Application Support/ccpick/` |
| ccpick state on Linux | `${XDG_DATA_HOME:-~/.local/share}/ccpick/` |
| claude-swap data on Windows/macOS | `~/.claude-swap-backup/` |
| claude-swap data on Linux | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

`CCPICK_DATA_DIR` overrides ccpick's state directory. Usage/status files can contain account email addresses. Do not attach them unredacted to issues, and never upload credentials or account exports. Existing claude-swap accounts are shared with ccpick; the dependency version is isolated, the account store is not.

## Development

```sh
python -m pip install -e .
python tools/check_public.py
python tools/test_package.py
python tools/test_core.py
```

Tests use temporary homes and mocked account commands. They do not authorize a browser, switch real accounts, or install background services. CI also builds the package, parses PowerShell, compiles Swift on macOS, and scans Git history for secrets.

This is an independent, unofficial project, not affiliated with Anthropic. Use accounts you own and are authorized to access, and follow the applicable service terms. ccpick does not change your subscription limits.

MIT licensed. See [third-party notices](THIRD_PARTY_NOTICES.md).
