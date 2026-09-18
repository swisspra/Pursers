# Fleet Dashboard and Board Butler

This guide is for an operator starting with a fresh `pip install pursers` and
`pursers-central init`. It explains how to run the repository-owned Fleet
Dashboard, use every page, configure seats without exposing credentials, and
operate Board Butler. Replace every `/PATH/TO/...` value with an operator-owned
path. Never paste a bearer token into a command, browser form, ticket, or log.

The dashboard and Butler are source-invoked operator tools, not installed
console commands. Keep a dedicated, clean Pursers clone for them. The Central
and Client packages can still be installed from PyPI for normal use; the
editable installs below deliberately keep these two services aligned with the
selected dashboard checkout.

## Install and start

Choose unused loopback ports. This guide uses `18767` for Central and `18899`
for the dashboard; it intentionally does not use the repository defaults.

```bash
python3 -m pip install pursers
pursers-central init /PATH/TO/private/central --port 18767
/PATH/TO/private/central/run-central.sh
```

Keep the generated admin token file private. In another terminal, from the
dedicated Pursers clone:

```bash
uv venv /PATH/TO/private/fleet-dashboard-venv
uv pip install \
  --python /PATH/TO/private/fleet-dashboard-venv/bin/python \
  -e /PATH/TO/Pursers/packages/client \
  -e /PATH/TO/Pursers/packages/central

export PURSERS_FLEET_PYTHON=/PATH/TO/private/fleet-dashboard-venv/bin/python
export PURSERS_FLEET_REPO=/PATH/TO/Pursers
export PURSERS_FLEET_RUNTIME_DIR=/PATH/TO/private/fleet-dashboard-runtime
export PURSERS_FLEET_STATE_DIR=/PATH/TO/private/fleet-dashboard-state
export PURSERS_FLEET_URL=http://127.0.0.1:18767/mcp
export PURSERS_FLEET_TOKEN_PATH=/PATH/TO/private/central/admin.jwt
export PURSERS_FLEET_HOME_BOARD=example-board
export PURSERS_FLEET_PORT=18899
/PATH/TO/Pursers/tools/fleet-dashboard/launch.sh
```

Open `http://127.0.0.1:18899`. The launcher binds only to loopback, puts its
temporary files and bytecode outside the clone, and refuses runtime or state
directories inside the clone. Prefer `PURSERS_FLEET_TOKEN_PATH`; the dashboard
uses the token file but does not return the token to the browser or log it.

To enable the **Doors** panel, add these paths before starting the dashboard,
and point Central at the same public JWKS file:

```bash
export PURSERS_FLEET_DOORS_KEYS_DIR=/PATH/TO/private/door-keys
export PURSERS_FLEET_JWKS_PATH=/PATH/TO/private/jwks.json
export CENTRAL_JWKS_PATH=/PATH/TO/private/jwks.json
```

The key directory is private; the JWKS contains public verification keys.

### macOS LaunchAgent example

Use the checked-in template rather than inventing a second service command:

```bash
cp /PATH/TO/Pursers/tools/fleet-dashboard/com.pursers.fleet-dashboard.plist.template \
  /PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
```

In the private copy, replace every `/PATH/TO/...` placeholder and set the
single-Central URL, admin token-file path, home board, and the optional port and
Door paths. Keep the runtime, state, token, and log locations outside the clone.
Then validate and load it:

```bash
plutil -lint /PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
chmod 600 /PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
launchctl bootstrap "gui/$(id -u)" \
  /PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
launchctl print "gui/$(id -u)/com.pursers.fleet-dashboard"
```

The template's `ProgramArguments` call `tools/fleet-dashboard/launch.sh`; its
environment names the interpreter, clone, external runtime/state paths,
Central URL, token file, and home board. For multiple Centrals, use a private
`PURSERS_FLEET_CENTRALS` JSON file and remove the single-Central URL, token, and
home-board entries.

**Not verified on this release:** the template passed `plutil -lint`, but this
guide's throwaway run did not bootstrap a persistent LaunchAgent. Loading a
user service would have modified operator launchd state outside the throwaway.

### Upgrade and verify the deployed revision

Upgrade only a clean, dedicated clone to a reviewed 40-character commit that
is in `origin/main`:

```bash
PURSERS_FLEET_REPO=/PATH/TO/Pursers \
PURSERS_FLEET_STATE_DIR=/PATH/TO/private/fleet-dashboard-state \
PURSERS_FLEET_PYTHON=/PATH/TO/private/fleet-dashboard-venv/bin/python \
  /PATH/TO/Pursers/tools/fleet-dashboard/upgrade.sh \
  0123456789abcdef0123456789abcdef01234567

curl --fail --silent http://127.0.0.1:18899/api/version
git -C /PATH/TO/Pursers ls-remote origin refs/heads/main
```

