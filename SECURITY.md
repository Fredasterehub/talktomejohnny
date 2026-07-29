# Security Policy

TalkToMeJohnny uses GitHub private vulnerability reporting for undisclosed
security issues. Do not open a public issue for an unpatched vulnerability.

## Report A Vulnerability

Use the repository's GitHub private disclosure flow. Include:

- affected version or commit
- platform and environment details
- clear reproduction steps
- relevant logs or screenshots with secrets removed
- whether the issue touches voices, caches, settings, hooks, or transport

## Safe Reporting Rules

- Do not publish exploit details before a fix is available.
- Do not include tokens, passwords, API keys, or private voice material.
- If the issue affects migration, say whether legacy `talktomeclaude` data is
  still reachable.

## Scope Notes

- Secrets, tokens, and operator-specific local configuration must never be
  committed.
- Voice references, caches, and migrated user settings are compatibility data,
  not public fixtures.
- Hook and companion changes should fail closed when lifecycle ownership or
  attachment state is unclear.
- Codex hook installation does not grant trust. Review current TalkToMeJohnny
  definitions through Codex `/hooks` before enabling them.
- On Windows, the generated encoded PowerShell hook payload contains only the
  resolved executable path and fixed hook arguments; it must never include
  tokens, transcripts, settings, or voice material.
- Run `talktomejohnny doctor` after upgrades. It reports optional Diffusers,
  Transformers, and setuptools installs below the fixed cloning security floors.

Maintainers should acknowledge a report within 7 days.

The public release audit for this version is recorded in
[`docs/SECURITY_AUDIT_0.1.4.md`](docs/SECURITY_AUDIT_0.1.4.md).
