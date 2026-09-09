# Pursers for AionUi

This extension adds a guided Pursers Home plus Worker and Reviewer presets. Home
connects one project with a coordinator-issued door, prepares distinct Team
seats through a dry-run-first plan, reports bounded submitted results and
independent review outcomes, and exposes bounded pause, stop, recovery, roster
controls, and a board-backed ticket lifecycle.

## Build and install

From the repository root:

```sh
python tools/aionui-extension/build.py
```

Install `dist/pursers-aionui-0.1.0.zip` through a managed AionUi Hub entry. That
route is external: AionUi 2.2.1 ships no in-app import for a local ZIP, and this
repository documents no Hub publishing procedure, so use the isolated local host
below for verification.

For an isolated local verification host, unpack the ZIP as one extension
directory, put only that directory's parent in `AIONUI_EXTENSIONS_PATH`, and
start AionCore with the host application version it must advertise:

```sh
AIONUI_EXTENSIONS_PATH=/PATH/TO/extensions \
  /PATH/TO/aioncore --host 127.0.0.1 --port 25999 \
  --data-dir /PATH/TO/isolated-data --app-version 2.2.1
```

`--app-version` is required. It defaults to AionCore's own version, which the
loader compares against the manifest `engines.aionui` range, so a bare AionCore
filters the extension out with `engine.aionui incompatible ... required=^2.2.1
actual=0.2.1` and serves nothing. Do not unzip it into an existing AionUi data
directory by hand.

That host is API-only; it serves no HTML shell and no login page. Assets are
served, authenticated, under `/api/extensions/pursers/assets/webui/`, and a
session is obtained by posting `{"username":..., "password":...}` to `/login`.
The host does not execute extension route handlers, so `pursers/status` comes
from the packaged helper described in `host/HELPER_CONTRACT.md`, not from this
origin.

The deterministic build writes `webui/candidate.json` from exact repository
`HEAD`; the verifier-owned browser observer reads that installed asset to bind
captures to the running candidate rather than caller-supplied metadata.

Start the packaged authenticated helper before using Home. It expects
`pursers-wait-bridge` and the bundled `aioncore` binary at the explicit paths
supplied on the command line. Its read-only result route expects the local Fleet
dashboard at `http://127.0.0.1:8899` unless `--fleet-url` selects another
loopback origin. `--central` is required so duplicate board IDs across WORK and
PERSONAL cannot fall back to Fleet's default domain. See
`host/HELPER_CONTRACT.md` for the mode-0600
token file, exact AionCore origin, selected board, and isolated bridge-state
arguments.

## Set up a project

1. Open Settings, then Pursers. Enter the loopback helper URL and one-time local
   access token; neither is persisted by the page.
2. Confirm that Home shows the helper's exact selected board.
3. Paste the door supplied by your coordinator, check it, and connect. The input
   is cleared immediately and status only shows redacted metadata.
4. Open an existing AionUi Team conversation. Enter its exact Team and monitor
   lead identity plus a unique name, folder, role, and tier ceiling per seat.
5. Preview the Team plan. Starting seats requires a separate confirmation of
   that exact plan; each result is reported independently.
6. Start a new conversation and pick the matching Worker or Reviewer preset.
7. In Ticket lifecycle, create unassigned board work or refresh its real status.
   Home never claims, submits, or reviews tickets on a seat's behalf.
8. Use Submitted results to read summaries, safe branch/commit and file
   references, and current review outcomes for the selected board.

The helper passes the door directly to `pursers-wait-bridge join`, which owns
the private mode-0600 credential store. The extension does not log or persist
the door. The page completes environment-free stdio registration through
AionUi's authenticated same-origin `POST /api/mcp/servers/import` endpoint; the
helper never receives AionUi session credentials.

## Backend onboarding contract

`door/adapter.cjs` provides typed `parse`, `validate`, `connect`, `status`,
`rotate`, and `recover` operations. The helper serves the
`/pursers/onboarding/` family plus compatible `/pursers/join` and
`/pursers/status` response shapes. Every request is bound to the exact origin,
token, selected board, and isolated wait-bridge state configured at startup.

Remote Central doors require HTTPS. HTTP doors are accepted only for loopback.
The adapter forwards a unique seat name and `PURSERS_TIER_MAX` to the shipped
wait bridge, returns an exact seat fragment for the Team adapter, and never
dispatches work. A partial MCP import is recoverable without replaying or
returning the door. See `door/DOOR_ONBOARDING_CONTRACT.md` for result codes,
idempotency, and current host limitations.

## Standalone lifecycle and results

Final acceptance groups seats by Pursers board and uses standalone seat
processes; it does not depend on native Aion Team Mode. Four user-facing
capabilities remain separate next-train feature dependencies:

- `TK-6db356ba00b8`: persisted standalone worker groups.
- `TK-08269cf76117`: safe standalone seat join/status/disconnect.
- `TK-fc9727be2848`: board-backed ticket lifecycle and bounded actions.
- `TK-12187737e73f`: submitted artifacts and independent review outcomes.

Existing Fleet, seat CLI, and Personal APIs may be reused by those features,
but their presence alone is not implementation proof. The acceptance harness
continues to report the four capabilities unavailable until approved feature
interfaces and tests are assembled. The packaged legacy `/pursers/team/`
adapter is compatibility-only and does not count as standalone lifecycle
proof. Source discovery never substitutes for live authenticated browser
evidence.

## Ticket lifecycle

The helper keeps one persistent `pursers-wait-bridge ticket-lifecycle` sidecar
for its configured board. The sidecar reads the private door store and joins as
a dedicated actor with `can_work=false` and `can_review=false`; credentials and
the Central URL never enter the browser. Use a wait-bridge build containing
`ticket_lifecycle.py` with this extension candidate.

Home supports list, get, generated-ID create, and cancel. Create always sets
`unassigned=true`. Cancel is not a UI authorization shortcut: Central permits it
only for the creator principal, current executor, or an authorized reviewer.
There are deliberately no Home routes for claim, renew, submit, review-claim,
review, assignment, or client-authored status changes. See
`ticket_lifecycle/FEATURE_CONTRACT.md` for the role and recovery contract.

`GET /pursers/results` is read-only and accepts only optional `ticket_id` and
`state` filters. It fetches the selected Central-and-board pair from the loopback Fleet dashboard,
removes submission and review notes, bounds rows and file references, and reports
`missing`, `pending`, `approved`, `rejected`, or `failed` without inferring
omitted data.

## AionUi 2.2.1 limitations

- AionCore 0.2.1 loads the Home settings tab, static assets, and permission set,
  but does not execute JavaScript route handlers. Home uses the explicit helper
  rather than leaving the installed page permanently disconnected.
- AionUi lists extension-declared MCP servers but does not inject them into
  Codex or Claude conversations. This extension deliberately has no
  `contributes.mcpServers` block and uses the authenticated REST import path.
- Extension assistant presets are contributed, but AionUi 2.2.1 may not expose
  them in the conversation preset picker. If a preset is not selectable, create
  the conversation explicitly and apply the matching context file.
- AionUi does not render Pursers MCP elicitation forms. Use the dashboard or the
  coordinator-provided fallback for human-input requests.
- The settings iframe has no native Team conversation credentials. Final
  lifecycle acceptance therefore depends on the board-managed standalone
  feature tickets above. Helper and host integration tests still cover
  selected-board status, door validation, origin/token/board failures, package
  discovery, and fail-closed legacy Team fallback.
