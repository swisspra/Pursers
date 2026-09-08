# Pursers AionUi Team lifecycle adapter — contract

Ticket: TK-628602eedb90 (board `pursers`, tier 2). Published per coordinator decision
AN-000000000022 ("Publish adapter contract before editing shared routes") before any
shared-file change is considered. Consumers: door onboarding sibling TK-4ba6bd1964de,
acceptance harness sibling TK-4f1f01ba50eb.

This contract is the operation/result agreement for `tools/aionui-extension/team/adapter.cjs`,
a bounded Team lifecycle adapter for the Pursers AionUi extension. It is a backend
preparation deliverable: it does not add UI, does not edit the shared manifest
(`aion-extension.json`), the shared routes (`webui/routes.js`), `build.py`, `contexts/`,
or `vendor/`, and it performs no live mutation of real user Teams in this ticket.

## 1. Verified host surfaces (summary)

Exact evidence, versions, schemas, and observed gaps: `HOST_API_EVIDENCE.md` in this
directory. Summary of the installed host:

- AionUi desktop app version `2.2.1` (`CFBundleShortVersionString`).
- Bundled backend `aioncore 0.2.1` (`manifest.json` version `v0.2.1`, source
  `github.com/iOfficeAI/AionCore` release `aioncore-v0.2.1-aarch64-apple-darwin.tar.gz`).
- Agent-facing CLI contract `agent-facing-team-cli`, `schema_version: 1`, exposed by
  `aioncore team capabilities` and by the MCP tool family `team_*` (bridge:
  `aioncore mcp-team-stdio`). Live commands require the conversation runtime environment
  `AIONUI_BASE_URL`, `AIONUI_USER_ID`, `AIONUI_CONVERSATION_ID`, `AIONUI_RUNTIME_TOKEN`;
  `capabilities`/`help` require none.
- Persistence: `~/.aionui/aionui-backend.db` tables `teams` (columns include `id`, `name`,
  `workspace`, `workspace_mode`, `agents`, `lead_agent_id`, `session_mode`,
  `agents_version`, `project_id`, `folder_id`, `archived_at`) and `team_tasks`.
- REST routes `/api/teams...` were observed inside the installed binary strings but were
  NOT exercised in this ticket (read-only discovery rule). Anything only observed there is
  reported by the adapter as `unsupported_by_host`, never as a working path.

The adapter uses only the agent-facing `team_*` surface (CLI stdin-JSON / MCP tools),
which is the documented, permission-gated API. Tool schemas are quoted verbatim in
`HOST_API_EVIDENCE.md`.

## 2. Input: TeamSpec

`validateTeamSpec(spec)` returns `{ ok: true }` or `{ ok: false, errors: string[] }`.
Every error string starts with the JSON pointer of the offending field.

```json
{
  "team": { "name": "Goose-Worker-Team" },
  "lead": { "name": "Goose", "assistant_id": "bare:600c6601", "monitor_only": true },
  "seats": [
    {
      "name": "pursers-example-goose-1",
      "assistant_id": "bare:600c6601",
      "model": "vertex_ai/gemini-3.8-flash",
      "role": "worker",
      "tier_max": 2,
      "folder": "pursers-example-goose-1",
      "kickoff_prompt": "optional override text"
    }
  ],
  "options": {
    "dry_run": true,
    "confirm": "",
    "send_kickoff": true,
    "create_tasks": false,
    "tasks": []
  }
}
```

Field rules:

- `team.name` (required, string): display name of an existing AionUi Team. The adapter
  never creates Teams: contract `agent-facing-team-cli` schema_version 1 exposes no
  team-create tool, so `apply` runs only inside the runtime conversation of an existing
  Team (the host resolves team context from `AIONUI_CONVERSATION_ID`).
- `lead` (required): must match an existing roster member with `role: "lead"`.
  `monitor_only: true` is a prompt-level invariant (section 5), not a host setting.
