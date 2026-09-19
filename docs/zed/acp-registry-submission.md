# Pursers ACP Registry submission

Status: prepared, not submitted. Registry requirements were checked on
2026-09-20 at [`agentclientprotocol/registry@9bd27065`](https://github.com/agentclientprotocol/registry/tree/9bd27065d5279bc668191e40413aacd361b8e696).
The ready-to-copy upstream directory is [`tools/acp-agent/pursers/`](../../tools/acp-agent/pursers/).

## Registry contract

The ACP Registry accepts one top-level directory whose name equals the
lowercase, hyphenated `id`. It must contain `agent.json` and `icon.svg`.
The manifest requires `id`, `name`, a stable numeric `version`, `description`,
`license_url`, and at least one distribution. `repository`, `website`,
`authors`, and SPDX `license` are optional. Unknown manifest fields are
rejected. Sources: [contribution guide], [format], and [JSON Schema].

Accepted distributions are:

- `npx`, with a published npm package;
- `uvx`, with a published PyPI package; or
- `binary`, with one or more supported OS/architecture targets, an archive URL,
  command, and preferably a SHA-256 digest.

Package and archive versions must equal the manifest version; `latest` is not
accepted. Binary targets are `darwin-aarch64`, `darwin-x86_64`,
`linux-aarch64`, `linux-x86_64`, `windows-aarch64`, and `windows-x86_64`.
The validator checks distribution URLs and package existence. Sources:
[contribution guide], [format], and [build validator].

The icon is required, square 16×16 SVG, and monochrome with only
`currentColor`, `none`, or `inherit` paint values. CI also launches every new
agent and requires the ACP `initialize` response to contain at least one
`authMethods` entry of type `agent` or `terminal`. Sources: [contribution
guide], [authentication requirements], and [auth validator].

Review happens through a pull request to the registry. CI validates schema,
directory/id uniqueness, versions, distribution reachability, icon shape and
colors, and the live authentication handshake. Merged package entries are
checked hourly for newer npm/PyPI versions. Sources: [contribution guide] and
[registry README].

## Prepared entry

`tools/acp-agent/pursers/agent.json` uses the published PyPI artifact
`pursers-acp==0.1.0` through `uvx`. It deliberately contains only upstream
schema fields. `icon.svg` is 16×16 and uses `currentColor`.

The runtime returns `pursers-personal-profile` (`type: agent`) in every
successful `initialize` response. If a profile is missing and the client
supports terminal authentication, it also returns `pursers-personal-login`
(`type: terminal`, `args: ["--login"]`). No bearer token, token path, or
credential is stored in the registry entry or Zed settings.

The five first-run commands published to Zed via ACP
`available_commands_update` are `/board`, `/create`, `/watch`, `/evidence`,
and `/answer`. Mutating `/create` and `/answer` requests use Zed's native
`session/request_permission`; only `allow_once` executes the prepared board
action. `/watch` publishes ACP plan updates while the subscription is active.

## Operator procedure

Do this only after the release owner confirms that `0.1.0` is still the
intended public version. Do not change the manifest version without a matching
published `pursers-acp` release.

```sh
REGISTRY=/PATH/TO/agentclientprotocol-registry
PURSERS=/PATH/TO/Pursers

test "$(git -C "$REGISTRY" rev-parse HEAD)" = \
  9bd27065d5279bc668191e40413aacd361b8e696
test ! -e "$REGISTRY/pursers"
cp -R "$PURSERS/tools/acp-agent/pursers" "$REGISTRY/pursers"

cd "$REGISTRY"
uv run --with jsonschema .github/workflows/build_registry.py
python3 .github/workflows/verify_agents.py --auth-check --agent pursers
git diff --check
git status --short
```

The first command performs the registry's own schema, ID, version,
distribution reachability, and icon checks. The second launches the published
uvx package and verifies its authentication handshake. A local source checkout
passing Pursers tests is not a substitute for that published-artifact check.

After both pass, the operator may create a registry fork/branch, commit exactly
`pursers/agent.json` and `pursers/icon.svg`, and open the pull request described
by the [contribution guide]. This repository task does not authorize creating
the fork, branch, issue, or pull request.

## Deferred R3 gaps

- **Thread identity and full worker/coordinator loop:** still deferred because
  the ACP product is intentionally a Personal board console; silently granting
  worker or coordinator authority would change its security model.
- **Model/repository execution and forwarded MCP tool use:** still deferred;
  adding a coding loop is a separate product and trust-boundary decision.
- **Persistent session list/load/resume/close:** still deferred because current
  sessions are memory-only and advertising persistence before storage exists
  would make Zed restore behavior dishonest.
- **Protocol-version negotiation and richer content/filesystem/terminal
  capabilities:** still deferred to a protocol-focused change. The agent keeps
  advertising only the narrow v1 surface it implements.
- **Remote-Central setup:** still uses the private file-backed Pursers Personal
  profile so secrets never move into Zed settings or registry metadata.

[registry README]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/README.md
[contribution guide]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/CONTRIBUTING.md
[format]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/FORMAT.md
[JSON Schema]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/agent.schema.json
[authentication requirements]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/AUTHENTICATION.md
[build validator]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/.github/workflows/build_registry.py
[auth validator]: https://github.com/agentclientprotocol/registry/blob/9bd27065d5279bc668191e40413aacd361b8e696/.github/workflows/verify_agents.py
