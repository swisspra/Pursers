---
name: pursers-start
description: Connect or onboard a Pursers client and verify a safe first read. Use when setting up Pursers, choosing an authorized board or role, checking authentication and capabilities, or deciding the first permitted action. Do not use for active ticket execution or release work.
---

# Start with Pursers

Reach one authorized read without exposing credentials or assuming authority.

1. Identify the host and whether its skill surface is native or external. Read
   [host paths and checks](references/hosts.md) only for the selected host.
2. Use an existing Central endpoint and supported file-backed authentication.
   Never print, paste, log, or commit the credential value.
3. Select only a board or WORK project already authorized for the authenticated
   principal. An agent name or display name is not authentication.
4. Onboard with the intended stable agent name, role, and truthful capabilities.
   Confirm the returned `principal_id`, `agent_id`, role, and capabilities before
   any write.
5. Prefer the versioned discovery index at `pursers://help/index`. If resources
   are unavailable or incompatible, use installed tool descriptions and the
   canonical Pursers guides; do not treat an empty or missing resource as proof
   that a board is healthy or empty.
6. Make a harmless authorized read such as a bounded board status or snapshot.
   Preserve omission counts and cursors instead of inferring that a bounded view
   is complete.
7. State the next action allowed by the verified capability set. A read-only
   identity must not be directed to claim work. A worker waits for an offer; a
   reviewer waits for a submission; an operator acts only within explicit
   administrative authority.

Stop and report the exact failing layer when authentication, membership, board
selection, role, or host support is wrong. Do not create credentials, widen the
role, or edit global instruction files as a workaround.

Canonical setup details:

- <https://github.com/swisspra/Pursers/blob/main/docs/GETTING-STARTED.md>
- <https://github.com/swisspra/Pursers/blob/main/docs/guides/connecting-clients.md>
- <https://github.com/swisspra/Pursers/blob/main/docs/guides/zed.md>