- `seats[]` (required, may be empty): each `name` is the unique stable per-teammate
  identity inside the Team (matching key, section 4). `assistant_id` must exist in the
  catalog returned by `team_list_assistants`. `model` is optional; when present it is
  compared against the roster member's `model` but is NOT settable through the
  agent-facing contract (rename/model routes are REST-only, unverified). `role` must be
  `"worker"` or `"reviewer"` (Pursers role; carried in the kickoff prompt, not a host
  field). `tier_max` must be 1, 2, or 3 (Pursers capability tier; carried in the kickoff
  prompt, not a host field). `folder` is the seat directory relative to the Team
  workspace (one folder per seat, section 5). `kickoff_prompt` optionally overrides the
  generated kickoff text.
- `options.dry_run` defaults to `true`. A mutating run requires
  `options.confirm === "apply-live"` AND `options.dry_run !== true`. This encodes the
  ticket scope `interactive-no-send`: no live Team mutation from this ticket's execution.
- `options.create_tasks` with `options.tasks[]` of `{ subject, description?, owner_seat?,
  blocked_by? }` maps to `team_task_create`; board-only dispatch (section 5) means real
  Pursers work is dispatched through board tickets, so this defaults to `false`.

## 3. Operations and result contract

Every operation resolves to a plain JSON object; no exceptions for host-level errors.

```json
{
  "op": "apply",
  "team": "Goose-Worker-Team",
  "dry_run": true,
  "results": [
    {
      "seat": "pursers-example-goose-1",
      "slot_id": "01a07f7f-0000-0000-0000-000000000000",
      "action": "spawn",
      "outcome": "dry_run",
      "detail": "would call team_spawn_agent {name, assistant_id}"
    }
  ],
  "summary": {
    "ok": 0, "dry_run": 1, "skipped_exists": 0, "identity_conflict": 0,
    "failed": 0, "unsupported_by_host": 0
  }
}
```

Per-teammate `outcome` enum (exact strings):

- `ok` — operation completed for this seat.
- `dry_run` — planned action, not executed (default mode).
- `skipped_exists` — seat already on the roster with matching identity; nothing sent.
- `identity_conflict` — a roster member carries the seat `name` but a different
  `assistant_id` (or a declared `model` mismatch). No mutation is performed; another
  active identity is never stolen, renamed, or shut down automatically.
- `unsupported_by_host` — the requested operation does not exist in the installed
  agent-facing contract (section 6); `detail` names the observed-only REST alternative
  when one exists.
- `failed` — host or validation error; `detail` carries the envelope `error.code` from
  the contract enum (`unknown_tool`, `schema_validation_failed`, `permission_denied`,
  `team_not_found`, `conversation_not_found`, `agent_not_found`, `not_in_team`,
  `transport_unavailable`, `runtime_context_missing`, `runtime_auth_failed`) or the
  validation message.

Operations:

| Adapter call | Host tool(s) | Permission | Notes |
|---|---|---|---|
| `status()` | `team_members {}`, optionally `team_task_list {}` | any_team_agent | roster `{slot_id, name, role, status, assistant_id, model}` (shape live-verified read-only) |
| `plan(spec)` | `team_members`, `team_list_assistants` | any_team_agent | diff only; never mutates |
| `apply(spec)` | `team_spawn_agent {name, assistant_id}`; then `team_send_message {to, message}` kickoff; optional `team_task_create` | spawn is lead_only | idempotent (section 4); default dry-run |
| `pauseSeat(slot_id, message, reason?)` | `team_interrupt_agent {slot_id, message, files?, reason?}` | lead_only | "pause" = interrupt the active turn and deliver replacement instruction; run-level pause is REST-only (unverified) |
| `stopSeat(slot_id, reason?)` | `team_shutdown_agent {slot_id, reason?}` | lead_only | cooperative: the teammate may answer `shutdown_rejected: <reason>`; result reports the request, not a guaranteed stop |
| `renameSeat(slot_id, new_name)` | `team_rename_agent {slot_id, new_name}` | lead_only | explicit operator action only; never used to resolve `identity_conflict` |
| `resetSeatContext(slot_id)` | `team_clear_agent_context {slot_id}` | lead_only | |
| `removeSeat(slot_id)` | — | — | always `unsupported_by_host` (section 6) |
| `createTeam(...)`, `archiveTeam(...)` | — | — | always `unsupported_by_host` (section 6) |
| `listAssistants()` / `describeAssistant(assistant_id)` | `team_list_assistants {}` / `team_describe_assistant {assistant_id, locale?}` | any_team_agent | catalog shapes live-verified read-only |

