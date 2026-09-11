# Beta sandbox: AionUi installation and Personal transport readiness

Status: source-derived, read-only preparation. This report does not claim that
the candidate was installed, that a Personal process was launched, that a
browser observation passed, or that the Beta candidate is approved.

The working Pursers source for this investigation is
`7f5ca61a556ed881567dadbc8c49f4b4a6a74c4a`. The final verifier must substitute
the independently approved final SHA and must rederive every candidate value
from that exact clean checkout.

## Observations

The installed desktop application reports AionUi `2.2.2`. Its bundled binary
reports `aioncore 0.2.2`, and the bundled manifest identifies the official
`v0.2.2` release archive. Official AionCore tag `v0.2.2` resolves to
`47e66d0d151123e973b3fd1e77afcb5671b3f8c5`.

The installed binary exposes `--data-dir`, `--work-dir`, and `--app-version` as
top-level options. It does not have a `start` subcommand. The verifier must pass
the actual host application version, `2.2.2`, to `--app-version`. The default is
the AionCore binary version, `0.2.2`; using that default makes the working
manifest's `engine.aionui="^2.2.1"` incompatible and filters the extension.

The working candidate currently derives `name="pursers"`, `version="0.1.0"`,
and 26 archive entries. These are observations, not frozen release inputs. The
final verifier must derive the name, version, archive filename, entry allowlist,
and embedded `candidate_commit` again from the final approved artifact.

There is no implemented local-ZIP upload or remote-download path in AionCore
`0.2.2`. The Hub installer expects an already expanded directory named by a Hub
index entry, validates the manifest and contributions, and then hot-reloads the
registry. `AIONUI_EXTENSIONS_PATH` is the highest-priority scan path and the
first install target; otherwise the target is `<data-dir>/extensions`.

The repository's current `tools/aionui-extension/README.md` distinguishes two
cases. It permits expanding the verifier-built ZIP as one extension directory
under a fresh isolated root for verification. It forbids hand-unzipping into an
existing AionUi data directory. Therefore managed Hub publication is not a
prerequisite for the isolated acceptance host, while mutation of existing app
or user data remains prohibited.

## Required verifier-owned parameters

The verifier supplies all of the following; none may be copied from an
untrusted receipt or assistant response:

- `FINAL_SHA`: the independently approved 40-character candidate SHA.
- `CLEAN_REPOSITORY`: a clean checkout at `FINAL_SHA`.
- `RUN_ROOT`: a newly created private directory outside the checkout.
- `AIONUI_APP`: the actual installed, signed AionUi application bundle.
- `AIONCORE`: the bundled AionCore executable inside `AIONUI_APP`.
- `PORT`, authenticated base URL, user identity, session token, and CSRF value
  required by the live host.
- `SANDBOX_BOARD`, sandbox profile, host ID, and session ID.
- `RUNTIME_PYTHON`: the exact Python executable for the clean candidate.
- `CHALLENGE_KEY`, `RUNTIME_RECEIPT`, a fresh nonce, and evidence output paths,
  all private and outside the checkout.

No credential value belongs in the report, command log, Git tree, MCP row
description, or ticket.

## Supported isolated extension installation

Run the following only from the exact clean candidate checkout. The two build
directories must be distinct and private.

```sh
git -C /PATH/TO/CLEAN/REPOSITORY rev-parse HEAD
git -C /PATH/TO/CLEAN/REPOSITORY status --porcelain
/PATH/TO/RUNTIME_PYTHON -m pytest \
  /PATH/TO/CLEAN/REPOSITORY/tools/aionui-extension/tests/test_package.py
/PATH/TO/RUNTIME_PYTHON \
  /PATH/TO/CLEAN/REPOSITORY/tools/aionui-extension/build.py \
  --output /PATH/TO/PRIVATE/build-one/pursers-aionui-0.1.0.zip
/PATH/TO/RUNTIME_PYTHON \
  /PATH/TO/CLEAN/REPOSITORY/tools/aionui-extension/build.py \
  --output /PATH/TO/PRIVATE/build-two/pursers-aionui-0.1.0.zip
shasum -a 256 /PATH/TO/PRIVATE/build-one/pursers-aionui-0.1.0.zip
shasum -a 256 /PATH/TO/PRIVATE/build-two/pursers-aionui-0.1.0.zip
```

The two SHA-256 values must match. The package test must confirm the final
allowlist, embedded exact HEAD, deterministic bytes, and absence of secrets,
home paths, and private identifiers. The literal archive name above is the
working value; replace it with the filename derived from the final manifest.

