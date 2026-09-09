# Pursers Home quickstart and recovery guide

<!--
DRAFT GATE: Do not submit this guide until TK-a3f0627d27db provides the final
assembled SHA, deterministic package receipt, and independently observed browser
labels. Recheck every visible label below against that exact installed package,
record the accepted dependency SHAs, then remove this comment.
-->

Pursers Home lets you connect one project, join standalone worker and reviewer
seats, create work, follow its progress, and read independently reviewed
results. This guide follows the board-managed standalone flow. It does not use
Aion Team Mode to create or control Pursers seats.

## What the main terms mean

| Term | Meaning |
| --- | --- |
| Project | One connected Pursers board in one trust domain |
| Ticket | One persisted work request on that board |
| Seat | One standalone worker or reviewer identity |
| Worker | Claims and completes tickets |
| Reviewer | Independently checks submitted work |
| Door | A coordinator-issued credential for one board and role |
| Seat group | Optional organization for existing seats; it does not start or stop them |
| Result | A bounded view of submitted artifacts and the independent review outcome |

WORK and PERSONAL projects are separate trust domains. Never connect a WORK
door to a PERSONAL project, combine their seats into one group, or treat their
results as one stream.

## Before you start

Ask your operator for:

1. The exact verified Pursers Home package approved for your environment.
2. The helper URL and one-time local access token.
3. The project board name and whether it belongs to WORK or PERSONAL.
4. A worker door or reviewer door for each role you need.
5. A unique seat name for every standalone seat.

The operator should also give you the package SHA-256 or another release receipt
that identifies the exact artifact. Do not install an unverified ZIP, unpack an
extension into an existing AionUi data directory, or reuse a helper configured
for another board.

## 1. Install the verified Home package

Install the exact package supplied by your operator through the managed AionUi
extension path used by your organization. The current AionUi host does not
provide a supported in-app import flow for an arbitrary local ZIP, so the
operator may need to prepare the extension before you open AionUi.

After installation:

1. Open AionUi Settings.
2. Open Pursers Home.
3. Confirm the Home page loads and shows the connection setup rather than a
   blank page or a raw JSON response.
4. Confirm the package receipt shown by the operator matches the installed
   candidate.

Installing the package alone does not connect a project or prove that the
helper is running.

## 2. Connect the authenticated Home helper

AionUi serves the Home page as static extension content. Live Pursers actions
go through a separate authenticated helper bound to one loopback origin and one
board. Your operator must start that helper before you connect a door.

In the Home helper section:

1. Enter the loopback helper URL supplied by your operator.
2. Enter the one-time local access token.
3. Choose **Connect helper**.
4. Confirm **Helper status** shows the expected board and a connected state.

The helper URL and token remain in page memory only. Enter them again after a
reload. Home must reject a wrong origin, token, host, or board; do not work
around those errors by weakening browser or helper security.

