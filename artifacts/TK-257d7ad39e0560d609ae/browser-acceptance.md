# Display-name browser acceptance

Acceptance ran against the repository's display-ready Fleet fixture in one
Ego Browser task space. The fixture uses synthetic identities and a loopback
server only.

## Observations

- Saving `ทีมเหนือ 🌏` changed the visible heading while preserving
  `Operational name synthetic-worker-01` and advanced revision `0` to `1`.
- A concurrent update advanced revision `1` to `2`. Submitting the still-open
  revision `1` form kept the local input and rendered
  `Save failed: display-name revision conflict: expected 1, current 2`.
- Pressing Escape restored the canonical value and rendered
  `Changes canceled.`.
- Reset changed the heading back to `synthetic-worker-01`, removed the alias,
  and advanced revision `2` to `3`.
- Assigning `<img src=x onerror=alert(1)>` to two synthetic identities rendered
  two literal headings, zero injected `img` elements, both operational names,
  the `fixture-board` disambiguator, and stable IDs `AI-synthetic-01` and
  `AI-synthetic-02`.

## Captures

- `revision-conflict.png` — board-scoped editor after the concurrent update.
- `xss-duplicate-display-names.png` — duplicate literal labels with operational
  names and board context.
