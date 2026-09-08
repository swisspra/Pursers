# Theme — Pursers Dashboard Surfaces

## Part 1 — Compact Token Summary

### Dashboard-UI (Personal Board)

**Color palette (dark, default):**

| Token | Value | Purpose |
|-------|-------|---------|
| `--surface` | `#09111f` | Page background |
| `--surface-soft` | `#111c2d` | Card background, input background |
| `--surface-raised` | `#17253a` | Raised surfaces |
| `--surface-hover` | `#1c2d45` | Hover state |
| `--text` | `#f3f7fb` | Primary text |
| `--text-soft` | `#b5c2d2` | Secondary text |
| `--text-muted` | `#8291a5` | Tertiary text, labels |
| `--border` | `#2a3b52` | Default border |
| `--border-strong` | `#3d536f` | Hover/focus border |
| `--accent` | `#67d9ff` | Primary accent (cyan) |
| `--success` | `#6ee7ad` | Success state |
| `--warning` | `#ffd166` | Warning state |
| `--danger` | `#ff8792` | Error/danger state |
| `--focus` | `#a5e9ff` | Focus ring |
| `--body-accent` | `rgba(65,165,214,0.16)` | Background radial gradient |
| `--card-background` | `linear-gradient(155deg, rgba(23,37,58,0.94), rgba(13,25,42,0.96))` | Card gradient |
| `--inset-background` | `rgba(9,18,32,0.46)` | Inset/nested background |

**Color palette (light, via `prefers-color-scheme: light` and `[data-theme="light"]`):**

| Token | Value |
|-------|-------|
| `--surface` | `#f3f7fb` |
| `--surface-soft` | `#ffffff` |
| `--text` | `#102033` |
| `--text-soft` | `#3e5268` |
| `--text-muted` | `#526276` |
| `--border` | `#cbd8e5` |
| `--accent` | `#006f94` |
| `--success` | `#087a50` |
| `--warning` | `#8a5b00` |
| `--danger` | `#b8273d` |

**Pill tones:**

| Tone | Dark Value | Light Value |
|------|-----------|-------------|
| `working` | `#67d9ff` (accent) | `#005b7a` |
| `submitted`/`live` | `#6ee7ad` (success) | `#055c3d` |
| `attention` | `#ff8792` (danger) | `#8a1530` |
| `warning` | `#ffd166` | `#684400` |
| `neutral` | `#b5c2d2` | `#3e5268` |

**Typography:**
- Font family: `var(--font-sans, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif)`
- Mono: `var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)`
- H1: `clamp(28px, 4vw, 46px)`, `letter-spacing: -0.035em`
- H2: `17px`
- H3: `14px`
- Body: `1.5` line-height
- Eyebrow/kicker: `11px`, `font-weight: 750`, `letter-spacing: 0.11em`, `text-transform: uppercase`

**Spacing:**
- `--space-page: 24px` (default), `18px` (≤900px), `14px` (≤620px)
- `--safe-top/right/bottom/left: 0px` (host-injected safe area insets)

**Border radius:**
- `--radius-lg: var(--border-radius-lg, 18px)` (cards)
- `--radius-md: var(--border-radius-md, 12px)` (insets, list items)

**Shadow:**
- `--shadow: 0 18px 55px rgba(0,0,0,0.22)` (dark), `0 16px 42px rgba(28,51,76,0.1)` (light)

**Breakpoints:**
- `900px` — header stacks, hero-grid → 1 column, today-grid → 2 columns
- `620px` — all grids → 1 column, metrics → 2 columns, footer stacks

**Accessibility:**
- `@media (prefers-reduced-motion: reduce)` — disables transitions/animations
- `@media (forced-colors: active)` — forces CanvasText borders
- Min button height: `44px`
- Skip link for keyboard navigation

### Fleet Dashboard

**Color palette (dark, default):**

| Token | Value |
|-------|-------|
| `--bg` | `#0b1020` |
| `--panel` | `#151b2d` |
| `--panel2` | `#202942` |
| `--line` | `#29324a` |
| `--text` | `#e7ecf7` |
| `--muted` | `#9aa6bf` |
| `--good` | `#46d39a` |
| `--warn` | `#f4bd55` |
| `--bad` | `#ef6f7d` |
| `--accent` | `#79a8ff` |

**Color palette (light, `[data-theme="light"]`):**

| Token | Value |
|-------|-------|
| `--bg` | `#f5f7fb` |
| `--panel` | `#fff` |
| `--panel2` | `#e9eef7` |
| `--line` | `#ccd4e2` |
| `--text` | `#182033` |
| `--muted` | `#5f6c82` |
| `--good` | `#167a55` |
| `--warn` | `#8b5b00` |
| `--bad` | `#b42332` |
| `--accent` | `#245fcc` |

