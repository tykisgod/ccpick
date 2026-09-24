# Install and configure ccpick as a coding agent

This is the installation playbook for an agent working on the user's computer. Follow it when the user asks you to set up ccpick. For repository development instead, use [AGENTS.md](AGENTS.md).

Target release: **v0.1.0 (alpha)**, Python **3.12+**, automatically installed backend **claude-swap 0.26.0**. Background integration supports Windows and macOS; Linux supports the command line. Reply in the user's language.

## Working agreement

Do the installation and verification yourself within the user's request. Do not stop at a list of commands, and do not ask for approval again for work already authorized. The README's copyable prompt requests browser integration and automatic switching on Windows/macOS; a user who requests only the CLI should get only that.

Briefly describe the selected setup before applying it. Request input only for a missing account/profile choice, interactive login or OS permission, or replacing an existing installation when replacement has not been authorized. Continue independent work while waiting. Automatic authorization, headless mode, custom User-Agent, and Chrome JavaScript settings are optional features, not requirements for installation.

Keep credentials, cookies, OAuth URLs/codes, browser files, and account exports on the user's machine. Report account counts and redacted identifiers instead of pasting raw account/status output into chat or issues. Do not force-close the user's browser or migrate their account store as an installation shortcut.

## 1. Inspect the environment

Identify the OS, shell, current user, and existing `uv`, `git`, `claude`, Chrome, and `ccpick` installations. Inspect `uv tool list` and executable locations so a legacy `ccpick` on PATH is not mistaken for the package you install.

Preserve existing `BROWSER`, `CLAUDE_CONFIG_DIR`, `CCPICK_DATA_DIR`, `CCPICK_CHROME_USER_DATA_DIR`, `XDG_DATA_HOME`, and `ZDOTDIR` settings. Record only relevant values locally. Do not introduce a temporary home or new account directory for a real installation: existing claude-swap accounts are intentionally shared. `CCPICK_DATA_DIR` changes ccpick state, not the credential store.

