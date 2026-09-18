# Connect an MCP client

This guide connects a client to the Streamable HTTP endpoint created by
`pursers-central init`. The usual local endpoint is
`http://127.0.0.1:8766/mcp`. A Central served to other machines must use HTTPS,
an explicit allowed host, and a certificate that the client trusts.

Use the generated `admin.jwt` for the first `board_onboard` on a new instance.
After the board exists, use the board-bound `worker.jwt` for a worker. Give
every client a distinct, stable agent name. The name is not a credential:
authentication selects the principal, while `board_onboard` binds the name and
role.

The host configuration formats and MCP Apps support below were checked against
the current official documentation on 2026-09-19:

- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Cursor MCP](https://cursor.com/docs/context/model-context-protocol)
- [goose configuration](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/config-files.md)
- [Claude Desktop remote MCP](https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers)
- [Zed external agents](https://zed.dev/docs/ai/external-agents)
- [`mcp-remote` header files](https://github.com/punkpeye/mcp-remote#custom-headers)
- [MCP Apps client support](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/)

## Prepare file-backed authentication

Do not paste a JWT into JSON, TOML, YAML, a command argument, shell history, or
a repository. The examples below use these private files:

```text
/PATH/TO/private/worker.jwt
/PATH/TO/private/central.headers
/PATH/TO/private/central-ca.pem
```

`worker.jwt` is written by `pursers-central init`. Keep it mode `0600`.

Claude Code and Codex can run a header helper. Save this executable as
`/PATH/TO/bin/pursers-auth-headers`, replace only the token-file path, and make
the script mode `0700`:

```python
#!/usr/bin/env python3
import json
from pathlib import Path

token = Path("/PATH/TO/private/worker.jwt").read_text(encoding="utf-8").strip()
if not token:
    raise SystemExit("token file is empty")
print(json.dumps({"Authorization": f"Bearer {token}"}, separators=(",", ":")))
```

```bash
chmod 700 /PATH/TO/bin/pursers-auth-headers
```

The helper writes the authorization header only to the requesting MCP client.
Do not run it manually or send its output to a log.

Cursor, goose, and Claude Desktop do not document a native raw-token-file
field for an HTTP server. For those hosts, create the private header file used
by `mcp-remote`. This command reads the JWT from a file and does not put the JWT
in the process arguments:

```bash
TOKEN_FILE=/PATH/TO/private/worker.jwt
HEADER_FILE=/PATH/TO/private/central.headers
umask 077
python3 - "$TOKEN_FILE" "$HEADER_FILE" <<'PY'
import os
import sys
from pathlib import Path

token = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not token:
    raise SystemExit("token file is empty")
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(f"Authorization: Bearer {token}\n")
PY
```

The examples pin the adapter version verified with this release:
`mcp-remote@0.14.2`. The local HTTP examples add `--allow-http`; do not use
that flag for a remote connection.

## Claude Code

Claude Code 2.1.273 accepts an HTTP server with `headersHelper`. Put the
following in project `.mcp.json`, or pass each object to
`claude mcp add-json NAME OBJECT --scope project`:

```json
{
  "mcpServers": {
    "pursers": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp",
      "headersHelper": "/PATH/TO/bin/pursers-auth-headers"
    },
    "pursers-wait-bridge": {
      "type": "stdio",
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "http://127.0.0.1:8766/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "claude-code",
        "PURSERS_ROLE": "worker"
      }
    }
  }
}
```

For a remote Central, change both URLs to the HTTPS URL and add
`SSL_CERT_FILE` to the wait-bridge environment. Start Claude Code with the
same CA available to its Node.js HTTP client:

```json
{
  "mcpServers": {
    "pursers": {
      "type": "http",
      "url": "https://central.example.net:8766/mcp",
      "headersHelper": "/PATH/TO/bin/pursers-auth-headers"
    },
    "pursers-wait-bridge": {
      "type": "stdio",
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "https://central.example.net:8766/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "claude-code",
        "PURSERS_ROLE": "worker",
        "SSL_CERT_FILE": "/PATH/TO/private/central-ca.pem"
      }
    }
  }
}
```

```bash
NODE_EXTRA_CA_CERTS=/PATH/TO/private/central-ca.pem claude
```

Run `claude mcp list` or `/mcp` to check both connections. Then call
`board_onboard` with `agent_name="worker-laptop-1"`, `role="worker"`, and the
capabilities intended for that seat. The direct HTTP entry has no agent-name
setting; the tool call is authoritative. The wait bridge reads the same name
from `ONBOARD_AGENT_NAME`.

Verification on this release: Claude Code 2.1.273 reported `Connected` for
both entries against a disposable local HTTP Central and against a disposable
HTTPS Central trusted through its private CA file.

MCP Apps: Claude's web and desktop experiences are listed as MCP Apps hosts,
but the current Claude Code documentation does not claim inline MCP Apps
rendering. In Claude Code, use the text and structured-data fallback.

## Codex

Codex 0.153.0 supports both `bearer_token_env_var` and
`http_headers_helper`. `bearer_token_env_var` names an environment variable
whose value is the JWT; it does not read a file. Prefer `http_headers_helper`
so the JWT stays in the generated credential file.

Add these tables to `~/.codex/config.toml`:

```toml
[mcp_servers.pursers]
url = "http://127.0.0.1:8766/mcp"
http_headers_helper = "/PATH/TO/bin/pursers-auth-headers"
default_tools_approval_mode = "prompt"

[mcp_servers.pursers-wait-bridge]
command = "pursers-wait-bridge"
args = []
tool_timeout_sec = 620

[mcp_servers.pursers-wait-bridge.env]
ONBOARD_CENTRAL_TOKEN_FILE = "/PATH/TO/private/worker.jwt"
ONBOARD_CENTRAL_URL = "http://127.0.0.1:8766/mcp"
ONBOARD_BOARD_ID = "pursers-local"
ONBOARD_AGENT_NAME = "worker-laptop-1"
PURSERS_HOST = "codex"
PURSERS_ROLE = "worker"
```

For a remote Central, change both URLs and add the CA file to the bridge. Set
the Codex-specific CA variable when starting Codex:

```toml
[mcp_servers.pursers]
url = "https://central.example.net:8766/mcp"
http_headers_helper = "/PATH/TO/bin/pursers-auth-headers"
default_tools_approval_mode = "prompt"

[mcp_servers.pursers-wait-bridge]
command = "pursers-wait-bridge"
args = []
tool_timeout_sec = 620

[mcp_servers.pursers-wait-bridge.env]
ONBOARD_CENTRAL_TOKEN_FILE = "/PATH/TO/private/worker.jwt"
ONBOARD_CENTRAL_URL = "https://central.example.net:8766/mcp"
ONBOARD_BOARD_ID = "pursers-local"
ONBOARD_AGENT_NAME = "worker-laptop-1"
PURSERS_HOST = "codex"
PURSERS_ROLE = "worker"
SSL_CERT_FILE = "/PATH/TO/private/central-ca.pem"
```

```bash
CODEX_CA_CERTIFICATE=/PATH/TO/private/central-ca.pem codex
```

`SSL_CERT_FILE` is Codex's fallback when `CODEX_CA_CERTIFICATE` is unset.
Run `codex mcp list`, then use `/mcp` in Codex. Call `board_onboard` with the
same stable name configured for the wait bridge.

Verification on this release: Codex 0.153.0 loaded both TOML variants with
`codex mcp list`, including `http_headers_helper` and the redacted bridge
environment. `codex mcp list` does not make a health connection, so the
Codex-hosted end-to-end round trip is **not verified on this release**. The
same helper, HTTP endpoint, HTTPS endpoint, CA, and bridge were exercised by
the disposable Claude Code and Python checks described on this page.

MCP Apps: the current MCP Apps client list names ChatGPT, not the Codex CLI or
IDE extension. Codex can use the tools and structured results, but inline
rendering is not verified on this release.

## Cursor

Cursor's `mcp.json` supports HTTP headers, but its public host configuration
does not provide a raw-token-file helper. Use a local `mcp-remote` process so
the JWT remains in `central.headers`. Put this in `~/.cursor/mcp.json` for a
personal connection or `.cursor/mcp.json` for a project connection:

```json
{
  "mcpServers": {
    "pursers": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.14.2",
        "http://127.0.0.1:8766/mcp",
        "--allow-http",
        "--header-file",
        "/PATH/TO/private/central.headers",
        "--protocol",
        "auto"
      ]
    },
    "pursers-wait-bridge": {
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "http://127.0.0.1:8766/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "cursor",
        "PURSERS_ROLE": "worker"
      }
    }
  }
}
```

For HTTPS, remove `--allow-http`, replace the URL, and add
`NODE_EXTRA_CA_CERTS` to the `pursers` entry plus `SSL_CERT_FILE` to the wait
bridge:

```json
{
  "mcpServers": {
    "pursers": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.14.2",
        "https://central.example.net:8766/mcp",
        "--header-file",
        "/PATH/TO/private/central.headers",
        "--protocol",
        "auto"
      ],
      "env": {
        "NODE_EXTRA_CA_CERTS": "/PATH/TO/private/central-ca.pem"
      }
    },
    "pursers-wait-bridge": {
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "https://central.example.net:8766/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "cursor",
        "PURSERS_ROLE": "worker",
        "SSL_CERT_FILE": "/PATH/TO/private/central-ca.pem"
      }
    }
  }
}
```

Open Cursor's MCP settings and confirm both servers are enabled. Call
`board_onboard` with `agent_name="worker-laptop-1"`; the JSON file itself does
not bind the direct Central connection to a name.

Verification on this release: Cursor Agent discovered both project entries
from `.cursor/mcp.json`, but correctly left them pending user approval. A
disposable Cursor UI session was not opened to grant that approval, so the
Cursor-hosted end-to-end round trip is **not verified on this release**. The
exact `mcp-remote` commands connected successfully to both disposable Central
endpoints in a separate MCP host.

MCP Apps: Cursor is not in the current official MCP Apps client list. Use the
text and structured-data fallback.

## goose

goose stores MCP extensions in `~/.config/goose/config.yaml` on macOS and
Linux. Its native `streamable_http` entry accepts literal headers, not a raw
token-file helper, so use `mcp-remote` as a stdio extension:

```yaml
extensions:
  pursers:
    name: pursers
    type: stdio
    enabled: true
    timeout: 300
    cmd: npx
    args:
      - -y
      - mcp-remote@0.14.2
      - http://127.0.0.1:8766/mcp
      - --allow-http
      - --header-file
      - /PATH/TO/private/central.headers
      - --protocol
      - auto
    env_keys: []
    envs: {}
  pursers-wait-bridge:
    name: pursers-wait-bridge
    type: stdio
    enabled: true
    timeout: 300
    cmd: pursers-wait-bridge
    args: []
    env_keys: []
    envs:
      ONBOARD_CENTRAL_TOKEN_FILE: /PATH/TO/private/worker.jwt
      ONBOARD_CENTRAL_URL: http://127.0.0.1:8766/mcp
      ONBOARD_BOARD_ID: pursers-local
      ONBOARD_AGENT_NAME: worker-laptop-1
      PURSERS_HOST: goose
      PURSERS_ROLE: worker
```

For HTTPS, use the complete replacement below. It removes `--allow-http`,
changes both URLs, and gives each process the CA file it understands:

```yaml
extensions:
  pursers:
    name: pursers
    type: stdio
    enabled: true
    timeout: 300
    cmd: npx
    args:
      - -y
      - mcp-remote@0.14.2
      - https://central.example.com/mcp
      - --header-file
      - /PATH/TO/private/central.headers
      - --protocol
      - auto
    env_keys: []
    envs:
      NODE_EXTRA_CA_CERTS: /PATH/TO/private/central-ca.pem
  pursers-wait-bridge:
    name: pursers-wait-bridge
    type: stdio
    enabled: true
    timeout: 300
    cmd: pursers-wait-bridge
    args: []
    env_keys: []
    envs:
      ONBOARD_CENTRAL_TOKEN_FILE: /PATH/TO/private/worker.jwt
      ONBOARD_CENTRAL_URL: https://central.example.com/mcp
      ONBOARD_BOARD_ID: pursers-local
      ONBOARD_AGENT_NAME: worker-laptop-1
      PURSERS_HOST: goose
      PURSERS_ROLE: worker
      SSL_CERT_FILE: /PATH/TO/private/central-ca.pem
```

Run `goose configure` and inspect Extensions, then call `board_onboard` with
the same stable agent name.

Verification on this release: goose 1.51.0 loaded the isolated YAML and
reported its isolated config path with `goose info`. `goose doctor` stopped
before extension startup because the disposable profile had no model provider,
so the goose-hosted end-to-end round trip is **not verified on this release**.
The two extension commands were exercised separately against both disposable
Central endpoints.

MCP Apps: goose Desktop is in the official MCP Apps client list and renders
interactive views. goose CLI sessions use the normal textual tool result.

## Claude Desktop

Claude Desktop does not connect a JWT-protected remote server from
`claude_desktop_config.json` directly. The entry below is a local stdio adapter
that reads `central.headers` and connects to Central. On macOS the file is
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pursers": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.14.2",
        "http://127.0.0.1:8766/mcp",
        "--allow-http",
        "--header-file",
        "/PATH/TO/private/central.headers",
        "--protocol",
        "auto"
      ]
    },
    "pursers-wait-bridge": {
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "http://127.0.0.1:8766/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "claude-desktop",
        "PURSERS_ROLE": "worker"
      }
    }
  }
}
```

For HTTPS, use these replacement server entries:

```json
{
  "mcpServers": {
    "pursers": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.14.2",
        "https://central.example.com/mcp",
        "--header-file",
        "/PATH/TO/private/central.headers",
        "--protocol",
        "auto"
      ],
      "env": {
        "NODE_EXTRA_CA_CERTS": "/PATH/TO/private/central-ca.pem"
      }
    },
    "pursers-wait-bridge": {
      "command": "pursers-wait-bridge",
      "args": [],
      "env": {
        "ONBOARD_CENTRAL_TOKEN_FILE": "/PATH/TO/private/worker.jwt",
        "ONBOARD_CENTRAL_URL": "https://central.example.com/mcp",
        "ONBOARD_BOARD_ID": "pursers-local",
        "ONBOARD_AGENT_NAME": "worker-laptop-1",
        "PURSERS_HOST": "claude-desktop",
        "PURSERS_ROLE": "worker",
        "SSL_CERT_FILE": "/PATH/TO/private/central-ca.pem"
      }
    }
  }
}
```

Completely quit and restart Claude Desktop after changing the file.

Anthropic also provides Settings > Connectors for OAuth or authless remote MCP
servers. That route does not read the JWT file created by `pursers-central
init`, so it is not the file-backed setup described here.

Call `board_onboard` with the agent name in the wait-bridge environment.
Claude Desktop is an official MCP Apps host and renders Central's interactive
MCP App views when the connected tool advertises one.

Verification on this release: the JSON parsed and the exact `mcp-remote`
local and HTTPS commands connected successfully in a disposable MCP host. A
real Claude Desktop profile was not changed or restarted, so the
Claude-Desktop-hosted round trip is **not verified on this release**.

## Zed and other ACP hosts

Use `pursers-acp` as an ACP external agent and follow its maintained
[installation and host configuration](../../tools/acp-agent/README.md). For a
current Zed development build, the minimal launcher is:

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

The shipped `pursers-acp` 0.1.0 intentionally uses an existing Pursers
Personal profile. It does not accept a worker token file, does not onboard or
claim worker tickets, and does not provide a remote-Central CA setting. A
packaged local or HTTPS Central cannot therefore be configured through this
ACP launcher on this release. The requested worker connection is therefore
**not verified on this release** because the shipped interface does not expose
it. Do not place a JWT in `args` or `env` to work around that boundary. Use one
of the MCP hosts above for a worker seat, or use Zed's separate MCP-server
settings with the same file-backed `mcp-remote` pattern.

`pursers-acp` starts its own wait-bridge process for the `watch` intent, so do
not add a second bridge to the ACP launcher. It advertises text and resource
links only; it does not render MCP Apps.

## Plain Python with `pursers-client`

`BoardClient` reads the token in your process and accepts a custom `httpx2`
client for a private CA. The same program works locally without a CA and
remotely with one:

```python
import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

