# Pursers Home Design System

## Product promise

Pursers is an approachable home inside AionUI for delegating work to a Team, understanding progress, resolving approvals, and receiving reviewed results. It must feel calm and understandable to ordinary users without hiding truthful operational state.

## Non-negotiable product behavior

- Use plain English first and keep all copy ready for localization.
- Preserve every current capability through direct navigation or progressive disclosure.
- Keep WORK and PERSONAL contexts visibly distinct.
- Model AionUI Team accurately: one folder per teammate, a monitor-only lead, and board-driven dispatch. Do not suggest ACP, agent forks, or automatic scaling.
- Show uncertainty, staleness, permission limits, partial completion, and disconnected states explicitly.
- Keep raw tokens, bridge configuration, diagnostics, and destructive operations behind Settings or advanced disclosure.
- User pause and stop actions must propagate across the Team and expose their current status.
- Do not invent APIs or capabilities absent from the supplied route and journey context.

## Information architecture

Primary navigation: Home, Projects, Work, Team, Approvals, Activity, Settings.

- Home: next action, project health, current work, active teammates, recent result, and important decision.
- Projects: board registry and project switching, including empty/disconnected states.
- Work: ticket list, ticket detail, status, ownership, review readiness, and recovery.
- Team: teammate roster, monitor-only lead, assignment state, availability, and paused/stopped state.
- Approvals: intake, human requests, review decisions, door rotation, and other explicit confirmations.
- Activity: bounded event history with scope and truncation labels.
- Settings: connection, seats, dispatch policy, bridge/release health, diagnostics, and advanced operations.

## Visual foundations

- Let the active prompt choose the visual direction. The faithful baseline must retain the current dark Personal Preview appearance; redesign branches may replace it.
- Use host-provided typography and color variables when embedded in AionUI.
- Prefer a clear hierarchy, generous whitespace, rounded but restrained surfaces, and readable status language over dense control-plane styling.
- Pair every semantic color with text or an icon. Never communicate state by color alone.
- Preserve the existing success, warning, danger, accent, muted, border, and focus semantics.
- Use one consistent shell and navigation model across Personal, Fleet, and extension surfaces.

## Interaction and accessibility

- Full keyboard navigation with visible focus, logical tab order, and a skip link.
- Minimum 44px interactive targets.
- WCAG AA contrast for normal text and controls.
- Respect reduced motion and forced colors.
- Announce loading, connection, copy, approval, and completion changes through appropriate live regions.
- Preserve user context after refresh, error recovery, and responsive layout changes.

## Responsive behavior

- Desktop: persistent primary navigation with a broad main workspace.
- Tablet: compact navigation and two-column summaries where space allows.
- Mobile: single-column content, bottom or compact navigation, no horizontal page scrolling, and advanced tables transformed into cards or focused detail views.
- Never hide critical approval, interruption, or recovery actions at narrow widths.

## Data and security presentation

- Treat all dashboard content as authorized, bounded projections rather than complete audit history.
- Never show bearer tokens, door secrets, raw credentials, or private identifiers.
- Labels such as Demo, Stale, Read-only, Truncated, Permission denied, and Local must remain explicit.
- Destructive or high-impact operations require clear scope, consequence, and confirmation.

