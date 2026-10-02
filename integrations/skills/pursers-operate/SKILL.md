---
name: pursers-operate
description: Operate an authorized Pursers board, Butler workflow, or release train. Use only for explicit coordinator, board-administrator, Butler, integration, or release-operator requests; it never grants those permissions or authorizes publishing by itself.
---

# Operate Pursers within explicit authority

1. Identify the requested operating mode and read only its reference:
   [coordinator](references/coordinator.md),
   [Butler](references/butler.md),
   [integration](references/integration.md), or
   [release](references/release.md).
2. Verify the authenticated principal, board membership, role, capabilities,
   and any generation or revision fence before a mutation. Skill presence is
   not authorization.
3. Prefer the role resource `pursers://help/roles/{role}` and relevant workflow
   resource. If unavailable or incompatible, use installed tool descriptions
   and the canonical guide named in the reference.
4. Preview policy, filesystem, configuration, integration, and release changes.
   Preserve existing files and active leases. Use compare-and-set or other
   documented concurrency controls where supplied.
5. Keep coordination, integration, and delivery as distinct facts. Ticket
   approval does not itself mean merged, released, or deployed.
6. Record actor, exact source, evidence, outcome, and rollback path. Stop on an
   authorization mismatch, conflict, incomplete gate, or unsupported host.

Do not issue credentials, alter live configuration, merge, tag, publish, deploy,
restart services, or start seats unless the current request and authenticated
role explicitly authorize that exact action.
