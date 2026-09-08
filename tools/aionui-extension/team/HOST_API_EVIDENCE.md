# Installed AionUi/AionCore Team API — evidence

Ticket: TK-628602eedb90. All evidence below was captured read-only from the installed
host on 2026-09-08. No real user Team was started, stopped, spawned into, or mutated;
live calls were limited to read-only tools (`capabilities`, `members`,
`list-assistants`, `describe-assistant`) and read-only file/database inspection.
Absolute home paths are redacted as `<home>`; loopback product paths are kept verbatim.

## 1. Installed versions and provenance

| Item | Value | Source |
|---|---|---|
| AionUi desktop app | `2.2.1` | `defaults read /Applications/AionUi.app/Contents/Info.plist CFBundleShortVersionString` |
| Bundled backend binary | `aioncore 0.2.1` | `aioncore --version` |
| Backend bundle manifest | `platform: darwin`, `arch: arm64`, `version: v0.2.1`, `generatedAt: 2026-09-01T10:23:47.249Z`, `sourceType: download` | `/Applications/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/manifest.json` |
| Upstream official source/release | `https://github.com/iOfficeAI/AionCore/releases/download/v0.2.1/aioncore-v0.2.1-aarch64-apple-darwin.tar.gz` | same `manifest.json`, field `source.url` |
| Extension engine constraint | `"engine": { "aionui": "^2.2.1" }` | `tools/aionui-extension/aion-extension.json` |
| Team persistence | `~/.aionui/aionui-backend.db` (SQLite; tables include `teams`, `team_tasks`, `mailbox`, `agent_metadata`, `assistant_*`) | read-only `sqlite3 .tables` |
| Data schema level | `teams.agents_version = '1.0.1'` observed on live rows | read-only query |

`aioncore --help` (verbatim excerpt) establishes the supported agent-facing domains:

```
Commands:
  capabilities               Print the top-level agent-facing CLI capability index
  config                     Agent-facing automation CLI for AionUi configuration
  diagnose                   Agent-facing read-only troubleshooting CLI for AionUi diagnosis
  team                       Agent-facing Team collaboration CLI fallback
  session                    Cross-session messaging: ...
  skills                     Agent-facing read-only runtime CLI for THIS conversation's skills. ...
  mcp-team-stdio             Stdio ↔ TCP bridge for the team MCP server (spawned by the ACP agent CLI). ...
```

`aioncore team --help` (verbatim command list): `capabilities, help, context, members,
read-messages, send-message, interrupt-agent, task, list-assistants,
describe-assistant, spawn-agent, rename-agent, clear-agent-context, shutdown-agent`.
Live subcommands take their request as a JSON object on stdin (the `--help` of
`spawn-agent`, `shutdown-agent`, `interrupt-agent`, `members`, `context` shows no
request flags, only `-h`), matching the assistant rule "Both read their arguments as a
JSON object on stdin".

## 2. Agent-facing CLI contract (`aioncore capabilities`)

`aioncore capabilities` → `{"success": true, "data": {...}}` with, verbatim:

- `"contract": "agent-facing-aioncore-cli"`, `"schema_version": 1`, `"stability": "stable"`.
- Output envelope: stdout JSON, `"success_shape": {"success": true, "data": {}, "meta": {"schema_version": 1}}`, stderr only for a single stable `..._FAILED` line.
- `runtime_context.environment`: `["AIONUI_HELPER_BIN", "AIONUI_BASE_URL", "AIONUI_CONVERSATION_ID", "AIONUI_USER_ID"]`; primary selector `AIONUI_CONVERSATION_ID`.

## 3. Team contract (`aioncore team capabilities`)

Verbatim top-level fields: `"contract": "agent-facing-team-cli"`, `"schema_version": 1`.

`data.commands` (which subcommands need the conversation runtime env):

```json
{
  "capabilities": { "runtime_env_required": [] },
  "help":         { "runtime_env_required": [] },
  "context":      { "runtime_env_required": ["AIONUI_BASE_URL", "AIONUI_USER_ID", "AIONUI_CONVERSATION_ID", "AIONUI_RUNTIME_TOKEN"] },
  "tool_call":    { "runtime_env_required": ["AIONUI_BASE_URL", "AIONUI_USER_ID", "AIONUI_CONVERSATION_ID", "AIONUI_RUNTIME_TOKEN"] }
}
```

