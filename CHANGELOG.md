# Changelog

## 0.2.0 — 2026-10-01

- Add an opt-in Windows/macOS account runtime with separate native identities and credentials, request-level account snapshots, and proxy-only API/OAuth/usage transport.
- Preserve managed conversations, local background workers, native fork arguments, Resume history, and input history across account selections.
- Route managed native login through the profile picker and import verified authorization into the account registry.
- Prevent stale automatic decisions from overriding a newer selection; recheck automatic eligibility at the final switch boundary.
- Mark manual-only accounts in purple, keep a single authoritative selected item, and display missing/expired usage and dynamic model windows accurately.
- Update predictive switching, watch-only services, notification scheduling, and isolated regression coverage.
- Scan public source and wheel/sdist outputs for local data and credentials; remove image provenance metadata from exports.

- Add an agent installation playbook and copyable setup prompts at the top of both READMEs.
- Add an original ccpick icon to the repository header.

## 0.1.0 — 2026-09-24

- Initial public alpha with an automatically installed, pinned claude-swap dependency.
- Chrome profile selection, manual account enrollment, cached usage, and verified switching.
- Opt-in automatic authorization, batch enrollment, and experimental headless/User-Agent options.
- Predictive automatic switching with Windows tray and macOS menu bar integration.
- Standalone setup/uninstall, disabled-account filtering, and Linux data-path support.
- Isolated regression tests, package builds, and privacy checks on three operating systems.