Create a fresh isolated root and expand only after validating every ZIP member.
This executable block rejects absolute paths, `..`, non-regular entries,
unexpected names, and any destination escape before writing a file:

```sh
umask 077
mkdir -p /PATH/TO/PRIVATE/RUN_ROOT/extensions/pursers
/PATH/TO/RUNTIME_PYTHON - \
  /PATH/TO/CLEAN/REPOSITORY/tools/aionui-extension/build.py \
  /PATH/TO/PRIVATE/build-one/pursers-aionui-0.1.0.zip \
  /PATH/TO/PRIVATE/RUN_ROOT/extensions/pursers <<'PY'
import importlib.util
import stat
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

builder_path, archive_path, destination_path = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("candidate_builder", builder_path)
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
expected = list(builder.PACKAGE_FILES)
destination = destination_path.resolve(strict=True)
assert not any(destination.iterdir()), "destination must start empty"
with ZipFile(archive_path) as archive:
    assert archive.namelist() == expected
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        mode = info.external_attr >> 16
        assert stat.S_ISREG(mode)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve(strict=True)
        assert resolved_parent == destination or destination in resolved_parent.parents
        target.write_bytes(archive.read(info))
PY
```

Write the Hub index beside the extension directory. Values must be derived from
the extracted manifest; they must not be copied from this working report:

```sh
/PATH/TO/RUNTIME_PYTHON - \
  /PATH/TO/PRIVATE/RUN_ROOT/extensions/pursers/aion-extension.json \
  /PATH/TO/PRIVATE/RUN_ROOT/extensions/index.json <<'PY'
import json
import sys
from pathlib import Path

manifest_path, index_path = map(Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
index = {
    "schema_version": 1,
    "extensions": [{
        "name": manifest["name"],
        "version": manifest["version"],
        "display_name": manifest.get("displayName"),
        "bundled": False,
    }],
}
index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
```

Confirm the actual host and binary versions, then launch the bundled binary in
the foreground with fresh isolated data and work directories. Do not use
`--local`, do not add a nonexistent `start` subcommand, and do not change the
installed application bundle:

```sh
defaults read /PATH/TO/AionUi.app/Contents/Info.plist CFBundleShortVersionString
/PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore --version
mkdir -p /PATH/TO/PRIVATE/RUN_ROOT/data /PATH/TO/PRIVATE/RUN_ROOT/work
AIONUI_EXTENSIONS_PATH=/PATH/TO/PRIVATE/RUN_ROOT/extensions \
  /PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore \
  --host 127.0.0.1 \
  --port 25999 \
  --data-dir /PATH/TO/PRIVATE/RUN_ROOT/data \
  --work-dir /PATH/TO/PRIVATE/RUN_ROOT/work \
  --app-version 2.2.2
```

After the verifier establishes an authenticated session through the supported
host flow, install by manifest name. `POST /api/hub/install` accepts only that
name, not a ZIP path or URL. The cookie jar below is verifier-owned and the
`x-csrf-token` value must exactly match its `aionui-csrf-token` cookie:

```sh
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  -H 'Content-Type: application/json' \
  -H 'x-csrf-token: <VERIFIER_CSRF_TOKEN>' \
  --data '{"name":"pursers"}' \
  http://127.0.0.1:25999/api/hub/install
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  http://127.0.0.1:25999/api/hub/extensions
```

The final request body must use the name derived from the final extracted
manifest. A successful operation must be corroborated by the authenticated Hub
list, extension registry/assets, and later signed-listener/browser evidence; an
HTTP 200 envelope by itself is not an acceptance pass.

## Supported Personal MCP registration and launch

AionCore `0.2.2` supports a user MCP row with a stdio `command`, `args`, and
`env`. The exact acceptance row is:

```json
{
  "name": "pursers-personal-acceptance",
  "transport": {
    "type": "stdio",
    "command": "/PATH/TO/RUNTIME_PYTHON",
    "args": [
      "-I",
      "-m",
      "pursers_personal.cli",
      "mcp",
      "--profile",
      "/PATH/TO/SANDBOX/profile.json",
      "--host-id",
      "<HOST_ID>",
      "--session",
      "<SESSION_ID>",
      "--acceptance-runtime-receipt",
      "/PATH/TO/PRIVATE/RUN_ROOT/personal-runtime-receipt.json",
      "--acceptance-challenge-key",
      "/PATH/TO/PRIVATE/RUN_ROOT/challenge.key",
      "--candidate-source",
      "/PATH/TO/CLEAN/REPOSITORY/packages/personal/src/pursers_personal/apps_server.py",
      "--candidate-commit",
      "<FINAL_SHA>",
      "--board-id",
      "<SANDBOX_BOARD>"
    ],
    "env": {}
  }
}
```

