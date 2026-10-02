# MCP resources and prompt discovery

Pursers exposes a small versioned help catalog through the local `pursers-mcp`
relay. The catalog describes available content; it does not preload the full
guides, skill bundle, board, or ticket history. Reading a resource never grants
authority or performs a workflow mutation.

## URI manifest

The discovery schema is `pursers.discovery.v1`; the current content version is
`2026-10-02`.

| URI | Content | Authorization and cache behavior |
| --- | --- | --- |
| `pursers://help/index` | Titles, purpose, URI templates, version, applicability, and host fallback | Static procedural help; cached by content version |
| `pursers://help/roles/{role}` | Prerequisites, boundaries, and next actions for `worker`, `reviewer`, `coordinator`, or `orchestrator` | Static procedural help; unsupported roles return an explicit unavailable document |
| `pursers://help/workflows/{workflow}` | Purpose, mutation boundary, and safe next action for `board`, `create`, `watch`, `evidence`, `answer`, or `setup` | Static procedural help; unsupported workflows return an explicit unavailable document |
| `pursers://boards/{board_id}/summary` | Fresh compact board status | Only the relay's configured board; Central rechecks the equivalent `board_status` authorization on every read; no dynamic cache |
| `pursers://boards/{board_id}/tickets/{ticket_id}` | Fresh compact ticket detail | Only the relay's configured board; Central rechecks the equivalent `ticket_get` authorization on every read; no dynamic cache |
| `board://{board_id}/digest` | Compatibility digest maintained by `pursers-wait-bridge` | Preserved for orchestrator mode; a missing engine returns `orchestrator_engine_unavailable`, never a fake empty board |

Board resources return `not_authorized_or_unavailable` without echoing Central's
private denial details. Select a connection for an authorized board and retry the
equivalent tool when its typed error is needed. Static help and dynamic board data
are not stored in a shared cache, so a permission change takes effect on the next
board-resource read.

Resource update notifications are cues. Consumers must still call
`resources/read`; a URI carried by a notification is not evidence that the read
succeeded. Current-protocol clients negotiate `subscriptions/listen`. The legacy
Zed relay advertises resources without falsely advertising legacy per-resource
subscriptions or list-changed notifications.

## Prompts

The existing `board`, `create`, `watch`, `evidence`, `answer`, and `setup` prompt
names remain compatible. Their copy is host-neutral and identifies the selected
role profile when known. A prompt prepares the next step: it does not create,
claim, answer, approve, merge, publish, provision, or otherwise mutate state
without a separately authorized tool call.

Hosts without MCP resource or prompt support retain the short tool descriptions
and the [client connection guide](../guides/connecting-clients.md). Context remains
server-side request infrastructure. No caller header, prompt argument, URI, or
resource body is treated as identity or authorization.

## Compatibility and rollback

| Client path | Resources | Prompts | Fallback |
| --- | --- | --- | --- |
| MCP `2026-07-28` | List/read plus `subscriptions/listen` negotiation | List/render | Equivalent authorized tools and this guide |
| Legacy `2025-11-25` Zed stdio relay | List/read; no false legacy subscribe claim | Existing names list/render | Equivalent authorized tools and Zed guide |
| Tool-only host | Unsupported | Unsupported | Tool descriptions and canonical guides |
| Wait bridge without orchestrator engine | Explicit unavailable digest | Not applicable | `board_digest` tool after configuring the supported mode |

Rollback can remove the additive `pursers://` registrations and restore the old
prompt wording. Existing tools and `board://{board_id}/digest` remain unchanged;
there is no stored-data migration. Old clients ignore resources they do not know
how to discover.

## Payload measurement

Measured as compact UTF-8 JSON or UTF-8 prompt text from the implementation,
without claiming model-token savings:

| Payload | Baseline | This implementation |
| --- | ---: | ---: |
| Resource/template listing | 0 resources, 0 templates | 1,438 bytes for one static URI and four templates |
| Help index body | unavailable | 1,740 bytes |
| Representative lazy path: index + worker role + watch workflow | unavailable | 2,584 bytes |
| Every static role and workflow body | unavailable | 4,116 bytes, not preloaded |
| Board prompt for `measure-board` | 282 bytes | 365 bytes with role and mutation boundary |

The test suite caps the index at 2,048 bytes, the representative lazy path at
3,000 bytes, and the complete static body set at 5,000 bytes. Dynamic board and
ticket bodies are read only when requested and are excluded from initial discovery.
