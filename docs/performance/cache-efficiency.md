# Prompt-cache efficiency: measured numbers

Pursers is designed so that agents re-send the same bytes in the same order:
static rules first, board rules next, and the changing ticket state last. It
also sends compact mutation receipts, and waiting seats make no model calls
until there is work. The design is described in
[cache-friendly-prose.md](../cache-friendly-prose.md), and tests check it by
repeating requests against unchanged state and comparing bytes.

These are the numbers observed while the fleet built Pursers itself. They were
read from the model providers' usage dashboards and provided by the maintainer.
The cache share is `cached input ÷ (cached input + uncached input)`.

## Codex worker and reviewer fleet (alpha phase, August 2026)

| Day | Output | Uncached input | Cached input | Total | Cache share |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-08-13 | 1,016,483 | 7,044,509 | 286,040,576 | 294,101,568 | 97.6% |
| 2026-08-14 | 823,821 | 5,307,969 | 204,087,808 | 210,219,598 | 97.5% |
| 2026-08-15 (dashboard A) | 1,397,971 | 8,792,696 | 283,205,888 | 293,396,555 | 97.0% |
| 2026-08-15 (dashboard B) | 4,545,248 | 27,415,979 | 966,328,960 | 998,290,187 | 97.2% |

Dashboard B reported about 2B tokens for the seven days ending 2026-08-16. The
two 2026-08-15 rows come from different dashboards, so they are listed
separately rather than added together.

## Operator seat that shipped 5.0.0 (Claude, September 2026)

One session of the operator seat that merged, gated, tagged and published
5.0.0:

| Input (uncached) | Output | Cache read | Cache write | Cache share |
| ---: | ---: | ---: | ---: | ---: |
| 372 | 43.6k | 99.9M | 1.2M | 98.8% |

The share here is `cache read ÷ (cache read + cache write + uncached input)`.
The provider's own panel showed 99%.

## Caveats

- These are observations from one team's fleet, not a benchmark. Your share
  depends on your host, model, and prompts.
- Hosts differ in how they cache. The board keeps its responses stable, but a
  host that rewrites its own system prompt on every turn will not benefit.
