# Security Policy

## Supported Versions

Security fixes are targeted at the latest published `main` branch state and the
most recent tagged release.

## Reporting A Vulnerability

Do not open a public issue for an unpatched security vulnerability.

Report it privately through GitHub security reporting if that surface is
available for the repository. If it is not, contact the repository owner
directly through GitHub and include:

- A short description of the issue.
- The affected version or commit.
- Reproduction steps or a minimal proof of concept.
- Any suggested mitigation, if you have one.

You should receive an acknowledgement within 7 days.

## Scope Notes

- Secrets, tokens, and operator-specific local configuration must never be
  committed.
- Voice references, caches, and migrated user settings are compatibility data,
  not public test fixtures.
- Hook and companion changes should fail closed when lifecycle ownership or
  attachment state is unclear.