**Typography:**
- Font: `14px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif`
- Mono: `ui-monospace, SFMono-Regular, monospace`
- H1: `24px`

**Density:**
- `[data-density="compact"]`: `--cell-y: 4px; --card-pad: 10px; --main-pad: 16px;`
- Default: `--cell-y: 8px; --card-pad: 14px; --main-pad: 24px;`

**Breakpoints:**
- `800px` — strips → 2 cols, grids → 1 col, hide-small hidden, agent summary → 2 cols

**Print:**
- `@media print` forces light theme with high-contrast colors

### Extension

Fixed dark theme (no light mode):

| Token | Value |
|-------|-------|
| Background | `#111827` |
| Text | `#f8fafc` |
| Border | `#475569` / `#334155` |
| Accent button | `#0ea5e9` (background), `#082f49` (text) |
| Muted | `#cbd5e1` / `#94a3b8` |
| Section bg | `#1e293b` |

## Part 2 — Exact source references

### Dashboard-UI: `dashboard.css`

Source: `tools/dashboard-ui/src/dashboard.css` (619 lines, 1–619). Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`. Its line-exact literal copy is `raw/tools/dashboard-ui/src/dashboard.css`; the block below is only the root-token excerpt.

Key raw `:root` (dark default):

```css
:root {
  color-scheme: light dark;
  font-family: var(--font-sans, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
  --safe-top: 0px; --safe-right: 0px; --safe-bottom: 0px; --safe-left: 0px;
  --space-page: 24px;
  --surface: var(--color-background-primary, #09111f);
  --surface-soft: var(--color-background-secondary, #111c2d);
  --surface-raised: var(--color-background-tertiary, #17253a);
  --surface-hover: #1c2d45;
  --text: var(--color-text-primary, #f3f7fb);
  --text-soft: var(--color-text-secondary, #b5c2d2);
  --text-muted: var(--color-text-tertiary, #8291a5);
  --border: var(--color-border-primary, #2a3b52);
  --border-strong: #3d536f;
  --accent: var(--color-accent-primary, #67d9ff);
  --accent-soft: rgba(103, 217, 255, 0.12);
  --success: var(--color-text-success, #6ee7ad);
  --warning: var(--color-text-warning, #ffd166);
  --danger: var(--color-text-danger, #ff8792);
  --focus: #a5e9ff;
  --pill-background: #1c2d45; --pill-neutral: #b5c2d2;
  --pill-working: #67d9ff; --pill-submitted: #6ee7ad;
  --pill-warning: #ffd166; --pill-attention: #ff8792;
  --shadow: 0 18px 55px rgba(0, 0, 0, 0.22);
  --body-accent: rgba(65, 165, 214, 0.16);
  --card-background: linear-gradient(155deg, rgba(23, 37, 58, 0.94), rgba(13, 25, 42, 0.96));
  --inset-background: rgba(9, 18, 32, 0.46);
  --radius-lg: var(--border-radius-lg, 18px);
  --radius-md: var(--border-radius-md, 12px);
  background: var(--surface);
}
```

Host theme overrides: The dashboard-ui accepts host CSS variables via `applyHostStyleVariables()` and `applyHostFonts()` from `McpUiHostContext`. Variables like `--color-background-primary`, `--font-sans`, `--font-mono`, `--border-radius-lg`, `--border-radius-md` are injected by the AionUI host.

### Fleet Dashboard: inline CSS

Source: `tools/fleet-dashboard/fleet_dashboard.py` line 5831. Exact surrounding source is in `excerpts/fleet-dashboard-py.txt`. Root-token excerpt:

```css
:root{color-scheme:dark;--bg:#0b1020;--panel:#151b2d;--panel2:#202942;--line:#29324a;--text:#e7ecf7;--muted:#9aa6bf;--good:#46d39a;--warn:#f4bd55;--bad:#ef6f7d;--accent:#79a8ff;--cell-y:8px;--card-pad:14px;--main-pad:24px}
:root[data-theme="light"]{color-scheme:light;--bg:#f5f7fb;--panel:#fff;--panel2:#e9eef7;--line:#ccd4e2;--text:#182033;--muted:#5f6c82;--good:#167a55;--warn:#8b5b00;--bad:#b42332;--accent:#245fcc}
:root[data-density="compact"]{--cell-y:4px;--card-pad:10px;--main-pad:16px}
```

### Extension: `style.css`

Source: `tools/aionui-extension/webui/style.css` (66 lines, fixed dark theme, no CSS custom properties). Its byte-exact literal copy is `raw/tools/aionui-extension/webui/style.css`.
