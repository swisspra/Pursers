# GitHub Beta v5.0.0b1 demo script

This is a 3 minute 35 second product demo for the first GitHub beta. It shows
one durable board being used by Claude Desktop, Codex, an independent reviewer,
Pursers for AionUi, and Fleet Dashboard. Keep the delivery factual: the board
coordinates the hosts; it does not replace them.

## Recording rules

- Record only disposable local data. Use the names `demo-coordinator`,
  `demo-claude`, `demo-codex`, and `demo-reviewer`.
- Keep every terminal at the repository root and replace any local path in the
  frame with `/PATH/TO/DEMO` before recording.
- Never show a token, door string, personal path, email address, Git credential,
  environment dump, or shell history. Token fields stay masked or off-screen.
- Use light theme at 1440 × 900. Crop browser chrome only when it does not hide
  the loopback origin.
- A reviewer must have a different `principal_id` from the submitting worker.
  Do not describe a different display name as independent review.
- Pursers Home is read-only for claim, submit, and review. Those actions belong
  in the Claude Desktop, Codex, or reviewer host. AionUi 2.2.1 also does not
  inject extension MCP servers into Claude or Codex conversations.

## One-time staging from a fresh checkout

Use a Python 3.11+ environment containing the approved v5.0.0b1 wheel set and
its dependencies. The source checkout supplies the tracked UI and exact beta
behavior. These commands use new loopback ports and disposable data:

```sh
DEMO_ROOT=$(mktemp -d /tmp/pursers-beta-demo.XXXXXX)
chmod 700 "$DEMO_ROOT"
git clone https://github.com/swisspra/Pursers.git "$DEMO_ROOT/repo"
cd "$DEMO_ROOT/repo"
git checkout 28f81308d1cf3d40c4ed38091cc02d9c7d0827aa
test "$(git rev-parse HEAD)" = 28f81308d1cf3d40c4ed38091cc02d9c7d0827aa

mkdir -p "$DEMO_ROOT/project" "$DEMO_ROOT/profiles" "$DEMO_ROOT/central"
chmod 700 "$DEMO_ROOT/profiles" "$DEMO_ROOT/central"
export CENTRAL_PORT=29641 FLEET_PORT=29642 REHEARSAL_PORT=29643

PYTHONPATH=packages/client/src python3 -m pursers_client.personal_profile \
  --profiles-root "$DEMO_ROOT/profiles" init \
  --project "$DEMO_ROOT/project" --port "$CENTRAL_PORT" \
  > "$DEMO_ROOT/profile-summary.json" 2> "$DEMO_ROOT/profile-init.log"

PROFILE=$(find "$DEMO_ROOT/profiles" -name profile.json -type f -print -quit)
export PROFILE DEMO_ROOT
eval "$(python3 - "$PROFILE" <<'PY'
import json, shlex, sys
from pathlib import Path
p = Path(sys.argv[1])
d = json.loads(p.read_text())
f = d["files"]
values = {
    "CENTRAL_JWT_ISSUER": d["issuer"],
    "CENTRAL_JWT_AUDIENCE": d["audience"],
    "CENTRAL_JWKS_PATH": str(p.parent / f["jwks"]),
    "DEMO_OWNER_TOKEN_FILE": str(p.parent / f["token"]),
    "DEMO_BOARD": d["board_id"],
}
for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"
```

The profile command prints only a safe summary. The `eval` block exports paths
and public configuration, never the credential value. Before the take, have the
operator use the normal admission flow to provision two worker credentials and
one reviewer credential. Store them as mode-0600 files under the disposable
root. Do not mint or copy credentials on camera.

Start Central in terminal 1:

```sh
export CENTRAL_AUTH_MODE=jwt CENTRAL_ADMISSION=invite STORE_BACKEND=sqlite
PYTHONPATH=packages/client/src:packages/central/src \
  python3 -c 'from pursers_central.pursers_central_runtime import main; main()' \
  --host 127.0.0.1 --port "$CENTRAL_PORT" --data-dir "$DEMO_ROOT/central"
```

Check readiness in terminal 2 without displaying any credential:

```sh
curl --fail --silent --show-error \
  "http://127.0.0.1:$CENTRAL_PORT/healthz" | python3 -m json.tool
```

Still in terminal 2, create three short-lived recording principals without
printing their credentials. This staging helper uses the profile's disposable
signing key, creates the board under the coordinator, admits the worker and
reviewer, and writes only a redacted identity summary to stdout:

```sh
PYTHONPATH=packages/client/src python3 - "$PROFILE" "$DEMO_ROOT" <<'PY'
from __future__ import annotations
import asyncio, hashlib, json, os, sys, time
from pathlib import Path
import jwt
from cryptography.hazmat.primitives import serialization
from pursers_client import BoardClient

profile_path, root = Path(sys.argv[1]), Path(sys.argv[2])
profile = json.loads(profile_path.read_text())
private_key = serialization.load_pem_private_key(
    (profile_path.parent / profile["files"]["private_key"]).read_bytes(),
    password=None,
)

def credential(label: str, scope: str) -> tuple[Path, str]:
    client_id, subject = f"demo-{label}", f"demo-{label}-principal"
    now = int(time.time())
    claims = {
        "iss": profile["issuer"], "sub": subject,
        "aud": profile["audience"], "resource": profile["audience"],
        "scope": scope, "client_id": client_id,
        "iat": now, "nbf": now - 5, "exp": now + 7200,
    }
    value = jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": profile["kid"]}
    )
    path = root / f"{label}.jwt"
    path.write_text(value)
    os.chmod(path, 0o600)
    canonical = json.dumps(
        [client_id, profile["issuer"], subject], separators=(",", ":")
    )
    principal = "PR-" + hashlib.sha256(canonical.encode()).hexdigest()
    return path, principal

coordinator_file, coordinator_principal = credential(
    "coordinator", "board:read board:write board:review board:coordinate"
)
worker_file, worker_principal = credential(
    "codex", "board:read board:write"
)
reviewer_file, reviewer_principal = credential(
    "reviewer", "board:read board:review"
)

async def stage() -> None:
    capabilities = {
        "can_work": False, "can_review": False, "tier_max": 2,
        "max_parallel": 1,
    }
    async with BoardClient(
        profile["audience"], coordinator_file.read_text(), profile["board_id"],
        agent_name="demo-coordinator", role="worker",
        capabilities=capabilities, allow_takeover=True,
    ) as coordinator:
        for principal, role in (
            (worker_principal, "member"),
            (reviewer_principal, "reviewer"),
        ):
            await coordinator._call("board_member_add", {
                "agent_name": "demo-coordinator",
                "principal_id": principal,
                "role": role,
            })
        registry = json.dumps({
            "schema_version": 1,
            "projects": {"demo": {
                "board_id": profile["board_id"], "status": "active",
                "fleet": True,
                "repository_url": "https://example.invalid/pursers-demo",
                "work_dir": "/PATH/TO/DEMO",
            }},
        }, separators=(",", ":"))
        await coordinator.board_state_update("project_registry", registry)

asyncio.run(stage())
print(json.dumps({
    "board_id": profile["board_id"],
    "coordinator_principal_id": coordinator_principal,
    "worker_principal_id": worker_principal,
    "reviewer_principal_id": reviewer_principal,
    "credential_files": [str(coordinator_file), str(worker_file), str(reviewer_file)],
}, indent=2))
PY
export DEMO_TOKEN_FILE="$DEMO_ROOT/coordinator.jwt"
```

This helper is for the disposable recording board only. Production admission
uses the normal operator door workflow. Start Fleet in terminal 3, using a new
reserved dashboard session name for every disposable run:

```sh
python3 tools/fleet-dashboard/fleet_dashboard.py \
  --port "$FLEET_PORT" \
  --url "http://127.0.0.1:$CENTRAL_PORT/mcp" \
  --token-file "$DEMO_TOKEN_FILE" \
  --home-board "$DEMO_BOARD" \
  --agent-name fleet-dashboard-session-recording
```

Open `http://127.0.0.1:29642`. In **Config**, use **Add or update seat** for
Claude Desktop and Codex. Select `worker`, give each a unique name, select the
same home board, and enter only a token *file path*. Choose **Preview exact
changes**, inspect **Confirm changes**, then choose **Confirm and apply**. Fully
restart each host and run **Doctor all**. The checked-in UI states that token
contents never enter this page.

Build the exact extension candidate before installing it through the managed
AionUi Hub used by the recording workstation:

```sh
python3 tools/aionui-extension/build.py
```

The output is `dist/pursers-aionui-0.1.0.zip`. Do not imply that AionUi 2.2.1
has an in-app local-ZIP importer. For layout rehearsal only, this disposable
server exposes tracked candidate UI with synthetic JSON:

```sh
python3 tools/aionui-extension/tests/home_acceptance/beta_blocking_fixture_server.py \
  --port "$REHEARSAL_PORT"
```

Open `http://127.0.0.1:29643/extension/` only for rehearsal. Never use synthetic
fixture state as final product evidence. The final AionUi shot must come from the
managed installed extension connected to the live disposable board.

## Tool-call cards

Enter these through the named host's Pursers connector. Keep returned IDs from
each response; do not type a made-up ticket ID.