`data.output_envelope`: `{"success": "boolean", "data": "object when success=true",
"error": "object when success=false", "meta": {"schema_version": 1}}`.

`data.errors` (exact enum): `unknown_tool, schema_validation_failed,
permission_denied, team_not_found, conversation_not_found, agent_not_found,
not_in_team, transport_unavailable, runtime_context_missing, runtime_auth_failed`.

`data.tools` — 13 tools, each mapping an MCP tool name to CLI argv, permission, and a
stdin JSON schema (all schemas `"type": "object", "additionalProperties": false`):

| MCP tool | CLI argv (`aioncore team ...`) | Permission | Required input | Optional input |
|---|---|---|---|---|
| `team_members` | `members` | any_team_agent | — | — |
| `team_read_messages` | `read-messages` | any_team_agent | — | `since_message_id` |
| `team_send_message` | `send-message` | any_team_agent | `to`, `message` | `files[]` (absolute paths) |
| `team_interrupt_agent` | `interrupt-agent` | lead_only | `slot_id`, `message` | `files[]`, `reason` |
| `team_task_create` | `task create` | any_team_agent | `subject` | `description`, `owner`, `blocked_by[]` |
| `team_task_update` | `task update` | any_team_agent | `task_id` | `status` enum `pending|in_progress|completed|deleted`, `description`, `owner`, `blocked_by[]` |
| `team_task_list` | `task list` | any_team_agent | — | `owner`, `status` (string or array), `include_deleted` (default true when `status` omitted), `limit` (clamped to 200) |
| `team_list_assistants` | `list-assistants` | any_team_agent | — | — |
| `team_describe_assistant` | `describe-assistant` | any_team_agent | `assistant_id` | `locale` |
| `team_spawn_agent` | `spawn-agent` | lead_only | `name`, `assistant_id` | — |
| `team_rename_agent` | `rename-agent` | lead_only | `slot_id`, `new_name` | — |
| `team_clear_agent_context` | `clear-agent-context` | lead_only | `slot_id` | — |
| `team_shutdown_agent` | `shutdown-agent` | lead_only | `slot_id` | `reason` |

Verbatim schema quotes for the lifecycle-critical tools:

`team_spawn_agent.stdin_json_schema.properties`:

```json
{
  "name":         { "type": "string", "description": "Agent display name" },
  "assistant_id": { "type": "string", "description": "Assistant ID to spawn. Call team_list_assistants when you need candidates; the runtime backend is derived from this assistant." }
}
```
required: `["name", "assistant_id"]`. There is NO workspace, prompt, model, role, or
tier parameter — this is the hard host boundary the adapter contract works within.

`team_shutdown_agent`: properties `slot_id` ("Agent slot_id to shut down"), `reason`
("Reason for shutdown"); required `["slot_id"]`.