The installed CLI accepts this JSON on standard input. It requires the existing
agent runtime context variables and its injected short-lived runtime token:

```sh
AIONUI_BASE_URL=http://127.0.0.1:25999 \
AIONUI_CONVERSATION_ID=<AUTHENTICATED_EXISTING_CONVERSATION_ID> \
AIONUI_USER_ID=<AUTHENTICATED_USER_ID> \
AIONUI_RUNTIME_TOKEN=<VERIFIER_RUNTIME_TOKEN> \
  /PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore \
  config mcp servers create < /PATH/TO/PRIVATE/personal-mcp-row.json
```

The equivalent authenticated API is `POST /api/mcp/servers` with the same JSON
body. Record the returned MCP row ID and read it back. Never put the AionUi
runtime token in the MCP row's `env` object.

The five acceptance arguments are all-or-none. Before runtime creation, require
the receipt path to be absent, the key and receipt parent to be private,
non-symlink paths outside the checkout, a clean checkout at `FINAL_SHA`, and a
sandbox/test board ID. The isolated interpreter must already contain the exact
candidate package, and this preflight must resolve the loaded module to the
clean candidate source named in the MCP row:

```sh
/PATH/TO/RUNTIME_PYTHON -I -c \
  'from pathlib import Path; import pursers_personal.apps_server as a; print(Path(a.__file__).resolve(strict=True))'
```

Generate a verifier-owned key and nonce:

```sh
umask 077
openssl rand -out /PATH/TO/PRIVATE/RUN_ROOT/challenge.key 32
openssl rand -hex 32 > /PATH/TO/PRIVATE/RUN_ROOT/nonce.txt
test ! -e /PATH/TO/PRIVATE/RUN_ROOT/personal-runtime-receipt.json
```

Create a new Codex ACP conversation. MCP selection is snapshotted at creation;
patching an existing conversation cannot replace it:

```sh
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  -H 'Content-Type: application/json' \
  -H 'x-csrf-token: <VERIFIER_CSRF_TOKEN>' \
  --data '{"type":"acp","name":"Pursers Personal acceptance","extra":{"workspace":"/PATH/TO/WORKSPACE","backend":"codex","selected_mcp_server_ids":["<RETURNED_MCP_ROW_ID>"]}}' \
  http://127.0.0.1:25999/api/conversations
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  -H 'x-csrf-token: <VERIFIER_CSRF_TOKEN>' \
  -X POST \
  http://127.0.0.1:25999/api/conversations/<NEW_CONVERSATION_ID>/runtime/ensure
```

Read the new conversation and require its immutable snapshot to contain exactly
the returned row ID, the expected MCP name, and loaded status. AionCore selects
explicit snapshot IDs even if a row's current global enabled flag is false. It
resolves the executable, preserves the configured args and env, converts the
row to a stdio MCP descriptor, and sends it in Codex `thread/start`
`config.mcp_servers`. Codex has stdio support and launches the child. AionCore
re-sends the same surface on `thread/resume` because a bare resume loses it.

Do not directly launch Personal from the verifier. Such a process is detached
from the selected AionUi/Codex transport and cannot satisfy this proof.

## Raw tool result, HMAC, and PID linkage

Send one fresh nonce in the new conversation and instruct Codex to call only
`acceptance_runtime_attest`. Wait for the turn to finish through the host's
WebSocket/UI, then retrieve the actual non-compact tool row, not the assistant's
summary text:

```sh
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  -H 'Content-Type: application/json' \
  -H 'x-csrf-token: <VERIFIER_CSRF_TOKEN>' \
  --data '{"content":"Call exactly the pursers-personal-acceptance MCP tool acceptance_runtime_attest with nonce <FRESH_NONCE>. Do not alter the nonce and do not use a CLI substitute.","files":[],"sessions":[],"inject_skills":[],"hidden":false}' \
  http://127.0.0.1:25999/api/conversations/<NEW_CONVERSATION_ID>/messages
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  http://127.0.0.1:25999/api/conversations/<NEW_CONVERSATION_ID>/messages?limit=200 \
  > /PATH/TO/PRIVATE/RUN_ROOT/conversation-messages.json
curl --fail-with-body \
  --cookie /PATH/TO/PRIVATE/RUN_ROOT/aionui-cookie.jar \
  http://127.0.0.1:25999/api/conversations/<NEW_CONVERSATION_ID>/messages/<CALL_ID> \
  > /PATH/TO/PRIVATE/RUN_ROOT/tool-call.json
```