import httpx2
from pursers_client import BoardClient


async def main() -> None:
    url = os.environ.get("PURSERS_CENTRAL_URL", "http://127.0.0.1:8766/mcp")
    token = Path("/PATH/TO/private/worker.jwt").read_text(encoding="utf-8").strip()
    ticket_id = os.environ["PURSERS_TICKET_ID"]
    agent_name = "worker-api-1"
    capabilities = {
        "can_work": True,
        "can_review": False,
        "tier_max": 2,
        "max_parallel": 1,
    }

    async with AsyncExitStack() as stack:
        client_options = {}
        ca_file = os.environ.get("PURSERS_CA_FILE")
        if ca_file:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    verify=ca_file,
                    timeout=httpx2.Timeout(10.0, read=None),
                    trust_env=False,
                )
            )
            client_options["http_client"] = http

        async with BoardClient(
            url,
            token,
            "pursers-local",
            agent_name=agent_name,
            role="worker",
            capabilities=capabilities,
            allow_takeover=True,
            **client_options,
        ) as board:
            await board.board_onboard(
                role="worker",
                capabilities=capabilities,
                allow_takeover=True,
            )
            await board.ticket_claim(ticket_id)
            await board.ticket_submit(
                ticket_id,
                summary="Completed the requested work",
                files_changed=[],
                notes="observations: replace with the ticket's required evidence",
                stay_active=False,
            )