If the helper does not connect, stop here and use [Helper recovery](#helper-recovery).

## 3. Check and connect a door

A door is scoped to one board and one role. A board-role door may record more
than one seat name, but every joined seat still has a distinct Central identity.

In **Connect this project**:

1. Paste the coordinator-issued value into **Door string**.
2. Enter the exact **Seat name**.
3. Select the correct **Role**: worker or reviewer.
4. Confirm the tier ceiling and workspace folder supplied for this seat.
5. Choose **Check door**.
6. Review the redacted board, role, key ID, expiry, and transport details.
7. Choose **Connect project**.
8. Confirm **Connection status** shows the expected board, role, and seat.

The door value is cleared after use and must never appear in results, logs,
screenshots, or ticket notes. The helper stores it through the wait bridge in a
private state directory. Home never receives a Central credential.

Door replacement is explicit. **Connect project** does not silently rotate a
stored door; use **Replace door** only when you intend to replace the existing
board-role credential.

## 4. Join and check a standalone seat

Use the standalone seat lifecycle controls for the connected board. Aion Team
controls, Fleet API workers, and seat-kit are not substitutes for this step.

1. Select the preserved seat name and role from the connected project.
2. Join the seat.
3. Confirm the result shows the exact board, `agent_id`, `principal_id`, seat
   name, role, and lifecycle returned by Central.
4. Refresh status before creating work.

A normal joined seat reports `active`. Home reports `handed_off`, `stale`,
`retired`, and `unknown` honestly rather than presenting them as active. Follow
the recovery message shown for that exact state. Rejoining an existing seat is
allowed only with the preserved board, name, role, agent ID, and principal ID.
A different identity is a conflict, not a replacement.

Do not retire a seat merely because it is temporarily idle. Safe disconnect is
covered in [Pause and safe stop](#pause-and-safe-stop).

## 5. Organize seats if useful

**Seat groups** organize existing seats on the current board. A group does not
join, start, pause, retire, dispatch, or grant authority to a seat.

To create one:

1. Choose **Refresh groups**.
2. Enter a unique group name.
3. Select existing worker or reviewer members from this board.
4. Choose **Create group**.

Edits use a saved revision. If another user changes the same group first, Home
reports a conflict and asks you to refresh instead of overwriting their change.
Removing a group removes only its metadata; seats, board membership, tickets,
and history remain intact.

## 6. Create work

Use **Ticket lifecycle** to create real unassigned work on the connected board.

1. Enter a short title and a complete description.
2. Enter the project-relative target URL.
3. Choose the scope, priority, and tier.
4. List the required result fields, such as `branch_and_commit`,
   `files_changed`, and `test_output`.
5. Add tags or related files only when they help route the work.
6. Choose **Create unassigned ticket**.

Home creates an unassigned ticket. It never claims, renews, submits, assigns,
or reviews work on a seat's behalf. The board dispatches eligible work to a
standalone worker, and an independent reviewer owns the final verdict.

Cancellation is also authority-checked by Central. Home cannot bypass the
creator, current-executor, or reviewer rule.

## 7. Follow progress and answer approvals

Choose **Refresh tickets** to read persisted board state. Treat the displayed
state as the source of truth:

- `open`: available for dispatch or returned for correction;
- `claimed`: held by a worker with an active lease;
- `needs_human`: waiting for a human answer;
- `submitted`: waiting for independent review;
- `closed`: independently approved;
- `parked` or `cancelled`: not active work.

Pursers Home does not invent progress from local processes. If the host cannot
render a human-input form, use the operator-provided dashboard or coordinator
fallback to answer the request. Verify the exact board and ticket before
submitting an answer.

### What the worker does

A standalone worker follows this loop:

1. Wait on the board's push subscription using the last saved positive cursor.
2. Save the complete returned cursor map.
3. Claim only a live offer addressed to that identity.
4. Work in an isolated ticket worktree and renew the lease about every 3
   minutes, including before long checks.
5. Push a feature branch and submit exact branch, full commit SHA, changed
   files, and test evidence.
6. Release the work slot after a successful submit and immediately re-arm for
   the next eligible ticket. The worker does not wait for review while holding
   the submitted slot.
7. If review rejects the ticket and the board offers the correction, create a
   fresh successor and follow the latest fix instructions.

Never reset the event cursor to zero and never replace push subscription with a
polling fallback. A reviewer uses a separate identity and never performs worker
tasks.

## 8. Read reviewed results

In **Submitted results**, choose **Refresh results**. Each row reports one
state:

- `missing`: no safe submitted artifact is available;
- `pending`: submitted and awaiting review;
- `approved`: independently reviewed and closed;
- `rejected`: changes were requested;
- `failed`: the bounded result projection could not be produced.

The result view may show the latest submission summary, safe branch and commit,
bounded changed-file references, reviewer identity, review outcome, status
transition, and current cursor when those fields are available. It deliberately
does not expose submission notes or review notes. Open the authorized ticket
detail or use the coordinator workflow when you need the full correction text.

Do not treat `submitted` or `pending` as complete. Work is complete only after
an independent approval closes the ticket.

## Recovery

### Helper recovery

If Home reports that the helper is unavailable:

1. Confirm the URL is loopback and matches the operator-provided origin.
2. Re-enter the current local access token; it is not retained across reloads.
3. Ask the operator to verify that the helper is running for the exact Central
   label, board, AionUi origin, token file, and bridge-state directory.
4. Choose **Connect helper** again, then refresh connection and seat status.

Do not paste a door into the helper-token field or a helper token into ticket
text.

### Expired, revoked, or replaced door

If door validation reports expired or unauthorized:

1. Ask the coordinator for a replacement for the same board and role.
2. Confirm that the existing project entry is the one you intend to replace.
3. Paste the replacement door and choose **Replace door**.
4. Recheck the redacted key ID and expiry.
5. Rejoin only the preserved seat identity and refresh status.

Rotation requires an existing board-role entry. It is never automatic.

### Partial connection

If Central accepted the door but MCP registration did not finish, Home reports
a partial result. Keep the stored door and choose **Recover registration**.
Recovery must not ask you to paste the door again or create a duplicate seat.

### Seat lifecycle recovery

- `stale`: reconnect the helper, refresh Central state, then rejoin the same
  seat if instructed.
- `handed_off`: use the original door and exact seat name, or ask the operator
  to inspect the handoff.
- `unknown`: refresh live state; if it remains unknown, rejoin only the
  preserved identity or ask the operator to inspect Central.
- `retired`: rejoin the same identity when you intend to reactivate it.
- identity mismatch or duplicate: stop and ask the operator to resolve it. Do
  not retire, forget, or replace an identity to make the warning disappear.

### Ticket recovery

- `backend_unavailable`: keep the form contents, reconnect the helper, and
  retry after status works.
- `permission_denied`: use an authorized principal; Home performs no fallback
  mutation.
- `ticket_not_found`: refresh and select a persisted ticket.
- `conflict`: refresh because the ticket changed or became terminal.
- invalid input: correct the named field before retrying.

### Result recovery

A missing result is not a failure if the ticket has not been submitted. For a
`failed` result, reconnect the helper and confirm the operator selected the
correct Central label and board. Home refuses duplicate board IDs when the
Central label is absent or mismatched.

### Pause and safe stop

There is no board-wide pause or resume operation in the standalone Home flow.
Do not stop Central to control a seat: that halts dispatch but does not safely
finish an active agent process.

To pause work, let the worker checkpoint according to its seat instructions.
To disconnect or retire a standalone seat safely:

1. Finish, submit, or release any active ticket so no work or review lease
   remains.
2. Refresh the seat's live Central status.
3. Review the exact board and seat name.
4. Type the exact confirmation shown by Home.
5. Request safe disconnect.
6. Wait for Central to report the same identity as `retired` before local door
   state is removed.

If Central retirement succeeds but local door removal fails, Home reports a
recoverable partial result. Retry disconnect with the same confirmation; do not
delete private state files manually. Pursers never needs arbitrary process
killing for this flow.

## Support matrix

| Capability | Beginner path | Boundary |
| --- | --- | --- |
| Install Home | Operator-managed verified package | Exact candidate receipt required |
| Connect helper | Home helper section | Loopback, exact Origin, token, Central, and board |
| Validate/connect door | **Connect this project** | Explicit replacement only |
| Join/status seat | Standalone seat lifecycle controls | Exact preserved identity |
| Organize seats | **Seat groups** | Metadata only |
| Create/track work | **Ticket lifecycle** | No Home claim, submit, or review |
| Answer human request | Authorized dashboard/coordinator fallback | AionUi native form may be unavailable |
| Read results | **Submitted results** | Read-only; notes are stripped |
| Renew worker lease | Seat-managed board CLI | About every 3 minutes while claimed |
| Pause all seats | Not supported | Checkpoint individual work instead |
| Safe stop | Confirmed standalone retirement | Active lease blocks retirement |
| Native Aion Team Mode | Compatibility only | Not the supported Pursers seat path |
| Fleet dashboard | Operator administration/troubleshooting | Not a beginner seat workflow |

## Operator and advanced setup

The beginner happy path assumes the operator already installed the exact
candidate and started its authenticated helper. Repository builds, seat-kit,
Fleet dashboard administration, credentials, and service management belong in
this section, not in the normal user flow.

The helper must be started from the exact installed candidate and bound to all
of these values:

```sh
umask 077
openssl rand -hex 32 > /PATH/TO/pursers-home-token
node host/helper.cjs \
  --board sandbox-example \
  --central work \
  --origin http://127.0.0.1:25808 \
  --token-file /PATH/TO/pursers-home-token \
  --bridge-state-dir /PATH/TO/isolated-bridge-state \
  --bridge-bin /PATH/TO/pursers-wait-bridge \
  --aioncore-bin /PATH/TO/aioncore \
  --fleet-url http://127.0.0.1:8899 \
  --core-version 0.2.1
```

Use the real connected board for normal work and a separate `sandbox-*` board
for acceptance. The token file must be a regular mode-0600 file, the host and
origin must be loopback, and the bridge-state directory must not be shared with
another trust domain. Never place tokens, doors, private paths, or personal
hostnames in screenshots, commits, or ticket notes.

For headless diagnosis, the wait bridge supports explicit door operations:

```sh
pursers-wait-bridge join '<DOOR>'
pursers-wait-bridge status
pursers-wait-bridge join --rotate '<REPLACEMENT_DOOR>'
```

These are troubleshooting tools, not the preferred beginner flow. A worker's
seat-local `bin/board.sh` is likewise reserved for its governed work loop.

## Current limitations

- AionUi serves the extension WebUI as static content; live actions require the
  authenticated loopback helper.
- Home does not claim, renew, submit, assign, or review tickets for a seat.
- Home result projection strips submission and review notes.
- Native AionUi elicitation may be unavailable; use the authorized fallback.
- Standalone seat groups organize metadata only.
- Native Team routes may remain for compatibility, but they do not satisfy the
  supported standalone seat flow.
- Source tests, mocks, static screenshots, and package presence do not prove the
  final browser flow. The exact installed candidate requires independent
  authenticated browser acceptance.

## Accepted implementation inputs

The final submitted guide must record the exact accepted component and assembly
SHAs used by the installed package. At draft time the approved standalone
components are:

| Capability | Accepted SHA |
| --- | --- |
| Ticket lifecycle | `0b0f0b78385a0315aa4e93e574fa967d0cef65ab` |
| Submitted results | `792dcae5dfe1a194329471fd914f0f6b1518db27` |
| Standalone groups | `20bb6bc5c54ad7b233dfc790a48d3bea335a92da` |
| Standalone seat lifecycle | `557fbc8af9362031dee6f18db84e3c604f4d133f` |
| Fleet session and deployment input | `e229dcb08879666475c532fd7296f4c5a2d9167b` |

Before submission, add the final approved assembly SHA, package filename,
package SHA-256, observed AionUi host build, and independent browser acceptance
receipt. Do not infer any of those values from this source draft.
