# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 5.0.0b1 beta | Yes, until superseded by a newer beta |
| Earlier prereleases | No |

Beta interfaces may change. Security fixes normally target the newest beta
rather than older prerelease lines.

## Report a vulnerability

Use GitHub's
[private vulnerability reporting form](https://github.com/swisspra/Pursers/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include the affected version or commit, component, impact, reproduction steps,
and any safe proof of concept. Redact credentials, private paths, board data,
and personal identifiers. The maintainers will use the private advisory to
coordinate validation, remediation, and disclosure.

## Scope

Security reports may cover authentication or authorization bypass, credential
or sensitive-data exposure, cross-board access, unsafe archive/import behavior,
and remote code execution in a supported Pursers component.

The following are out of scope:

- ordinary bugs, feature requests, or documentation corrections without a
  security impact;
- unsupported versions or modified builds that cannot be reproduced on the
  supported beta;
- attacks that require an already trusted local OS user or process on the same
  single-owner machine, unless they cross a documented Pursers boundary;
- vulnerabilities solely in third-party services or MCP hosts that Pursers
  does not maintain;
- denial-of-service testing against infrastructure you do not own or lack
  permission to test;
- reports containing live credentials, private board contents, or personal
  data that are unnecessary to demonstrate the issue.

For non-security problems, use the repository's bug-report form.