`apply` executes seats sequentially and never aborts the whole run on a single-seat
failure: each seat gets its own result, so a partial failure is visible and resumable.

## 4. Identity and idempotency guarantees

- Stable identity: the seat display `name` inside the Team is the matching key; the host
  assigns `slot_id` (UUIDv7 string) at spawn. The adapter is stateless: every run
  re-reads the roster via `team_members`, so retry/restart cannot duplicate teammates.
- Reconcile rule per seat: name absent → `spawn`. Name present with equal `assistant_id`
  (and equal `model` when the spec declares one) → `skipped_exists` (existing `slot_id`
  is reused; kickoff is NOT resent unless `options.send_kickoff === "always"`). Name
  present with different `assistant_id`/`model` → `identity_conflict`, zero mutations.
- Kickoff messages are sent only for seats spawned in the same run, or for
  `skipped_exists` seats when the operator explicitly passes
  `options.send_kickoff === "always"`. Restarting `apply` after a crash between spawn and
  kickoff therefore converges (spawn is skipped, kickoff is delivered when requested)
  without duplicating the teammate.
- The response `data` shape of `team_spawn_agent` was not exercised live in this ticket
  (mutation forbidden). The adapter extracts the new slot defensively in the order
  `data.slot_id`, `data.agent.slot_id`, then the newest roster member by name via a
  follow-up `team_members` call, and reports `failed` with detail
  `spawn_ack_unparsed` if none yields a slot. Live acceptance of this path belongs to
  TK-4f1f01ba50eb.

## 5. Pursers invariants carried by the adapter

- One folder per seat: `<team workspace>/<seat folder>`. The host stores only a
  team-level `workspace` (`workspace_mode: "shared"` observed); there is no per-agent
  workspace column or spawn parameter in the installed version, so the per-seat folder
  is created by the operator/kickoff convention and stated verbatim in the generated
  kickoff prompt (`buildSeatKickoff`).
- Monitor-only lead and board-only task dispatch: enforced as prompt text in the
  generated kickoff (lead: observe and coordinate, do not perform ticket work; seats:
  accept work only through Pursers board tickets via the seat kit `bin/board.sh`). The
  host does not enforce these; the contract reports them as prompt-level invariants.
- Role and tier preservation: Pursers `role` (worker/reviewer) and `tier_max` (1/2/3)
  are embedded in the kickoff prompt exactly as given in the spec; the adapter never
  rewrites them, and no host field exists for them beyond `role: lead|teammate`.
- No auto elastic scaling (T8 deferred): `apply` materializes exactly the seats in the
  spec. There is no code path that spawns teammates from load, queue depth, or board
  state.

## 6. Honest gaps in aioncore v0.2.1 (agent-facing contract)

Reported as `unsupported_by_host`, never fabricated:

1. No team create/archive/remove tool. Teams are created in the AionUi GUI/REST layer
   (`/api/teams` observed in binary strings only).
2. No per-seat workspace or initial-prompt parameter on `team_spawn_agent` (schema is
   exactly `{name, assistant_id}`, `additionalProperties: false`).
3. No teammate removal tool; observed-only REST route
   `/api/teams/{id}/agents/{slot_id}` and `teams.archived_at` persistence for Teams.
4. Run-level pause/cancel (`/api/teams/{id}/runs/{team_run_id}/...`) is REST-only and
   unverified; the agent-facing equivalent for stopping an in-flight turn is
   `team_interrupt_agent`.
5. Model switching per seat is REST-only (`/api/teams/{id}/agents/{slot_id}/model`,
   observed in binary strings, unverified); the adapter only compares models.

If a future host version adds these, the adapter surfaces light up by extending the
operation table; the result contract and outcome enum stay unchanged.

## 7. Transport contract

- CLI transport (default): `execFile(helperBin, ["team", ...command], { timeout: 30000 })`
  with the JSON request on stdin and the stdout envelope
  `{ "success": bool, "data": object, "error": object, "meta": { "schema_version": 1 } }`.
  `helperBin` resolves from `AIONUI_HELPER_BIN`, else `aioncore` on `PATH`. No shell
  interpolation anywhere; seat names and prompts travel as JSON on stdin only.
