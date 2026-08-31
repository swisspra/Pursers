# Cache-friendly prompt and response layout

Keep repeated context byte-identical and in the same order. A canonical request
uses this sequence:

1. **Static directive** — tool definitions, safety rules, role, output contract.
2. **Semi-static board rules** — review policy, scope, project registry guidance.
3. **Dynamic ticket tail** — cursors, counts, events, ticket body, user input.

Do not move a timestamp, cursor, status count, ticket body, or generated summary
ahead of reusable instructions. Keep stable section names and field order, sort
values sourced from sets or maps, and append dynamic history rather than
rewriting the prefix. If a static rule changes, version it deliberately; cosmetic
rewrites invalidate the same cache prefix as semantic changes.

## Response layout

Rendered briefings put the board identifier and review policy before current
counts. Structured catch-up responses put request bounds before the event tail.
Ticket bodies remain dynamic and are fetched separately; they do not belong in a
worker directive.

JSON object order is not a wire-level semantic guarantee, but the server emits a
fixed order so clients that preserve insertion order receive a stable prefix.
Unordered collections must be sorted before serialization. Tests should serialize
without key sorting, repeat the same request against unchanged state, and compare
bytes through the first dynamic field.

Worker and reviewer directives should contain only reusable instructions. Do not
inject per-poll state into those files or rewrite compliant directives merely to
change wording.

## Provider adapters

Caching controls belong in the model client adapter, not in directive prose.

| Provider | Current behavior | Adapter responsibility |
| --- | --- | --- |
| OpenAI | Prompt caching is automatic for supported models. Cache hits require an exact prefix; place static content first. Current GPT-5.6 models can cache prefixes from 1,024 tokens, while older supported models may require 2,048. | Preserve message and tool order. Configure `prompt_cache_options` or a cache key only when the client needs explicit routing or retention behavior. |
| Anthropic | Prompt caching supports automatic caching and explicit `cache_control` breakpoints. The default TTL is 5 minutes and an optional 1-hour TTL is available. Minimum cacheable block size varies by model and platform. | Put stable `tools`, then `system`, then reusable `messages` content first. Set breakpoints and TTL in the Anthropic request adapter; consult the current model matrix for minimum sizes. |
| Google Gemini | Gemini 2.5 and later support implicit caching. Explicit caching uses a `CachedContent` resource as a prefix; its configurable TTL defaults to 1 hour. Minimum input sizes vary by model. | Choose implicit or explicit caching in the Gemini client. Create, reference, renew, and delete `CachedContent` resources there, never in generated prose. |
| DeepSeek | Disk-based prefix caching is automatic and best-effort for overlapping request prefixes. Cache entries may be evicted. | Keep the request prefix stable and monitor cache-hit usage fields. No cache instruction belongs in the prompt. |

Official references:

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Gemini context caching](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/)

## Verification checklist

- Serialize the same board state twice without `sort_keys`; the bytes match.
- Change only dynamic state; bytes before the first dynamic field still match.
- Run under different hash seeds; semantic event field order stays fixed.
- Confirm no provider-specific cache controls were added to directive prose.
- Treat hit-rate telemetry as operational evidence; stable layout alone does not
  prove a provider cache hit.