`upgrade.sh` fetches `origin/main`, rejects a dirty clone or unrelated SHA,
reinstalls changed package manifests, probes imports, and restarts the loaded
LaunchAgent. It restores the previous checkout if setup or restart fails. The
version endpoint returns `running_sha` and `dirty`; the page header shows the
first 12 characters. The prior full SHA is saved under the external state
directory and can be supplied to the same command for rollback while it remains
in `origin/main` history.

**Not verified on this release:** the throwaway dashboard was started directly
through `launch.sh`; `upgrade.sh` was not executed because it requires and
restarts an already loaded persistent LaunchAgent.

## Read the dashboard

The global search filters visible data. The theme, density, and keyboard-help
controls are local browser preferences. A synthetic four-ticket board looks
like this:

![Fleet overview with health, waiting requests, and attention](img/fleet-home.png)

### Home

Home is the triage surface across all configured Centrals. Health cards show
online, busy, ready, and stale seats plus ticket totals. **Waiting for you**
contains unresolved human requests and held Butler drafts. **Needs attention**
surfaces findings, old open work, lapsed leases, failed dispatch, context
pressure, and unavailable push subscriptions.

For a human request, complete only the requested safe fields, choose a
disposition, then select **Accept** or **Decline**:

- **reopen** resumes the ticket with the answer;
- **park** records the answer while leaving the ticket waiting;
- **cancel** ends the ticket.

A request involving credentials is not rendered as a browser form. Trusted
external hand-offs open only after an explicit click. For an attention row,
**Acknowledge** hides that exact fingerprint until it changes; **Snooze 24h**
hides it for one day. These controls change the dashboard's private attention
state, not the ticket lifecycle.

### Projects

Projects lists the active registry boards and their visible ticket counts.
**Workspace** opens the board. Shortcut buttons open **Ticket Flow**,
**Timeline**, **Changes**, and **Routes**. A bounded or truncated snapshot is
labelled; missing rows are never inferred.

### Work

Work groups current work into waiting/open, working, review, and recently done
states. Use **Details** for the ticket record or **Flow** for its lane. Rejected
submissions are called out instead of being blended into ordinary review work.

![Work grouped by ticket state](img/fleet-work.png)

### Team

Team shows the shared pool by role and live state, including active tickets,
lease timing, capabilities, models, and clients. Filters narrow by role, state,
board, or client. Operator controls can test, start, stop, or restart a
configured seat and open its bounded logs. **New agent** and **Copy seat
command** lead into the configuration workflow; retired and stale seats remain
visibly distinct from available ones.

### Approvals

Approvals collects human requests, submitted tickets waiting for review, and
intake decisions. Open the linked ticket or board before acting. Human-request
resolution is performed on Home; intake accept/decline is performed in the
board workspace. Review submission remains a reviewer action, not a dashboard
shortcut.

### Activity

Activity is a bounded recent-change feed across boards. Ticket, Timeline, and
Changes links preserve the relevant board context. Counts describe only the
returned event window; use the board's sequence filter when investigating an
exact transition.

### Settings

Settings contains service and operator controls:

- **Board Butler** shows configuration, running mode, last activity, and the
  immediate stop control.
- **Connections** shows Central and dashboard connectivity.
- **Team seats and dispatch** links to Config.
- **Coordinator**, **workers**, and **overhead** expose bounded operational
  state and thresholds.
- **Release operations** display version and release evidence and require an
  explicit server-generated confirmation plan before a guarded action.

![Settings service and operations cards](img/fleet-settings.png)

Release, stage, kickstart, and restart controls are operator actions. A worker
must not use them unless a ticket explicitly authorizes that operation.

### Config

Config is the write-oriented setup page. It inventories seats, bridge versions,
Doctor results, registry coverage, dispatch policy, projects, release state,
and Door credentials. Plan/apply operations are loopback and same-origin only,
use atomic writes and timestamped backups, and append scrubbed audit records.

![Config seat wizard and Doctor controls](img/fleet-config.png)

## Work inside a board

Open a project, then use its five tabs:

- **Tickets** shows bounded ticket records, details, required submission fields,
  current findings, review state, and ticket activity.
- **Timeline** groups returned journal events by day and ticket.
- **Changes** summarizes created, claimed, submitted, closed, and rejected
  events in the last 24 hours or after an entered sequence number.
- **Ticket Flow** places tickets into Open, Working, Review, and Done lanes.
- **Routes** traces who created, executed, submitted, and reviewed each ticket,
  with rework counts and per-seat load for the returned window.