1. Connect Claude Desktop as the coordinator, Codex as the worker, and the
   independent host as the reviewer. Run each call through that host's own
   credential-backed connector:

   ```text
   board_onboard({
     board_id: "<DEMO_BOARD>", agent_name: "demo-claude",
     role: "coordinator", allow_takeover: true,
     capabilities: {can_work: false, can_review: false, tier_max: 2, max_parallel: 1}
   })
   board_onboard({
     board_id: "<DEMO_BOARD>", agent_name: "demo-codex",
     role: "worker", allow_takeover: true,
     capabilities: {can_work: true, can_review: false, tier_max: 2, max_parallel: 1}
   })
   board_onboard({
     board_id: "<DEMO_BOARD>", agent_name: "demo-reviewer",
     role: "reviewer", allow_takeover: true,
     capabilities: {can_work: false, can_review: true, tier_max: 2, max_parallel: 1}
   })
   ```

2. Coordinator creates unassigned work:

   ```text
   ticket_create({
     board_id: "<DEMO_BOARD>",
     agent_name: "demo-claude",
     title: "Persist vendor-neutral run state",
     description: "Store one durable status record that both hosts can inspect.",
     target_url: "https://example.invalid/pursers-demo",
     scope: "interactive-no-send",
     required_fields: ["summary", "files_changed", "test_output"],
     related_files: ["docs/demo-state.md"],
     unassigned: true,
     tier: 2
   })
   ```

3. Worker waits for the push offer, then uses the returned `ticket_id`:

   ```text
   a2a_wait({boards: ["<DEMO_BOARD>"], only_mine: true, timeout_s: 180})
   ticket_claim({
     board_id: "<DEMO_BOARD>", agent_name: "demo-codex",
     ticket_id: "<RETURNED_TICKET_ID>"
   })
   lease_renew({
     board_id: "<DEMO_BOARD>", agent_name: "demo-codex",
     ticket_id: "<RETURNED_TICKET_ID>"
   })
   ```

4. Worker submits the completed artifact:

   ```text
   ticket_submit({
     board_id: "<DEMO_BOARD>",
     agent_name: "demo-codex",
     ticket_id: "<RETURNED_TICKET_ID>",
     summary: "Documented the shared durable state.",
     files_changed: ["docs/demo-state.md"],
     notes: "branch_and_commit: demo/state@<FULL_SHA>\ntest-command: python -m pytest -q\ntest-output: 1 passed",
     stay_active: false
   })
   ```

5. A different authenticated principal reviews it:

   ```text
   ticket_review_claim({
     board_id: "<DEMO_BOARD>", agent_name: "demo-reviewer",
     ticket_id: "<RETURNED_TICKET_ID>"
   })
   ticket_review({
     board_id: "<DEMO_BOARD>",
     agent_name: "demo-reviewer",
     ticket_id: "<RETURNED_TICKET_ID>",
     verdict: "approve",
     review_notes: "Verified from a different principal."
   })
   ```

6. Coordinator records the amended decision:

   ```text
   ticket_annotate({
     board_id: "<DEMO_BOARD>",
     agent_name: "demo-claude",
     ticket_id: "<RETURNED_TICKET_ID>",
     kind: "decision",
     text: "Decision: use one board as the durable handoff between hosts."
   })
   ```

For the review shot, open `ticket_get` and frame both
`submitted_by_principal_id` and `reviewed_by_principal_id`. Pause only after the
values are visibly different. For the decision shot, show the annotation with
`kind: decision`; the newest decision amends the earlier ticket text.

## Timed scene list

### 0:00–0:20 — The problem

**On screen:** A clean title card: “Two AI hosts. One durable handoff.” Cut to
Claude Desktop and Codex side by side, each with a different local chat history.

**Narration:** “Agent hosts are good at their own conversations, but their local
history is not a durable shared work queue. A task can disappear between tools,
and a second host cannot independently verify what happened.”

### 0:20–0:45 — Start Central

**On screen:** Run the Central command. In the second terminal, run the health
check and hold on `"status": "ok"`, `"store_backend": "sqlite"`, and the
loopback address.

**Narration:** “Pursers Central is a local MCP service backed by SQLite. This
demo uses a throwaway data directory, JWT verification, and loopback-only
ports.”

### 0:45–1:15 — Connect Claude Desktop and Codex

**On screen:** In Fleet **Config**, show the two seat rows after **Doctor all**.
Then show `board_onboard` in Claude Desktop and Codex. Frame the same board ID,
two unique agent names, and their verified principal IDs. Keep token paths and
all credential fields outside the crop.