Resolve missing prerequisites using their official instructions: [uv installation](https://docs.astral.sh/uv/getting-started/installation/), [Git](https://git-scm.com/downloads), [Claude Code setup](https://code.claude.com/docs/en/setup), and [Chrome](https://www.google.com/chrome/). Use the user's existing package manager where suitable. On macOS, menu bar installation also needs `swiftc` from Xcode Command Line Tools; `xcode-select --install` opens Apple's installer if these are missing. Complete user-interactive steps with the user, then resume.

## 2. Install the release and resolve its launcher

Install a persistent tool environment, outside a Git checkout:

```sh
uv tool install --python 3.12 git+https://github.com/tykisgod/ccpick.git@v0.1.0
```

If this exact public release is already installed, reuse it. If an existing tool must be updated, inspect its origin first; use uv's reinstall option only as part of the requested update. Do not overwrite a different installation just to make the command succeed. Do not install a separate global `cswap`, use an editable checkout, or launch background services through `uv run`.

Resolve the executable from uv rather than trusting a stale PATH entry.

Windows PowerShell:

```powershell
$ccpickBin = Join-Path (uv tool dir --bin) 'ccpick.exe'
& $ccpickBin --version
& $ccpickBin doctor --json
```

macOS/Linux shell:

```sh
ccpick_bin="$(uv tool dir --bin)/ccpick"
"$ccpick_bin" --version
"$ccpick_bin" doctor --json
```

Below, `ccpick` means this resolved executable. Use `& $ccpickBin ...` or `"$ccpick_bin" ...` until PATH is verified. If uv reports its bin directory is missing from PATH, fix the user's shell integration using uv's instructions and verify the command resolves correctly in a new terminal.

Check the doctor's `ccpick`, `python`, `launcher`, `backend_version`, `backend_required`, `chrome`, and `claude` fields. The backend version must equal the required version. A nonzero doctor exit needs investigation; do not hide it. Windows/Linux also check for the Python `tkinter` module. If the graphical profile picker is needed, choose a Python interpreter with working Tk using `uv tool install --python /path/to/python ...`. A user-selected `CCPICK_PROFILE` can avoid the picker, but doctor can still report missing Tk; document that limitation.

`doctor` is read-only. Its `ok` result checks prerequisites, not successful login, usable quota, a running service, or even the presence of accounts. `autoswitch_status_present` means only that a file exists.

## 3. Use existing accounts first

Inspect `ccpick accounts` and `ccpick list` locally. The first lists managed accounts; the second lists Chrome profiles. Preserve disabled accounts and the current active account. Do not enable, remove, or switch accounts merely to test installation.

If the user wants to save the currently logged-in Claude Code account, run `ccpick add`. If another account is needed, let the user complete Claude Code's `/login`, then add it. Once the browser hook is installed, restart Claude Code before testing its profile picker. Finish enrollment before starting a new automatic switcher where practical.

If no accounts are available, installation can still be completed, but report that switching cannot yet be verified. Do not claim an empty account list is a successful switching test.

### Optional automatic enrollment

Use this only when requested, with the user's selected profiles and account identities. Preview a batch first:

```sh
ccpick auto-enroll-all --profiles "Profile 1" "Profile 2" --dry-run
ccpick auto-enroll --profile "Profile 1" --email account@example.com
```

Replace the example values with the user's choices. An empty profile list can produce exit code 2 in the batch preview. Single-account `auto-enroll` has no dry-run option. Enrollment can change the active Claude account.

CDP automation needs Chrome fully closed and, on Chrome 136+, a dedicated non-default data directory selected with `CCPICK_CHROME_USER_DATA_DIR`. Have the user sign into that dedicated profile; do not copy cookies or tokens. Do not force-kill Chrome. See [browser compatibility and optional headless/User-Agent/JS-gate options](README.md#start-with-your-existing-accounts). Preserve manual login as a fallback and report automation failures accurately.

## 4. Preview and apply the requested integration

| Requested outcome | Preview | Apply |
| --- | --- | --- |
| Windows tray / macOS menu bar with automatic switching | `ccpick setup --autoswitch --dry-run` | `ccpick setup --autoswitch` |
| Browser profile picker and `/best-account`, without background switching | `ccpick setup --dry-run` | `ccpick setup` |
| Keep the existing browser hook, install `/best-account` only | `ccpick setup --no-browser --dry-run` | `ccpick setup --no-browser` |
| Linux CLI only | Inspect doctor/accounts | No setup command needed |

For Linux, browser integration is optional; do not pass `--autoswitch` or create a scheduler unless requested. The POSIX installer supports bash/zsh shell files; for another shell, use `--no-browser` and configure the shell's `BROWSER` to the exact launcher only if the user wants that hook.

Read the preview, then apply the chosen command within the existing authorization. `--autoswitch` starts real background checks immediately and can change the active account. The default switching criteria use account-wide windows; do not enable per-model criteria during routine setup.

If setup detects an older installation, preserve it until replacement is authorized. Then preview and apply `setup --autoswitch --replace-legacy`. This disables known older startup services and retains account data; it does not migrate credentials. Check for an older tray/menu bar still running and arrange a graceful exit. Uninstalling the public version later does not automatically reactivate old services.

Setup pins its Python interpreter and copies service resources into the ccpick data directory. A successful dry-run does not prove that backend validation, Swift compilation, or service registration will succeed; inspect the actual setup exit code and output.

Windows writes the user-level `BROWSER` setting; existing processes keep their old environment. On bash/zsh, setup edits the appropriate shell file. Restart the terminal and Claude Code after setup. When a check needs the new hook in the agent's current shell, set that process's `BROWSER` to the doctor's exact `launcher` value; do not add arguments to it.

## 5. Verify the installed result

Recheck `--version` and `doctor --json` using the resolved launcher. Verify the intended browser hook and the `/best-account` command under `${CLAUDE_CONFIG_DIR:-~/.claude}/commands/`. Read only the relevant command file, not nearby credentials.

If background switching was installed, inspect its registration and recent activity.

Windows PowerShell:

```powershell
Get-ScheduledTask -TaskPath '\' -TaskName 'ccpick-public-tray','ccpick-public-fallback'
Get-ScheduledTaskInfo -TaskPath '\' -TaskName 'ccpick-public-tray'
Get-ScheduledTaskInfo -TaskPath '\' -TaskName 'ccpick-public-fallback'
```

macOS:

```sh
launchctl print "gui/$(id -u)/io.github.tykisgod.ccpick.menubar"
launchctl print "gui/$(id -u)/io.github.tykisgod.ccpick.autoswitch"
```

The Windows fallback can be Ready between runs. The macOS fallback is scheduled at minutes 0, 15, 30, and 45; an idle fallback is not itself an error. Check the tray/menu bar and recent status timestamp/state in `<doctor.data_dir>/autoswitch/status.json`, with local logs if needed. Do not treat an old status file as proof of health.

For a recommendation without changing accounts, `ccpick auto --dry-run --json` may refresh usage cache. `ccpick autoswitch tick --dry-run` also writes status/log/lock and may refresh usage data; its guarantee is no account switching, not no writes. Use these when appropriate to the requested setup, and do not run a real switch just to produce a success report.

Finish with a short report:

```text
Installed: version and exact launcher
Configured: browser hook / slash command / tray or menu bar, as requested
Accounts: count available; identifiers redacted
Verified: commands, service registration and recent activity actually checked
Still needed: login, permissions, restart, or compatibility limitations, if any
Rollback: ccpick uninstall, then uv tool uninstall ccpick
```

Distinguish package installation from browser-login and account-switching verification. Never report a real OAuth flow as tested based on CI or doctor output alone.

## Troubleshooting and rollback

| Symptom | Next action |
| --- | --- |
| Wrong version or backend found | Recheck the resolved uv launcher and its doctor paths; avoid a legacy PATH command or global `cswap`. |
| Setup refuses a Git checkout | Install the tagged release into a persistent uv tool environment, then use its launcher. |
| Missing Tk or Chrome profiles | Resolve the selected Python/profile prerequisites; do not manufacture a profile or reuse another person's browser session. |
| Old switcher detected | Preserve it; use the explicit replacement path only within the user's requested scope. |
| Hook has no effect | Restart terminal/Claude Code and inspect their BROWSER value; the installer cannot rewrite an already-running process's environment. |
| macOS menu bar build fails | Check `swiftc` and the Command Line Tools installation, then rerun setup. |
| Automation fails | Report the failing stage without OAuth details; offer manual login or the dedicated Chrome profile flow. |

When removal is requested, preview `ccpick uninstall --dry-run`, then run `ccpick uninstall` before `uv tool uninstall ccpick`, with the same data-directory environment used during setup. This restores owned hooks/commands and removes public startup services. Credentials, history, and copied resources remain. If setup partially failed, inspect its manifest and error before cleanup; do not delete whole Claude or Chrome directories.