`team_interrupt_agent`: properties `slot_id` ("Exact teammate slot_id; wildcard is not
supported"), `message` ("Replacement instruction delivered after interruption"),
`files[]` ("Absolute attachment paths"), `reason`; required `["slot_id", "message"]`.

`team_rename_agent`: `slot_id`, `new_name`; both required.
`team_clear_agent_context`: `slot_id`; required.
`team_members`: empty object input.

## 4. Live read-only verification (shapes actually returned by the running host)

- `team_members {}` on a live 4-agent Team returned an array of members with exactly
  these keys: `slot_id` (UUIDv7 string), `name`, `role` (`lead` | `teammate`), `status`
  (observed: `idle`, `working`), `assistant_id`, `model`. Example row (test-fleet data):
  `{"slot_id": "01a07f7f-3adb-7f60-9427-f04e37ecbf9f", "name": "<seat-name>", "role":
  "teammate", "status": "working", "assistant_id": "bare:600c6601", "model":
  "dashscope/qwen3.8-max"}`.
- `team_list_assistants {}` returned `{"assistants": [{"assistant_id", "name",
  "backend", "description", "skills"}]}` with real catalog entries, e.g.
  `bare:8e1acf31` = "Codex CLI" backend `codex`; `bare:2d23ff1c` = "Claude Code"
  backend `claude`; `bare:600c6601` = "Goose" backend `goose`; `bare:632f31d2` =
  "Aion CLI" backend `aionrs`; plus built-in assistants (`aionui-assistant`,
  `word-creator`, ...) carrying `skills[]`.
- `team_describe_assistant {"assistant_id": "bare:600c6601"}` returned
  `{"status": "ok", "assistant_id", "name": "Goose", "description",
  "description_markdown", "skills": [], "example_tasks": [], "default_model": null}`
  and the hint: `Use team_spawn_agent with assistant_id="bare:600c6601"`.
- `team_read_messages`, `team_task_list`, `team_task_update` were exercised read-mostly
  by the seat conversation itself (task status transition only); response envelopes
  matched `{... , "task": {task_id, subject, status, owner, blocked_by}}` /
  `{messages[], returned_count, total_unread_count, has_more, next_since_message_id}`.

`team_spawn_agent`, `team_shutdown_agent`, `team_interrupt_agent`,
`team_rename_agent`, `team_clear_agent_context` were NOT exercised (would mutate a
real Team). Their request shapes are contract-verified above; their response `data`
shapes remain for live acceptance (TK-4f1f01ba50eb).

## 5. Persistence model (read-only inspection of `~/.aionui/aionui-backend.db`)

`teams` DDL (verbatim columns):

```sql
CREATE TABLE IF NOT EXISTS "teams" (
    id             TEXT PRIMARY KEY NOT NULL,
    user_id        TEXT    NOT NULL DEFAULT 'system_default_user',
    name           TEXT    NOT NULL,
    workspace      TEXT    NOT NULL DEFAULT '',
    workspace_mode TEXT    NOT NULL DEFAULT 'shared',
    agents         TEXT    NOT NULL DEFAULT '[]',
    lead_agent_id  TEXT,
    session_mode   TEXT,
    agents_version TEXT    NOT NULL DEFAULT '1.0.0',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
, project_id TEXT, folder_id TEXT, archived_at INTEGER);
```

- `teams.workspace` holds one absolute directory for the whole Team; observed live rows
  use `workspace_mode = 'shared'`. There is no per-agent workspace column.
- `teams.agents` is a JSON array; every live agent object has exactly the keys
  `["assistant_id", "backend", "conversation_id", "model", "name", "role", "slot_id"]`
  (`role` observed: `lead`, `teammate`). No prompt/rules/workspace keys per agent:
  per-seat workspace and prompt are delivered through conversation messages and seat
  folders inside the shared Team workspace (the pattern the Pursers seat kit uses).
- `archived_at` (nullable INTEGER) is how Team removal/archival persists; observed set
  on one archived Team row. No agent-facing CLI tool exposes archive/removal.
- `team_tasks` DDL columns: `id, team_id, subject, description, status CHECK IN
  ('pending','in_progress','completed','deleted'), owner, blocked_by (JSON), blocks
  (JSON), metadata, created_at, updated_at` — matches the `team_task_*` tool contract.
- `mailbox` table backs `team_send_message`/`team_read_messages`.

Runtime process bookkeeping: `~/.aionui/runtime/agent-process-registry.json` is a JSON
object with top-level keys `["version", "processes"]` (per-agent process registry used
by the backend; not an agent-facing API).

## 6. HTTP routes observed in the installed binary (NOT exercised)

Extracted from `strings` of `/Applications/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore`
(v0.2.1). Listed for completeness; the adapter treats all of them as unverified:

```
/api/teams
/api/teams/{id}
/api/teams/{id}/run-state
/api/teams/{id}/mailbox
/api/teams/{id}/tasks
/api/teams/{id}/activity
/api/teams/{id}/name
/api/teams/{id}/agents
/api/teams/{id}/agents/{slot_id}
/api/teams/{id}/agents/{slot_id}/name
/api/teams/{id}/agents/{slot_id}/model
/api/teams/{id}/agents/{slot_id}/messages
/api/teams/{id}/agents/{slot_id}/interrupt
/api/teams/{id}/agents/{slot_id}/attach
/api/teams/{id}/agents/{slot_id}/runtime/restart
/api/teams/{id}/agents/{slot_id}/context/reset
/api/teams/{id}/conversations/{conversation_id}/config-options
/api/teams/{id}/runs/{team_run_id}/cancel
/api/teams/{id}/runs/{team_run_id}/agents/{slot_id}/cancel
/api/teams/{id}/runs/{team_run_id}/agents/{slot_id}/pause
/api/teams/{id}/session
/api/teams/{id}/active-lease
/api/teams/{id}/session-mode
/api/runtime/team-tools/call
/api/runtime/team-tools/context
```

`/api/runtime/team-tools/call` and `/api/runtime/team-tools/context` are the endpoints
behind the `team` CLI/MCP bridge (from `data.commands.tool_call` /
`data.commands.context` runtime-env requirements). Server defaults from
`aioncore --help`: `--host 127.0.0.1`, `--port 25808`, `--data-dir data`,
`--identity-mode webui` (auth on) vs `--local` (auth skipped); the desktop app runs the
backend with `--port 0` (ephemeral) against `--data-dir ~/.aionui`.

## 7. Gap summary (grounding for `unsupported_by_host`)

| Capability asked by the ticket | Supported agent-facing surface in v0.2.1 | Gap handling |
|---|---|---|
| Create Team | none (GUI/REST `/api/teams`, unverified) | `unsupported_by_host` |
| Create teammates | `team_spawn_agent {name, assistant_id}` (lead_only) | supported |
| Per-seat workspace | none (team-level `teams.workspace`, `workspace_mode: shared`) | convention + kickoff prompt text |
| Per-seat prompt | none at spawn; delivered via `team_send_message` after spawn | supported by composition |
| Listing/status | `team_members`, `team_task_list`, `team_read_messages` | supported |
| Pause | `team_interrupt_agent` (turn-level); run-level pause REST-only | turn-level supported; run-level `unsupported_by_host` |
| Stop | `team_shutdown_agent` (cooperative request) | supported (request semantics documented) |
| Removal (teammate) | none; REST `/api/teams/{id}/agents/{slot_id}` observed only | `unsupported_by_host` |
| Removal (Team) | none agent-facing; `teams.archived_at` persistence observed | `unsupported_by_host` |
| Model/role/tier per seat | model: REST-only observed; Pursers role/tier: not host concepts | compare-only; carried in prompt |

## 8. Reproducing this evidence (all read-only)

```sh
/Applications/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore --version
.../aioncore capabilities
.../aioncore --data-dir ~/.aionui team capabilities
.../aioncore team --help ; .../aioncore team spawn-agent --help
sqlite3 ~/.aionui/aionui-backend.db ".tables"   # plus per-table .schema
# inside a Team conversation (MCP or "$AIONUI_HELPER_BIN" team ...):
#   team_members {}, team_list_assistants {}, team_describe_assistant {assistant_id}
```

## 9. Exercise log for this ticket (TK-628602eedb90)

Live host calls actually executed while gathering this evidence — all read-only:
`aioncore --version`, `aioncore capabilities`, `aioncore --data-dir ~/.aionui team
capabilities`, `aioncore team --help` and per-subcommand `--help`, in-conversation
`team_members {}`, `team_list_assistants {}`, `team_describe_assistant
{"assistant_id": "bare:600c6601"}`, read-only `sqlite3` inspection of
`~/.aionui/aionui-backend.db` (`.tables`, `.schema`, selected `teams` columns), and
`strings` over the installed binary for route observation. No Team was created,
spawned into, interrupted, shut down, renamed, archived, or removed; no real user
Team was started or stopped at any point.

Local validation executed in the ticket worktree (no host interaction):

```
node --test tools/aionui-extension/tests/team_adapter.test.cjs   # node v24.20.0: tests 18, pass 18, fail 0
node --test tools/aionui-extension/tests/routes.test.cjs         # pass 3, fail 0 (regression)
python3 -m pytest tools/aionui-extension/tests -q                # 8 passed
python3 tools/leak_scan.py tools/aionui-extension/team tools/aionui-extension/tests/team_adapter.test.cjs
# leak_scan: clean (0 violations)
git diff --check                                                 # clean
```