**Narration:** “Claude Desktop and Codex connect through separate seats. Both
join the same board, and Central returns the effective identity instead of
trusting a display name.”

### 1:15–1:50 — Create, offer, claim, and renew

**On screen:** In Claude Desktop, use the `ticket_create` card. Cut to Codex as
the push wait returns the offer. Run `ticket_claim`, then `lease_renew`. Finish
on the live lease expiry rather than a fabricated status label.

**Narration:** “The coordinator creates unassigned work. Dispatch offers it to
an eligible worker. Codex claims it atomically, and the renewable lease makes
ownership explicit if a worker stops responding.”

### 1:50–2:15 — Submit evidence

**On screen:** Show the changed file and a one-test pass in the worker terminal.
Use `ticket_submit`, then show the returned `submitted` state and the exact
branch, full commit, file list, test command, and test output.

**Narration:** “Submission is a handoff, not a chat message. It carries the
exact source reference, changed files, and test evidence that the reviewer can
check.”

### 2:15–2:45 — Independent review

**On screen:** Switch to the reviewer host. Show its different principal ID,
then `ticket_review_claim` and the approving `ticket_review`. Open `ticket_get`
and frame `independent-principal-review` with the two different principal IDs.

**Narration:** “A separate reviewer principal reserves the review, checks the
submission, and approves it. Central enforces the identity boundary; changing a
seat name would not turn self-review into independent review.”

### 2:45–3:00 — Coordinator decision

**On screen:** Run `ticket_annotate` with `kind: decision`, then refresh
`ticket_get` and highlight the newest decision annotation.

**Narration:** “The coordinator records the decision on the ticket. Later
workers read the newest decision as the amendment, so the reason travels with
the work.”

### 3:00–3:20 — Pursers for AionUi

**On screen:** In the installed extension, open **Connect the Home helper**,
choose **Connect helper**, and frame **Selected board**. Move to **Submitted
results**, open the approved item, then hold on **Open the Pursers dashboard**.

**Narration:** “Pursers Home gives AionUi a board-pinned view of connection
status, ticket lifecycle, and bounded submitted results. It does not silently
claim, submit, or review work for an agent.”

### 3:20–3:35 — Fleet Dashboard

**On screen:** Open Fleet Dashboard in light theme. Show **Fleet overview**,
**Unified agent pool**, the board card, and the closed-today count. Briefly open
**Seats and dispatch** and frame **Current offers**.

**Narration:** “Fleet closes the loop: one view for boards, agents, seats,
dispatch, and recent outcomes. The durable record remains available after the
individual conversations end.”

## Screenshot-ticket shot list

Use these as the handoff to the showcase-screenshots ticket. Capture PNGs at
1440 × 900 after the live state is staged.

1. `01-central-health.png` — loopback Central and the redacted health response.
2. `02-two-hosts-one-board.png` — Claude Desktop and Codex onboard results with
   the same board ID and distinct seat names.
3. `03-offer-and-lease.png` — offered ticket, accepted claim, and live lease.
4. `04-submission-evidence.png` — submitted state, safe source reference, file
   list, and test evidence.
5. `05-independent-review.png` — approved review with visibly different worker
   and reviewer principal IDs.
6. `06-decision-annotation.png` — newest `decision` annotation on the ticket.
7. `07-aionui-home.png` — **Selected board**, **Submitted results**, and approved
   result in the installed Pursers Home.
8. `08-fleet-overview.png` — **Fleet overview**, **Unified agent pool**, and the
   board's closed-today count.

Reject any capture containing a token-shaped string, door, email, user account,
personal path, unrelated notification, or synthetic fixture presented as live.

## B-roll

- Slow pan across the ticket's event order: created, offered, claimed,
  submitted, review claimed, approved, annotated.
- Close crop of the SQLite-backed health response.
- Two-second hold on `lease_expires_at` after renewal.
- Side-by-side crop of the two principal IDs used for submit and review.
- Fleet search finding the demo ticket by ID.
- AionUi **Submitted results** changing from pending to approved after refresh.

## 30-second cut

**0:00–0:05:** Two host windows. “Claude Desktop and Codex do not share durable
chat state.”

**0:05–0:10:** Central health and one board. “Pursers gives them one local,
authenticated work board.”

**0:10–0:17:** Create → offer → claim → lease. “Work is dispatched and reserved
with an expiring lease.”

**0:17–0:23:** Submit and independent approval. “Evidence is submitted, then a
different principal reviews it.”

**0:23–0:27:** Decision annotation and AionUi approved result. “The decision
stays attached to the ticket.”

**0:27–0:30:** Fleet overview. “Fleet shows the shared state after the chats are
gone.”