![Board workspace with tabs and bounded ticket records](img/fleet-board.png)

The board header warns when a snapshot is truncated. Timeline, Changes, Flow,
and Routes do not extrapolate beyond the displayed bounds.

## Common operator tasks

### Add a project

1. Configure the dashboard Door key directory and public JWKS path, and give
   Central the same JWKS path.
2. In Config, enter a unique project name, board ID, absolute operator checkout,
   and integration ref.
3. Select **Add project** and inspect each ordered result: registry entry, board,
   worker/reviewer Door principals, policy defaults, and clean fleet clone.
4. Copy each returned `prs1.…` Door string once to its intended role. The
   dashboard does not persist or list the string.
5. On the seat, run `pursers-door join <door-string>`.

Re-running the same project is idempotent. The endpoint requires the dashboard
principal to be a board administrator and does not touch key material until
that authorization is proven.

### Add or update a seat

1. In Config, choose the host and role, then enter the seat identity,
   capabilities, model/provider, Central URL, token-file path, optional CA path,
   bridge command, and host config path. Token contents never enter the page.
2. Select **Preview exact changes**. Read the redacted diff and confirm the
   role-derived capabilities: workers work, reviewers review, and coordinator
   or orchestrator seats do neither by default.
3. Select **Confirm and apply** only when the diff is correct. The adapter backs
   up existing files before atomic replacement.
4. If the result says **NEEDS RESTART**, restart that host, then run **Doctor**.

Use **Import discovered seats** only after reviewing host/seat/connector/role
mappings and conflicts. Import adds conflict-free inventory rows and runs
Doctor; it does not rewrite discovered host configs.

Host configuration layouts evolve. The wizard owns the generated managed
blocks; do not copy a stale JSON/TOML example from another host. These official
host references were checked on 2026-09-19:

- [Codex MCP configuration](https://developers.openai.com/en-US/docs/extend/mcp)
- [Claude Code MCP](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Claude Desktop MCP](https://docs.anthropic.com/en/docs/mcp)
- [Cursor MCP](https://docs.cursor.com/context/model-context-protocol)
- [Goose extensions](https://block.github.io/goose/)
- [Zed MCP](https://zed.dev/docs/ai/mcp)

### Run Doctor and upgrade the bridge

Use **Doctor** on one row or **Doctor all** after configuration or a host
restart. It checks managed config drift, host timeout, token/CA paths, bridge
version, identity fingerprint, clean-clone freshness, registry visibility, and
a five-second push subscription. `poll` is an explicit warning/fallback, not a
healthy push result. **Fix** applies supported repairs; review its output.

**Install / upgrade bridge** updates the persistent bridge tool. **Upgrade all
seats** updates managed seat configurations; **Upgrade bridge** operates from a
single row. Jobs expose an ID and update once per second. Restart a host when
the completed result requests it, then rerun Doctor. Goose rows also provide
**Regenerate Goose** for their managed configuration.

For headless diagnosis from the clone:

```bash
python3 tools/fleet-dashboard/seat_config.py doctor --json
python3 tools/fleet-dashboard/seat_config.py doctor --fix --json
```

Doctor redacts token contents. Its configuration references token files and a
fingerprint; it never requires a raw bearer token in host configuration.

### Issue, rotate, and revoke Doors

In Config, **Copy door string** issues or fetches a role Door and returns it once
over loopback with `Cache-Control: no-store`. **Rotate** creates a new signing
key version, publishes its public key, revokes the previous key ID, and returns
the replacement Door string. Seats using the prior string must join again.

For an explicit headless revocation, use only the visible non-secret `kid` from
the inventory:

```bash
python3 tools/wait-bridge/door_admin.py revoke-kid \
  door-example-worker-v2 --jwks /PATH/TO/private/jwks.json
```

Revocation invalidates all Door credentials signed by that key version. It does
not reveal or delete bearer tokens already issued through another principal.

## Refresh, cache, and connection behavior

Fleet and the open board detail refresh every five seconds. The server also
caches its Central snapshot for up to five seconds, so two quick reads may show
the same generation. Long Config jobs have their own one-second progress poll.

When a refresh fails, the last successful snapshot remains visible and a
connection banner reports the affected source, last success, error class, and
recovery hint. Treat the banner as stale-data disclosure, not proof that the
displayed state is current. The browser uses no-store requests; pressing reload
does not turn an old server snapshot into new Central evidence.

## Board Butler

Board Butler is one resident coordinator seat. For every active registry board
it refreshes the real coordinator derivation and drafts evidence-backed answers
to mechanically checkable questions. It is not a general autonomous
coordinator, worker, reviewer, release bot, or source of invented evidence.

It always escalates scope changes, gate waivers, releases, membership changes,
registry changes, and unknown or incomplete evidence. It never answers a
coordinator question directly; questions remain human-reviewed drafts.

### Shadow mode

Shadow is the launcher and LaunchAgent default. It refreshes findings, produces
drafts and durable holds, and performs no ticket action. Validate one cycle
before installing the service:

```bash
PYTHONPATH=/PATH/TO/Pursers/packages/client/src \
python3 /PATH/TO/Pursers/tools/board-butler/board_butler.py \
  --url http://127.0.0.1:18767/mcp \
  --token-path /PATH/TO/private/board-butler.jwt \
  --home-board example-board \
  --agent-name board-butler-local \
  --repo /PATH/TO/Pursers \
  --pid-file /PATH/TO/private/board-butler-state/board-butler.pid \
  --cursor-file /PATH/TO/private/board-butler-state/cursor.json \
  --refresh-seconds 60 --once --dry-run
```

Give Butler a distinct coordinator credential and private state path; never
reuse a worker or reviewer token. Install its checked-in LaunchAgent template
the same way as the dashboard template, leave
`PURSERS_BUTLER_RUNTIME_MODE=shadow`, validate with `plutil -lint`, and bootstrap
it with `launchctl`. The repository does not install or start it automatically.

### Active mode and its limits

Active mode adds exactly two mechanical ticket actions, and only for boards
explicitly named by `--act-on-board`:

1. park an open ticket after repeated `no_live_candidates` cycles when no live
   seat advertises `can_work=true`;
2. record refusal of a proposed escalation target whose identity cannot work.

Create the separate, owner-only authorization file first:

```bash
python3 /PATH/TO/Pursers/tools/board-butler/authorize_active.py \
  --output /PATH/TO/private/board-butler-state/active-authorized.json \
  --confirm ENABLE-BOARD-BUTLER-ACTIVE
chmod 600 /PATH/TO/private/board-butler-state/active-authorized.json
```

Then stage and review a plist with active mode, its authorization path, and at
least one explicit acting board before reloading the service. Changing only the
mode string cannot activate Butler. Repeat `--active-action` to narrow the two
allowed classes; it cannot add new autonomous classes. Each proposed action is
written to `coordinator_findings`, shown under **Waiting for you**, and held for
the configured interval so an operator can veto it.

### Stop, veto, resume, and inspect

**Settings → Stop butler now** writes a private `KILLED` marker before sending
`SIGTERM`. The LaunchAgent then exits cleanly and stays stopped. A Central write
already accepted remains atomic; an interrupted question is replayed because
its cursor was not advanced.

To veto one held action, run the documented `board_butler.py` command with all
normal connection arguments plus:

```bash
--veto-question CQ-example --control-reason "evidence is stale"
```

To resume safely, return the staged configuration to shadow mode unless active
operation is deliberately re-authorized, then:

```bash
rm /PATH/TO/private/board-butler-state/KILLED
launchctl kickstart "gui/$(id -u)/com.pursers.board-butler"
```

Decisions, findings, holds, vetoes, and human quality marks are durable Central
board state and appear in Fleet. The local private `runtime.json` records only
PID, mode, start time, and last activity; `cursor.json` preserves the positive
journal cursor. Process output goes to the private LaunchAgent log paths.
Settings reports **Not configured**, **Configured · not running**, **Running ·
shadow**, or **Running · active** only after it verifies the singleton pidfile
and live process. A stale runtime file is not treated as a running Butler.

**Not verified on this release:** the Butler plist passed `plutil -lint`, but
the throwaway did not install a resident Butler, authorize active mode, execute
an autonomous action, or engage its kill switch. Those steps intentionally
change persistent service or board state and require an operator-owned Butler
credential.

## Safe troubleshooting

- If startup fails, verify that Central `/healthz` succeeds, the dashboard token
  file contains exactly one bearer token, and all service paths are absolute.
- If data is stale, read the connection banner before restarting anything.
  Confirm Central independently, then wait for the next five-second refresh.
- If Doctor reports split identity, regenerate that seat from the same private
  token file, restart its host, and rerun Doctor.
- If `upgrade.sh` refuses, keep the safety condition: clean the dedicated clone
  deliberately or choose a different clean clone; do not bypass the check.
- If a Door action is unavailable, configure both dashboard key/JWKS paths and
  Central's public JWKS path, then restart the respective services.
- Keep the dashboard loopback-only. If an operator adds a reverse proxy, the
  proxy and access policy remain operator-owned and must preserve the stable
  loopback port and same-origin protections.
