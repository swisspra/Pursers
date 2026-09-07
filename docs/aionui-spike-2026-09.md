# AionUi 2.2.1 Pursers integration spike

Date: 2026-09-07

Plan of record: `plan-doors-http-aion`

Scope: live GUI/API probe against the isolated `sandbox-aion-763e2b` board. No
Central, client, or wait-bridge code was changed.

## Result

| Check | Verdict | Evidence |
| --- | --- | --- |
| 1. Extension MCP reaches a session | **Direct: NO-GO. REST-import fallback: GO.** | The extension contribution appeared under Settings > Tools and its connection check listed `board_digest` and `a2a_wait`, but a fresh GUI Codex conversation had `mcp_server_ids: []` and returned `UNAVAILABLE`. Importing the same transport through `POST /api/mcp/servers/import` produced a loaded MCP snapshot in an API-created Codex conversation. That conversation called both tools on the sandbox board. |
| 2. Preset assistant end to end | **NO-GO.** | `GET /api/extensions/assistants` normalized the contribution, including its 20-line context, but the preset was absent from the Assistants screen and `GET /api/assistants`. There was therefore no preset-started seat to join, wait, claim, or submit. |
| 3. API-opened preset conversation | **NO-GO.** | `POST /api/conversations` returned `201`, but silently ignored the extension assistant ID. The row had no `backend`, `preset_assistant_id`, or `preset_context`; sending `start` failed with `ACP agent requires either agent_id or backend in extra`. The active count did not increase at creation. API deletion succeeded and the sandbox board showed no lease. |
| 4. Two parallel preset conversations | **NO-GO; not run past the hard gate.** | Check 3 proved that the extension preset does not resolve to a runnable agent. Starting two such rows cannot produce distinct seats, names, or offers. |

The viable 2.2.1 path is therefore an authenticated MCP import plus an
explicitly constructed conversation. The proposed single extension cannot yet
provide the promised zero-file-edit preset-seat flow.

## Environment and safety

- Settings > About reported `AionUi Version 2.2.1`.
- The bundled binary reported `aioncore 0.2.1`:

  ```sh
  /PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/<TARGET>/aioncore --version
  # aioncore 0.2.1
  ```

- The extension directory was inventoried before the temporary install. The
  temporary extension, token file, and probe runtime were removed after the
  test. A later coordinator check found the disabled imported MCP row and two
  spike-named conversation rows still present in AionUi's local database. They
  are inactive but retained pending cleanup through a paired AionUi session.
- All bridge calls named only `sandbox-aion-763e2b`. One early ambiguous probe
  resolved to an already configured connector and returned the production
  board name; the probe was stopped, the server was renamed uniquely to
  `Pursers Spike Wait Bridge`, and subsequent calls were sandbox-only.

## Check 1: MCP contribution and fallback

The working manifest shape used a nested transport object:

```json
{
  "name": "pursers-spike",
  "version": "0.0.1",
  "engine": { "aionui": "^2.2.1" },
  "contributes": {
    "mcpServers": [
      {
        "id": "pursers-spike-wait-bridge",
        "name": "Pursers Spike Wait Bridge",
        "transport": {
          "type": "stdio",
          "command": "/PATH/TO/pursers-wait-bridge",
          "args": [],
          "env": {
            "ONBOARD_CENTRAL_URL": "https://<CENTRAL_HOST>/mcp",
            "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/temporary-token-file",
            "SSL_CERT_FILE": "/PATH/TO/ca-bundle",
            "ONBOARD_BOARD_ID": "sandbox-aion-763e2b",
            "ONBOARD_AGENT_NAME": "aion-spike"
          }
        }
      }
    ]
  }
}
```

Click path and observed output:

```text
Settings > Tools > Pursers Spike Wait Bridge > Check MCP Availability
project_registry_get
board_digest
board_digest_ack
board_watch
board_unwatch
board_human_requests
a2a_wait
```

A new GUI conversation still excluded the contribution:

```text
New Chat > Codex CLI
Prompt: use only `Pursers Spike Wait Bridge`; call `board_digest` on the sandbox
Response: UNAVAILABLE
Persisted snapshot: mcp_server_ids=[] mcp_servers=[] mcp_statuses=[]
```

The fallback import used `AIONUI_SESSION_HEADER`, a shell variable containing
the authenticated browser-session header. Its value is intentionally omitted:

```sh
curl -X POST http://127.0.0.1:<AIONUI_PORT>/api/mcp/servers/import \
  -H "${AIONUI_SESSION_HEADER}" \
  -H 'x-csrf-token: <CSRF>' \
  -H 'Cookie: aionui-csrf-token=<CSRF>' \
  -H 'content-type: application/json' \
  --data '{"servers":[{"name":"Pursers Spike Wait Bridge","transport":{"type":"stdio","command":"/PATH/TO/pursers-wait-bridge","args":[],"env":{"ONBOARD_CENTRAL_URL":"<REDACTED>","ONBOARD_CENTRAL_TOKEN_FILE":"/PATH/TO/temporary-token-file","SSL_CERT_FILE":"/PATH/TO/ca-bundle","ONBOARD_BOARD_ID":"sandbox-aion-763e2b","ONBOARD_AGENT_NAME":"aion-spike"}},"builtin":false,"enabled":true}]}'
```

Redacted response and API-created session snapshot:

```json
{"success":true,"imported":[{"id":"<MCP_ID>","name":"Pursers Spike Wait Bridge","enabled":true,"type":"stdio"}]}
{"mcp_server_ids":["<MCP_ID>"],"mcp_servers":["Pursers Spike Wait Bridge"],"mcp_statuses":[{"id":"<MCP_ID>","name":"Pursers Spike Wait Bridge","status":"loaded"}]}
```

`board_digest` returned only the sandbox cursor and zero tickets. The same
session then made exactly one bounded wait:

```json
{"arguments":{"agent_name":"aion-spike","boards":["sandbox-aion-763e2b"],"only_mine":false,"timeout_s":3,"wait_for":"claimable"}}
{"new_seq":{"sandbox-aion-763e2b":1},"events":[],"waited_s":3.0,"timed_out":true,"mode":"poll","mode_by_board":{"sandbox-aion-763e2b":"poll"},"reason":"timeout"}
```

The fallback proves tool injection and execution, but not push: the tested
connection used polling and reported its subscription disconnected.

## Check 2: extension assistant

The assistant contribution used `agentId: "codex"`,
`presetAgentType: "codex"`, and a `contextFile` containing exactly 20
non-empty directive lines. Loader output was:

```json
{"id":"ext-pursers-sandbox-seat","name":"Pursers Sandbox Seat","agentId":"codex","isPreset":true,"enabled":true,"_source":"extension"}
```

The context loaded, but the runtime surfaces disagreed:

```text
GET /api/extensions/assistants -> count=1, Pursers preset present
Assistants screen               -> preset absent
GET /api/assistants             -> count=27, Pursers preset absent
```

Because the preset was not selectable, the offer/claim/submit timeline and a
push-held wait could not be exercised.

## Check 3: API-opened preset and close

Request:

```sh
curl -X POST http://127.0.0.1:<AIONUI_PORT>/api/conversations \
  -H "${AIONUI_SESSION_HEADER}" \
  -H 'x-csrf-token: <CSRF>' \
  -H 'Cookie: aionui-csrf-token=<CSRF>' \
  -H 'content-type: application/json' \
  --data '{"type":"acp","name":"Pursers extension preset API probe","assistant":{"id":"ext-pursers-sandbox-seat","locale":"en"},"source":"aionui","extra":{}}'
```

Observed sequence:

```text
POST /api/conversations -> HTTP 201, id=<CONVERSATION_ID>
active-count            -> 5 before, 5 after creation
persisted preset fields -> backend=null preset_assistant_id=null preset_context=null
POST .../messages start -> HTTP 202, then BAD_REQUEST
detail                  -> ACP agent requires either agent_id or backend in extra
DELETE .../<CONVERSATION_ID> -> success=true
sandbox board status    -> seat idle, lease_expires_at=null, current_offer=null
```

The `201` is not proof of a functioning preset: unknown extension assistant IDs
are accepted but resolve to no assistant snapshot.

## Check 4: parallel identity

Not started. A pair of conversations would inherit the same missing runtime
identity seen in check 3, so it could not test distinct `agent_id`, derived
`agent_name`, or independent offers. This remains a release-blocking gap for
the one-extension seat design.

## MCP elicitation

The effective MCP client was the nested Codex CLI, not AionUi itself. It
reported this declaration through `board_human_requests`:

```json
{"declared":{"form":true,"url":true,"raw":{"form":{},"url":{}}}}
```

With a pending sandbox form request, AionUi rendered only a generic
confirmation titled `Elicitation` with `Allow`, `Allow Always`, and `Reject`.
It did not render the requested schema field or the required disposition
selector. The form was rejected, resolved through the direct sandbox API, and
the temporary ticket was canceled. Verdict: **no native AionUi MCP form
renderer was proven**, even though the nested Codex client declared form and
URL support.

## Manifest loader findings

- `mcpServers[].transport` is required. Putting `command`, `args`, and `env`
  directly on the contribution produced a null transport; nesting them under
  `transport` fixed the loader output.
- `agentId` and `contextFile` were accepted. The context file was read.
- `presetAgentType` was silently discarded; it was absent from normalized
  output rather than rejected with an error.
- Extension assistants and stored assistant definitions are separate in this
  build. The conversation resolver reads the latter, so an extension assistant
  ID is silently ignored by `POST /api/conversations`.

## What the real extension must do differently

1. Import or create MCP rows through the authenticated local API until native
   extension contributions are included in conversation MCP snapshots.
2. Ensure GUI creation sends the imported MCP ID instead of an explicit empty
   selection; API creation without `selected_mcp_server_ids` used the enabled
   global fallback successfully.
3. Register presets in the assistant-definition store used by
   `/api/assistants` and the conversation resolver, or wait for AionUi to bridge
   extension assistants into that store.
4. Persist a runtime `backend`, the 20-line `preset_context`, and the MCP ID in
   the resolved assistant snapshot.
5. Derive `ONBOARD_AGENT_NAME` from the AionUi conversation ID in a bootstrap
   wrapper. A static manifest environment value cannot create distinct seats.
6. Treat push as unavailable until a new live probe returns `mode: "push"`;
   the measured fallback was `poll`.
7. Provide a dashboard/link fallback for human requests until AionUi renders
   the MCP form schema and disposition field rather than a generic approval.

## Sources

- [AionCore v0.2.1](https://github.com/iOfficeAI/AionCore/tree/v0.2.1)
- [AionHub extension repository](https://github.com/iOfficeAI/AionHub)