The selected row must have `type="tool_call"`, terminal message status
`finish`, content status `completed`, and a single call ID. Its persisted
`content.input` is the complete Codex `mcpToolCall` start item, so its serialized
tree must identify `acceptance_runtime_attest` and contain the exact fresh nonce.
Its `content.output` is produced from the completed MCP result's text and
`structuredContent`; parse the attestation object from this field. This is
AionCore-owned tool evidence from the same conversation, not model prose.

Verify the signature independently. The candidate signs canonical compact JSON
of every claim field except `signature`, with sorted keys and HMAC-SHA256:

```sh
/PATH/TO/RUNTIME_PYTHON - \
  /PATH/TO/PRIVATE/RUN_ROOT/tool-call.json \
  /PATH/TO/PRIVATE/RUN_ROOT/challenge.key \
  /PATH/TO/PRIVATE/RUN_ROOT/nonce.txt \
  /PATH/TO/PRIVATE/RUN_ROOT/personal-runtime-receipt.json <<'PY'
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

tool_path, key_path, nonce_path, receipt_path = map(Path, sys.argv[1:])
row = json.loads(tool_path.read_text(encoding="utf-8"))["data"]
assert row["type"] == "tool_call"
assert row["status"] == "finish"
content = row["content"]
assert content["status"] == "completed"
nonce = nonce_path.read_text(encoding="utf-8").strip()

def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)

input_strings = list(strings(content["input"]))
assert "acceptance_runtime_attest" in input_strings
assert input_strings.count(nonce) == 1
output = content["output"].strip()

required = {
    "schema_version", "server_name", "version", "build", "candidate_commit",
    "candidate_source", "board_id", "pid", "transport", "nonce", "signature",
}
decoder = json.JSONDecoder()
candidates = []
for offset, char in enumerate(output):
    if char != "{":
        continue
    try:
        value, _ = decoder.raw_decode(output[offset:])
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and set(value) == required:
        candidates.append(value)
unique = {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in candidates}
assert len(unique) == 1
claim = json.loads(unique.pop())
signature = claim.pop("signature")
payload = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode("utf-8")
expected = hmac.new(key_path.read_bytes(), payload, hashlib.sha256).hexdigest()
assert hmac.compare_digest(signature, expected)
assert claim["nonce"] == nonce
assert claim["schema_version"] == 1
assert claim["server_name"] == "On Board Personal"
assert claim["candidate_commit"] == "<FINAL_SHA>"
assert claim["candidate_source"] == "/PATH/TO/CLEAN/REPOSITORY/packages/personal/src/pursers_personal/apps_server.py"
assert claim["board_id"] == "<SANDBOX_BOARD>"
assert claim["transport"] == "stdio"
assert isinstance(claim["pid"], int) and claim["pid"] > 1
source = Path(claim["candidate_source"])
assert claim["build"] == hashlib.sha256(source.read_bytes()).hexdigest()
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
for field in ("build", "candidate_commit", "candidate_source", "board_id", "pid", "transport"):
    assert receipt[field] == claim[field]
os.kill(claim["pid"], 0)
print(json.dumps(claim, indent=2, sort_keys=True))
PY
```

If the MCP framework renders one text block before the structured JSON, select
the one JSON object whose required keys are exactly the attestation fields; do
not read an assistant text row and do not accept a substring match as a claim.

Finally capture the live process command for the HMAC-authenticated PID and
compare it to the registered row's resolved executable and exact argument list:

```sh
/bin/ps -p <HMAC_AUTHENTICATED_PID> -o pid=,ppid=,command=
```

The PID authority comes from the signed claim returned through the active MCP
call. The fresh receipt and `ps` result only corroborate that authenticated
claim. A receipt-only PID, a copied PID file, a process with merely similar
arguments, or an attestation from a separately launched Personal process fails.

## Readiness gaps and negative gates

- No runtime action in this report was executed. An independent verifier still
  owns installation, authenticated API calls, browser capture, nonce generation,
  HMAC verification, and the final verdict.
- Rebuild and rederive all candidate values from the independently approved
  final SHA; do not reuse the working `0.1.0`, 26-entry inventory, or source
  digest if the final tree differs.
- Verify the actual signed AionUi bundle and the live bundled-AionCore listener;
  a downloaded standalone binary or unsigned replacement does not satisfy the
  host boundary.
- Do not spoof `--app-version` to bypass engine compatibility. It must equal the
  independently observed host application version.
