# Current Primary Dashboard Contract

This is the bounded contract for faithfully reproducing the current primary dashboard before redesign.

## Source provenance

| Source | Lines | SHA-256 | Use |
| --- | ---: | --- | --- |
| `tools/dashboard-ui/dashboard-entry.html` | 145 | `cb640ae68837d02d3d0eb2843c4c31e9c8d563351264d0c3a7c68699e16500e7` | Exact semantic shell and visible copy |
| `tools/dashboard-ui/src/dashboard.css` | 619 | `9d3b71edd4e0149337da441e769deeddb44c87d058d94455e3921ce0907f0fbd` | Exact responsive styling and tokens |
| `tools/dashboard-ui/src/dashboard.ts` | 1528 | `bce74539deb910fa4c74b735cc3ab61e9a37fb75c4489cffc334cba7dc19fdbb` | Bounded render ranges below |
| `packages/personal/src/pursers_personal/resources/dashboard.html` | 257 | `746c6eccd85afcc38588c8b9e1946ff2c91a7eb8477783c2f0d6bff0f4c6d922` | Built live MCP Apps entrypoint; not uploaded because it is 404280 bytes |

Dependency snapshot: `codex/TK-f8a62bab8d05-resubmit-1@dfa4fa535bbc9aa665f0d8856971472651659af5`.

## Exact render path

The live MCP Apps HTML is built from `tools/dashboard-ui/dashboard-entry.html`, `tools/dashboard-ui/src/dashboard.ts`, and `tools/dashboard-ui/src/dashboard.css` into `packages/personal/src/pursers_personal/resources/dashboard.html`. The app imports `App`, `PostMessageTransport`, `applyDocumentTheme`, `applyHostFonts`, `applyHostStyleVariables`, and `McpUiHostContext` from `@modelcontextprotocol/ext-apps`.

The primary view is `today`. Its render path is:

1. The semantic shell exposes header, connection status, global search, six tab buttons, `main`, and footer.
2. `renderToday()` computes Open, Working, Submitted, and Agents active metrics.
3. `renderHealth()` selects a truthful health state: demo error, demo, stale/feed error, needs attention, ready for review, work in progress, ready work, or clear.
4. Current work shows at most four non-ended tickets using `ticketRow()`.
5. Agents shows at most four non-stale agents.
6. Latest handoff, important pinned note, and five newest activity events complete the Today grid.
7. `selectView()` manages `aria-selected`, roving tab index, panel visibility, focus, and lazy Fleet/Links refresh.
8. `applyHostContext()` applies AionUI theme, fonts, CSS variables, and safe-area insets.

## Existing desktop composition

```
App shell (max-width 1240px)
├── Header: OB mark + product labels + board name + source + Refresh
├── Connection banner
├── Global search
├── Today / Work / Agents / Fleet / Links / Activity tabs
├── Today
│   ├── Hero: Project health + four metrics
│   └── Three-column grid
│       ├── Current work (two columns)
│       ├── Agents
│       ├── Latest handoff
│       ├── Important pinned note
│       └── Recent activity
└── Footer: product + Local / Read-only notice
```

At 900px the header and hero stack and the Today grid becomes two columns. At 620px all content grids become one column and metrics become two columns.

## Faithful baseline requirements

- Reproduce the dark navy Personal Preview surface before proposing a redesign.
- Preserve all exact section names, six tabs, connection banner, search, footer, status meanings, card hierarchy, responsive breakpoints, and accessibility affordances.
- Show realistic synthetic data only, with Demo and Read-only labels visible.
- Do not add the proposed Home/Projects/Approvals/Settings navigation to the faithful baseline.
- Do not add actions that the current read-only dashboard does not support.
- Use CSS-generated `OB`; no licensed image or logo asset exists in the repository.

## Uploaded context boundary

Pass this contract, the six `.superdesign/init/*.md` files, `.superdesign/design-system.md`, the 145-line HTML shell, and the 619-line CSS. Do not upload the 404280-byte built HTML or the full 1528-line TypeScript module.

