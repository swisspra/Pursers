# Use Pursers from Zed

The Pursers extension connects Zed's Agent Panel to one Pursers board. You can
check the board, create work, watch for changes, inspect delivery evidence, and
answer a seat's question without putting a bearer token in Zed settings. The
extension starts a local `pursers-mcp` relay, which reads the credential from a
token file and talks to Pursers Central.

## Commands

| Command | Result |
| --- | --- |
| `/board` | Show a compact board summary and the items that need you. |
| `/create <summary>` | Create one ticket after Zed shows its normal tool permission prompt. |
| `/watch` | Watch this board until an important update arrives; a seat question ends the turn so Zed can notify you. |
| `/evidence <ticket-id>` | Show the exact branch, commit, changed files, tests, and review state for one ticket. |
| `/answer <ticket-id> <answer>` | Answer a pending human request after Zed shows its permission prompt. In an ACP thread, use `/answer` or `/answer #N` to open a form for a watched seat question; `#N` is needed only when several are pending. |

These are MCP prompts. If another server defines the same prompt name, Zed may
prefix the command with the server ID, for example `/pursers.board`.

## Prerequisites

You need:

- [uv](https://docs.astral.sh/uv/), including the `uvx` command;
- a running Pursers Central; follow the [Pursers Quickstart](../../README.md#quickstart)
  if you do not have one; and
- the token-file path created by `pursers-central init DIR`. A local setup
  creates `DIR/worker.jwt`. Keep the file private and do not paste its contents
  into Zed.

## Install the extension

The extension is not listed in Zed Extensions yet. Install it from a Pursers
checkout for now:

1. Open the Zed Command Palette.
2. Run **zed: install dev extension**.
3. Select `integrations/zed/pursers-mcp` in the checkout.

After the extension is listed, open Zed Extensions, search for **Pursers**, and
select **Install** instead.

## Configure the connection

Open **Settings → AI → MCP Servers**, find **Pursers**, and open its native
configure modal. Enter a small JSONC object in that modal. You do not need to
edit `settings.json` directly.

| Setting | Required | Value |
| --- | --- | --- |
| `central_url` | Yes | The Central MCP endpoint, such as `http://127.0.0.1:8766/mcp`. |
| `board_id` | Yes | The board to use. The local Quickstart default is `pursers-local`. |
| `token_file` | Yes | The path to `worker.jwt` created by `pursers-central init`. Store the path, not the token. |
| `ca_file` | No | A CA certificate file when Central uses a private TLS certificate. |
| `uvx_path` | No | A different `uvx` executable path or command name. |
| `package_spec` | No | A different `pursers-client` version or a local client checkout for development. |

The default form is:

```json
{
  "central_url": "http://127.0.0.1:8766/mcp",
  "board_id": "pursers-local",
  "token_file": "/PATH/TO/PURSERS/worker.jwt"
}
```

![Pursers extension configure modal showing the default Central URL, local board ID, and token-file placeholder](../media/zed/configure-defaults-dark.png)

Save the modal. In **Settings → AI → MCP Servers**, confirm that Pursers has a
green state dot. Open the Agent Panel and run `/board`. A successful response
starts with the configured board ID and shows a compact status summary.

![Zed MCP Servers settings showing Pursers connected with a green state dot and extension badge](../media/zed/mcp-servers-connected-dark.png)

![Zed Agent Panel showing a compact Pursers board summary and Needs you ticket list](../media/zed/agent-board-summary-dark.png)

### Restricted Mode

Zed opens a project it has not seen before in Restricted Mode, and that blocks
every MCP server from starting. Pursers then shows no state dot and its commands
are missing, with nothing in the server log to explain why. Click **Trust and
Continue** in the banner at the top of the workspace, once per project, and the
server starts.

## Daily loop

Keep one Agent Panel thread for the board:

1. Run `/board` to see what needs attention.
2. Run `/create <summary>` when you want to add work. Review Zed's native tool
   permission prompt before allowing the write.

   ![Zed Agent Panel waiting for permission before the Pursers ticket_create write](../media/zed/create-permission-dark.png)

3. Run `/watch` while claims, reviews, or questions matter. Keep that turn
   active. The first new seat question, review verdict, or failure ends the
   turn after its update so Zed's normal completion notification can wake you;
   run `/watch` again when you want the next important update.

   ![Zed Agent Panel showing an active Pursers watch with a new ticket event and advanced cursor](../media/zed/watch-event-dark.png)
4. Run `/evidence TK-…` before acting on a submission. Check the exact commit,
   file list, test output, and independent-review state.

   ![Zed Agent Panel showing bounded ticket evidence with branch, commit, files, tests, and review state](../media/zed/agent-evidence-dark.png)

5. Merge only through your normal, explicitly authorized repository workflow.
   The extension does not add a merge button.

### When a seat asks you a question

Questions reach you in Zed only while `/watch` is active and the Personal
profile selects a coordinator identity registered to at least one project on
the board. In that case, each new seat question appears as a clearly marked
update with the seat name, ticket ID, question, waiting time, and a short `#N`
reference. The agent then completes that watch turn; Zed does not notify for a
free-standing `session/update`, but it does apply its normal completion
notification when the turn stops. Run this to open the answer form without
retyping the ticket ID:

```text
/answer
```

If several questions have accumulated, select one explicitly; Pursers never
guesses:

```text
/answer #2
```

The session-scoped `elicitation/create` form shows the question and fields for
the ticket's required evidence. Accepting the form proceeds to a separate
`allow_once` permission request for the board write. Declining or cancelling
the form, rejecting permission, or closing the thread leaves the question
unanswered on the board.

The update can reach a loaded ACP thread for the lifetime of its connection,
but Zed drops it after the thread entity is released. There is no delivery or
notification when Zed is closed entirely, and no background watcher remains
after the watch turn ends. Questions raised while `/watch` is stopped appear
only after `/watch` runs again; its initial read checks the coordinator's
current open-question inbox rather than relying on retained journal events.
That inbox read is bounded to the oldest 100 open questions. If the selected
identity is a worker or is not registered as a project coordinator, `/watch`
states that seat-question replies are unavailable and continues streaming
ordinary board updates; it does not widen that identity's authority. The MCP
extension still has no push channel that can wake the Agent Panel; use a
correctly registered coordinator ACP thread for these same-thread replies, or
check the board elsewhere.

## Optional: use a Pursers ACP thread

Use `pursers-acp` when you prefer a dedicated Pursers thread in Zed's Agent
Panel. The ACP thread publishes the same five commands, uses Zed's native
permission prompt for writes, and can show plan state while `/watch` is active.
It uses an existing Pursers Personal profile; it does not use the worker token
from the MCP extension and it is not a coding worker.

The ACP Registry entry is prepared but not listed yet. Until it is listed,
install `pursers-acp` and add the current custom agent configuration to Zed:

```sh
python -m pip install "pursers-acp==0.1.0"
```

```json
{
  "agent_servers": {
    "pursers": {
      "type": "custom",
      "command": "pursers-acp",
      "args": ["--project", "/PATH/TO/PROJECT"],
      "env": {}
    }
  }
}
```

Start a new external-agent thread and select **Pursers**. If the project does
not have a Personal profile, run the documented setup first or launch
`pursers-acp --project /PATH/TO/PROJECT --login` once.

Prefer the MCP extension when you want Pursers tools inside a normal Zed Agent
thread. Prefer the ACP thread when you want a separate board conversation and
native ACP plan updates. Re-run `/watch` after each surfaced question if more
questions must arrive in Zed.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Zed reports that it could not start `uvx`. | Install uv, confirm `uvx --version` works in a new terminal, then restart Zed. Set `uvx_path` only when Zed needs an explicit executable path. |
| Authentication fails or the token file is rejected. | Confirm that `token_file` points to the correct `worker.jwt`, that the file is readable by your account, and that it contains one credential line. Do not paste the token into the modal. |
| Central is unreachable. | Check `central_url`, confirm Central is running, and confirm the URL ends at the intended Central origin or `/mcp` endpoint. |
| A private TLS endpoint fails certificate validation. | Set `ca_file` to the private CA certificate file. Do not disable certificate verification. |
| A tool reports a timeout. | Check Central health and retry the bounded command. Keep `/watch` active instead of using an unbounded tool call; the relay caps each wait below Zed's 60-second default. |
| Pursers has no state dot at all and no log output. | The project is in Restricted Mode. Click **Trust and Continue** in the banner at the top of the workspace. |
| A command is missing. | Confirm the Pursers MCP server has a green state dot. Restart it after changing settings. If a prompt name collides, look for the server-prefixed form such as `/pursers.board`. |

Zed 1.20.2 speaks MCP `2025-11-25`. Pursers Central also supports newer MCP
`2026-07-28` features, but Zed does not consume those subscription APIs
directly. The local relay handles the protocol boundary and exposes the
bounded `/watch` workflow instead.