- Reject ZIP traversal, extra entries, symlinks/special files, nondeterministic
  builds, dirty source, embedded secrets, and extraction into existing data.
- Reject missing or multiple MCP IDs, mutable/reused conversation snapshots,
  non-Codex backends, failed MCP startup, assistant-prose-only evidence,
  receipt-only identity, stale/reused nonces, HMAC mismatch, dead PID, argv
  mismatch, non-sandbox board, or any direct verifier-spawned substitute.
- Keep the verifier-started AionCore process in the foreground. Stop only that
  recorded process after evidence capture. For recoverable cleanup, move only
  the freshly created `RUN_ROOT` into a verifier-owned trash/staging directory;
  never recursively target an unresolved variable, checkout, app bundle,
  existing AionUi data directory, or home directory.

## Source references

Official AionCore `v0.2.2`, commit
`47e66d0d151123e973b3fd1e77afcb5671b3f8c5`:

- `crates/aionui-extension/src/loader.rs:21-60,84-103` — scan priority and
  install-target resolution.
- `crates/aionui-extension/src/hub/index_manager.rs:16-52,58-119` — exact
  `index.json` schema and target directory.
- `crates/aionui-extension/src/hub/installer.rs:57-61,79-121` — expanded
  directory requirement, verification, missing downloader, and hot reload.
- `crates/aionui-extension/src/hub_routes.rs:31-42,77-87` and
  `crates/aionui-api-types/src/extension.rs:70-74` — authenticated Hub route and
  name-only install body.
- `crates/aionui-app/src/router/state.rs:1108-1121` — Hub directory is the
  resolved install target.
- `crates/aionui-api-types/src/mcp.rs:10-23,56-67` — MCP stdio and create
  request schema.
- `crates/aionui-mcp/src/routes.rs:59-80,88-128` — authenticated MCP CRUD route.
- `crates/aionui-app/src/commands/cmd_config.rs:21-24,427-455` and
  `crates/aionui-app/assets/builtin-skills/auto-inject/aionui-config/SKILL.md:278-300`
  — runtime environment names, stdin JSON CLI, and create endpoint.
- `crates/aionui-api-types/src/conversation.rs:77-88,357-378` and
  `crates/aionui-conversation/src/routes.rs:114-147,250-310,389-400` — create,
  ensure-runtime, message list/detail, and raw message content API.
- `crates/aionui-conversation/src/service.rs:1331-1421,2288-2300` — MCP
  snapshot construction and post-creation immutability.
- `crates/aionui-ai-agent/src/factory/acp.rs:210-280,411-480,490-516,592-619`
  — selected-row load, explicit-ID precedence, exact stdio conversion, and
  executable/argument resolution.
- `crates/aionui-session/src/backend/descriptor.rs:62-78` and
  `crates/aionui-session/src/backend/codex_conn.rs:538-585` — Codex stdio
  capability and `thread/start`/resume injection.
- `crates/aionui-session/src/backend/codex_conn.rs:3156-3305,3720-3766` — the
  raw start item and completed MCP result mapping.
- `crates/aionui-ai-agent/src/session_agent.rs:4256-4285,4579-4593` — tool result
  conversion into persisted output.
- `crates/aionui-conversation/src/stream_persistence.rs:448-498` and
  `crates/aionui-conversation/src/convert.rs:153-175` — upsert by call ID and
  unmodified DB JSON conversion for API response.
- `crates/aionui-conversation/src/stream_relay.rs:850-877` — same tool event on
  the live WebSocket path.

Working Pursers candidate
`7f5ca61a556ed881567dadbc8c49f4b4a6a74c4a`:

- `tools/aionui-extension/README.md:17-40` — managed-Hub limitation, approved
  isolated expansion, required app version, and existing-data prohibition.
- `tools/aionui-extension/aion-extension.json:1-10` — working manifest name,
  version, and engine range.
- `tools/aionui-extension/build.py:13-40,45-64,67-87` and
  `tools/aionui-extension/tests/test_package.py:21-80` — allowlist,
  exact-candidate embedding, deterministic regular entries, and secret scan.
- `packages/personal/src/pursers_personal/cli.py:1052-1064,1269-1279` — exact
  Personal MCP argument surface.
- `packages/personal/src/pursers_personal/apps_server.py:1967-2025,2047-2060`
  — key/source validation, canonical claim, HMAC, live PID, and model-only tool.
- `packages/personal/src/pursers_personal/apps_server.py:2496-2556,2559-2610`
  — clean-SHA receipt, private path checks, all-or-none arguments, and stdio run.