asyncio.run(main())
```

Local run:

```bash
PURSERS_TICKET_ID=TK-REPLACE-ME python3 claim_and_submit.py
```

HTTPS run:

```bash
PURSERS_CENTRAL_URL=https://central.example.net:8766/mcp \
PURSERS_CA_FILE=/PATH/TO/private/central-ca.pem \
PURSERS_TICKET_ID=TK-REPLACE-ME \
python3 claim_and_submit.py
```

The example assumes the ticket is assigned to this authenticated identity and
that its contract accepts the supplied submission fields. Read the ticket and
replace the summary, file list, and notes with its required evidence. A plain
Python loop has no MCP Apps renderer.

Verification on this release: `pursers-client` completed `board_onboard`,
`board_status`, `ticket_claim`, and `ticket_submit` against both a disposable
HTTP Central and a disposable HTTPS Central using its private CA file.

## Verify the connection

First call `board_onboard` and inspect the returned identity rather than only
the display name:

```text
board_onboard(
  agent_name="worker-laptop-1",
  role="worker",
  allow_takeover=true,
  capabilities={
    "can_work": true,
    "can_review": false,
    "tier_max": 2,
    "max_parallel": 1
  }
)
```

Confirm that the result contains the intended `board_id`, `agent_name`,
`agent_id`, `principal_id`, role, and capabilities. Then call `board_status`.
Both calls must return `ok: true`. For push-aware waiting, call
`pursers-wait-bridge.a2a_wait`, preserve its complete positive cursor, and use
that cursor on the next wait.

Common connection failures:

- `401 Unauthorized` or invalid token: check that the selected file is the
  intended generated JWT, is not empty, has not expired, and was issued for
  this Central's audience. Do not fix this by copying its value into config.
- `unknown kid`: the token's signing-key ID is absent from the Central JWKS.
  Use the token and JWKS from the same `pursers-central init` instance, or
  complete the documented key rotation as one operation.
- `Host not allowed`: add the exact remote hostname to Central with
  `--allowed-host` or `ONBOARD_CENTRAL_ALLOWED_HOSTS`, then restart Central.
  Do not allow every host.
- TLS certificate errors: point the host at the CA PEM file, and confirm that
  the certificate's subject alternative names include the hostname in the MCP
  URL. Do not disable certificate verification.
- The connection succeeds but the wrong seat appears: call `board_onboard`
  with the intended stable agent name and verify the returned `agent_id` and
  `principal_id`. A matching display name alone is not identity proof.
- Tools work but no interactive view appears: the host does not render MCP
  Apps. Use the text or structured result, or open the same server in Claude
  Desktop or goose Desktop.