- MCP transport: the same 13 `team_*` tools with identical schemas (bridge
  `aioncore mcp-team-stdio`); the adapter's `runCli` dependency is injectable so the
  acceptance harness can drive it through either transport or a fake.
- Missing runtime env (`AIONUI_BASE_URL`, `AIONUI_USER_ID`, `AIONUI_CONVERSATION_ID`,
  `AIONUI_RUNTIME_TOKEN`) surfaces as `failed` with `runtime_context_missing`, mapped
  from the host error enum — the adapter never guesses a team context.

## 8. Shared-file ownership (coordination with TK-4ba6bd1964de)

This ticket owns only:

- `tools/aionui-extension/team/TEAM_ADAPTER_CONTRACT.md` (this file)
- `tools/aionui-extension/team/HOST_API_EVIDENCE.md`
- `tools/aionui-extension/team/adapter.cjs`
- `tools/aionui-extension/tests/team_adapter.test.cjs`

It does not edit `aion-extension.json`, `webui/routes.js`, `webui/app.js`,
`webui/index.html`, `webui/style.css`, `build.py`, `contexts/`, or `vendor/`. Proposed
future wiring (a `/pursers/team` route family or a settings-tab panel calling this
adapter, plus adding `team/adapter.cjs` to `build.py` `PACKAGE_FILES`) is intentionally
left to the door-onboarding/first-run contract in TK-4ba6bd1964de so that shared
manifest/routes have a single owner per change; this contract is the interface that
wiring should consume.

## 9. Testing and acceptance boundary

- Shipped with this ticket: `tests/team_adapter.test.cjs` (node:test, injected fake
  transport). Contract tests assert request payloads byte-for-byte against the schemas
  quoted in `HOST_API_EVIDENCE.md`, plus idempotency, identity-conflict, partial-failure,
  dry-run default, and unsupported-surface behavior. No test mutates a real Team.
- Remaining real acceptance (honest report, owned by TK-4f1f01ba50eb / operator GUI):
  live `team_spawn_agent` ack shape, live kickoff delivery, live interrupt/shutdown
  propagation, REST routes, GUI team creation. None of it is claimed from mocks here.

## 10. Shipped implementation status (this ticket)

- `team/adapter.cjs` (CommonJS, zero runtime dependencies beyond `node:child_process`)
  exports: `createTeamAdapter({ runCli })`, `validateTeamSpec(spec)`,
  `buildSeatKickoff(spec, seat)`, `buildLeadBrief(spec)`, `defaultRunCli(command,
  input)`, `OUTCOMES`, `HOST_ERROR_CODES`, `CONTRACT = "agent-facing-team-cli"`,
  `CONTRACT_SCHEMA_VERSION = 1`.
- Adapter instance operations: `status({tasks})`, `plan(spec)`, `apply(spec)`,
  `pauseSeat(slot_id, message, reason?)`, `stopSeat(slot_id, reason?)`,
  `renameSeat(slot_id, new_name)`, `resetSeatContext(slot_id)`, `removeSeat(slot_id)`,
  `createTeam()`, `archiveTeam()`, `listAssistants()`,
  `describeAssistant(assistant_id, locale?)`.
- Default transport: `execFile(process.env.AIONUI_HELPER_BIN || "aioncore", ["team",
  ...argv])`, request JSON on stdin, stdout parsed as the host envelope, 30000 ms
  timeout, 4 MiB max buffer; transport problems collapse to `transport_unavailable`.
- Tests: `tests/team_adapter.test.cjs` (node:test) — 18 contract tests covering
  validation, plan diffing, idempotent reconcile, identity-conflict freeze, dry-run
  default, byte-exact live payloads, kickoff delivery, partial-failure continuation,
  spawn-ack fallback (`spawn_ack_unparsed`), `runtime_context_missing` propagation,
  unsupported-surface honesty, pause/stop/rename/reset payloads, status passthrough,
  and kickoff/lead-brief invariant text. All green; existing suites unaffected.
