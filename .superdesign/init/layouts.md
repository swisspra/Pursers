# Layouts — Pursers Dashboard Surfaces

Shared layout components that appear on every page or across multiple pages.

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Surface 1: Dashboard-UI (Personal Board)

Source: `tools/dashboard-ui/src/dashboard.css` + `tools/dashboard-ui/dashboard-entry.html`

### App Shell

The root layout wrapper. Contains header, connection banner, command bar (search), view tabs, main content, and footer.

```
.app-shell
├── header.app-header
│   ├── .brand-block (product-mark, eyebrow, labels, h1, board-id)
│   └── .header-actions (source text, refresh button)
├── section.connection-banner
├── section.command-bar (global search)
├── nav.view-tabs (6 tab buttons)
├── main#main-content
│   ├── section#search-results (hidden)
│   ├── section#view-today
│   ├── section#view-work (hidden)
│   ├── section#view-agents (hidden)
│   ├── section#view-fleet (hidden)
│   ├── section#view-links (hidden)
│   └── section#view-activity (hidden)
└── footer.app-footer
```

Representative layout declarations follow. The literal 619-line stylesheet and 145-line HTML shell are preserved at `raw/tools/dashboard-ui/src/dashboard.css` and `raw/tools/dashboard-ui/dashboard-entry.html`.

```css
.app-shell { width: min(1240px, 100%); margin: 0 auto; }

.app-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 4px 2px 22px;
}

.view-tabs { display: flex; gap: 6px; margin-bottom: 18px; overflow-x: auto; padding: 2px; scrollbar-width: thin; }
.view-tabs button { min-width: 96px; padding: 9px 16px; }

.hero-grid { display: grid; grid-template-columns: minmax(280px, 1.25fr) minmax(0, 2fr); gap: 12px; margin-bottom: 12px; }
.today-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.span-two { grid-column: span 2; }
.metrics-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 0; }
.work-groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.agents-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); }

.app-footer { display: flex; justify-content: space-between; gap: 16px; margin-top: 22px; padding: 14px 2px 2px; color: var(--text-muted); font-size: 11px; }

@media (max-width: 900px) {
  .app-header { flex-direction: column; }
  .header-actions { width: 100%; justify-content: space-between; }
  .hero-grid { grid-template-columns: 1fr; }
  .today-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 620px) {
  .today-grid, .work-groups { grid-template-columns: 1fr; }
  .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .app-footer { flex-direction: column; gap: 4px; }
}
```

### Skip Link

```css
.skip-link {
  position: fixed; z-index: 100;
  top: calc(8px + var(--safe-top)); left: calc(8px + var(--safe-left));
  padding: 10px 14px; border-radius: 8px;
  background: var(--text); color: var(--surface);
  transform: translateY(-160%);
}
.skip-link:focus { transform: translateY(0); }
```

## Surface 2: Fleet Dashboard

Source: `tools/fleet-dashboard/fleet_dashboard.py` (7507 lines). The HTML definition and patches occupy lines 5828–6427; `do_GET` occupies lines 6580–6883 and `do_POST` occupies lines 6885–7284. The exact design-facing HTML range is preserved at `excerpts/fleet-dashboard-py.txt`. Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.

### App Shell with Sidebar

```
body
├── aside.sidebar
│   ├── a.brand (brand-mark "P" + "Pursers Fleet")
│   ├── nav.primary-nav (Overview, Boards, Agents, Operations)
│   └── .sidebar-foot
├── main
│   ├── #connection-banner
│   ├── .top (h1, view-controls, search, #state)
│   ├── #home-view (central sections)
│   ├── #detail-view (board detail / config / overhead)
│   └── dialog#help-overlay
└── (dynamically rendered content)
```

Representative sidebar declarations:

```css
.app-shell { /* wraps sidebar + main */ }
.sidebar { aria-label: "Primary navigation"; }
.brand { display: flex; align-items: center; gap: 8px; }
.brand-mark { /* CSS-generated "P" logo */ }
.primary-nav { display: flex; flex-direction: column; gap: 4px; }
.primary-nav a { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px; }
.primary-nav a.active { background: var(--panel2); }
.nav-icon { font-size: 16px; }
```

### Fleet Dashboard Top Bar

```css
.top { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; align-items: end; }
.view-controls { display: flex; gap: 8px; }
.search-wrap { position: relative; }
main { width: 100%; max-width: 1500px; min-width: 0; margin: auto; padding: var(--main-pad); }
```

## Surface 3: Extension Join/Settings

Source: `tools/aionui-extension/webui/index.html` + `style.css`. Literal copies are under `raw/tools/aionui-extension/webui/`.

### Extension Layout

```
body
└── main (max-width: 42rem, margin: 0 auto, padding: 2rem)
    ├── h1 "Pursers"
    ├── p.intro
    ├── form#join-form (label, input, button)
    ├── p#message
    └── section#status-card (hidden, dl with dt/dd pairs)
```

Representative layout declarations:

```css
main { max-width: 42rem; margin: 0 auto; padding: 2rem; }
form { display: grid; gap: 0.75rem; margin: 2rem 0; }
section { margin-top: 2rem; padding: 1.25rem; border: 1px solid #334155; border-radius: 0.75rem; background: #1e293b; }
dl { display: grid; grid-template-columns: minmax(7rem, 1fr) minmax(12rem, 2fr); gap: 0.6rem 1rem; }
```
