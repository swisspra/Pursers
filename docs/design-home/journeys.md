# Pursers Home journeys and dashboard coverage

## Status and scope

This document defines the target product journey and information architecture for a beginner-friendly Pursers experience inside AionUI. It is a behavior and content contract, not a visual design. It does not select colors, typography, spacing, icons, or component styling.

The current Fleet Dashboard is the capability baseline. A target behavior in this document is not evidence that the behavior is already implemented. Where the target depends on an extension host API, Team lifecycle API, or door connection flow that is not yet available, the interface must show the gap rather than simulate success.

The primary outcome is:

> Install the extension, connect one door, start a Team, describe the work, follow progress, answer an approval, and receive a reviewed result without editing configuration files.

Advanced diagnostics, coordinator policy, release controls, credential references, protocol pressure, and token plumbing belong in Settings. WORK and PERSONAL remain separate trust domains and must never be blended into one project, Team, activity stream, or approval queue.

AionUI Team is authoritative for the target Team experience. The redesign must not revive a legacy Goose CLI flow as an alternative product model.

## Plain-language model

| Product term | Meaning for an ordinary user | Technical terms kept out of the default path |
| --- | --- | --- |
| Home | The next useful action and anything that needs the user's attention | central, registry, board snapshot, catchup cursor |
| Project | A connected place where work happens | board ID, work directory, integration ref |
| Work | One request and its reviewed result | ticket, claim, lease, submission |
| Team | The agents available for a project and their current work | seat, principal, capability projection, dispatcher |
| Approval | A question that requires a human decision | `needs_human`, requested schema, disposition |
| Activity | A bounded history of work, decisions, review, and recovery | journal, sequence number, route provenance |
| Settings | Connection, Team setup, diagnostics, policy, and operations | door, JWT, JWKS, bridge, coordinator config |

Technical identifiers remain available in details, copy actions, diagnostics, and support messages. They do not replace the human title or plain-language state.

## Shared information architecture

The target primary navigation is:

1. **Home** — next action, connection health, pending approvals, active Team, and recent results.
2. **Projects** — connected projects, project state, add project, and project-level entry points.
3. **Work** — active and recent work, work detail, new work, review state, and result delivery.
4. **Team** — Team members, roles, availability, current assignments, and lifecycle controls.
5. **Approvals** — every unresolved human request and intake draft decision.
6. **Activity** — chronological events, changes, flow, provenance routes, and bounded-history notices.
7. **Settings** — doors, seats, Doctor, dispatch policy, coordinator policy, overhead, registry clones, and release operations.

On narrow screens, keep Home, Projects, Work, Team, and Approvals directly reachable. Activity and Settings may move into a labeled **More** menu. They must not disappear or become icon-only.

## Two Home alternatives

Both alternatives use the same underlying capabilities and navigation. The design direction may choose either without changing the coverage boundary.

### Alternative A — next-action Home

Use this when the product should feel like a guided assistant rather than an operations console.

Order:

1. **Next step** — one primary action based on state.
2. **Waiting for you** — approvals and blocked questions.
3. **In progress** — active work with Team state and latest meaningful event.
4. **Recent results** — reviewed outcomes ready to open.
5. **Project health** — compact connection and recovery summary.

Example primary actions:

- No project: **Connect your first project**
- Project connected, no Team: **Start a Team**
- Team ready, no work: **Describe the work**
- Work active: **View progress**
- Approval pending: **Answer 1 question**
- Reviewed result ready: **Open reviewed result**
- Connection unhealthy: **Reconnect project**

Safe default: choose Alternative A for first-run and single-project users because it keeps one clear action above operational metrics.

### Alternative B — project Home

Use this when returning users commonly manage several projects and need orientation before action.

Order:

1. **Your projects** — project cards with connection, Team, active work, and approval counts.
2. **Selected project** — one compact status panel and primary action.
3. **Waiting for you** — approvals across the selected trust domain.
4. **In progress** — active work grouped by project.
5. **Recent activity** — reviewed results and important recovery events.

Each project card answers four questions without opening details:

- Is it connected?
- Is a Team ready?
- Is work running?
- Does the user need to act?

Safe default: keep the most recently used project selected. Do not rank WORK and PERSONAL projects together; switch trust domains explicitly before showing their projects.

### Coverage boundary for both alternatives

Both alternatives must include or link to:

- connection state and last successful update;
- every pending human approval;
- active and partially started Teams;
- active work, review state, and reviewed results;
- empty, disconnected, expired-door, permission-denied, paused, and partial-start recovery;
- Projects, Work, Team, Approvals, Activity, and Settings;
- explicit bounded or stale-data notices when the current APIs cannot provide a complete view.

Neither alternative puts raw token paths, bridge versions, dispatch timing, coordinator thresholds, release actions, or protocol overhead on Home. A compact warning may link to the relevant Settings section.

## Primary beginner journey

### 1. Install and open

Target behavior:

- AionUI confirms that Pursers is installed and opens Home.
- Home detects whether a project connection already exists.
- The user is not asked for a board ID, token file, JWKS path, bridge command, or host configuration path.

Copy:

- Heading: **Bring a project home**
- Body: **Connect a project, start a Team, and describe what you need. Pursers will show progress and ask before it needs a decision.**
- Action: **Connect a project**

Acceptance:

- A first-time user can identify the first action without documentation.
- If AionUI cannot provide a required host API, the page names the missing capability and links to supported setup; it does not display a fake completion state.

### 2. Connect one door

Target behavior:

- The user pastes or chooses one `prs1.…` door string in a protected connection flow.
- The product explains what the door connects in project language before confirmation.
- The door value is not repeated in activity, notifications, logs, support copy, or listing views.
- Success returns to the connected project, not to credential settings.

Copy:

- Heading: **Connect this project**
- Field: **Door string**
- Help: **A door lets this Pursers Team join one project. You can replace it later in Settings.**
- Action: **Connect project**
- Success: **Project connected. Your door was used securely and will not be shown here again.**

Acceptance:

- The default flow requires no manual configuration edit.
- The user sees the project name and trust domain before confirming.
- A WORK door cannot be attached to PERSONAL, or the reverse.
- Invalid, expired, rotated, or unauthorized doors produce different recovery messages.

### 3. Start a Team

Target behavior:

- The project recommends a minimal Team based on available, actually supported roles.
- The user can inspect member purpose in plain language before starting.
- Start reports each member independently; partial success is not collapsed into either “started” or “failed.”

Copy:

- Heading: **Start a Team for this project**
- Worker: **Does the work**
- Reviewer: **Checks the result independently**
- Action: **Start Team**
- Partial result: **1 of 2 Team members started. Your work has not started yet.**

Acceptance:

- Unsupported roles are unavailable with a reason.
- A reviewer is never described as running work.
- Each Team member uses its own workspace folder; members do not share a mutable working folder.
- The lead monitors and coordinates but does not execute or review the assigned work.
- The board dispatches assignments; the lead does not manually bypass dispatch.
- Start can be retried only for failed members.
- The Team is “Ready” only after the minimum required members are confirmed available.

### 4. Describe the work

Target behavior:

- The user enters one concrete request in ordinary language.
- Pursers shows the drafted work title and category before creation when approval is required.
- The user can approve, edit the title, or decline.
- Rate limits and project availability are explained before data is lost.

Copy:

- Heading: **What should the Team do?**
- Placeholder: **Describe one result you want from this project**
- Action: **Prepare work**
- Draft prompt: **Review the title and category before the Team starts.**
- Waiting state: **Preparing a clear work item…**

Acceptance:

- The input remains available after recoverable errors.
- The current 5–500 character and 10 asks/hour limits are visible when they apply.
- “Approved” means creation is authorized, not that work is complete.
- A consumed intake item is not presented as pending.

### 5. Follow progress

Target behavior:

- Work detail shows the request, plain-language state, assigned Team member, last meaningful event, required deliverables, annotations, review state, and current blocker.
- The default view summarizes progress. Raw events, sequence numbers, route provenance, and bounded-history details live under Activity or diagnostics.

Plain-language states:

| Technical states | Default label |
| --- | --- |
| open, offered | Finding the right Team member |
| claimed, in progress, creating report | In progress |
| submitted, reviewing, in review | Being reviewed |
| rejected or reopened after review | Changes requested |
| `needs_human` | Waiting for you |
| closed with approval | Reviewed and complete |
| paused | Paused |
| cancellation requested | Stopping |

Copy:

- **In progress · The Team is working on your request.**
- **Being reviewed · An independent reviewer is checking the result.**
- **Changes requested · The Team is addressing review feedback.**

Acceptance:

- Status is never communicated by color alone.
- The last-update time and stale-data state are visible.
- Bounded responses say that older or omitted items may exist; zero visible rows must not imply zero total rows.
- Refresh pauses while the user edits a form and clearly offers **Resume updates**.

### 6. Answer an approval

Target behavior:

- The same approval appears on Home and Approvals without duplicating the underlying request.
- The prompt identifies the project, work item, asking Team member, consequence, and safe choices.
- Supported schemas render human forms for strings, numbers, booleans, single-choice, and multi-choice values.
- Secret or credential requests never render an inline form; they require a trusted external handoff.

Copy:

- Heading: **The Team needs your answer**
- Context: **This answer will resume “Prepare the release notes.”**
- Accept action: **Send answer and continue**
- Decline action: **Decline request**
- Recovery choices: **Resume work**, **Keep paused**, or **Cancel work**

Acceptance:

- Required fields and minimum selections are announced before submission.
- External links open only after explicit user action and show the destination host.
- Keyboard focus returns to the updated work item after the response succeeds.
- A failure preserves the entered answer and explains whether it was sent.

### 7. Receive a reviewed result

Target behavior:

- Completion is shown only after independent review approval.
- The result view leads with the outcome, changed artifacts, validation, and limitations.
- Technical submission history remains available as supporting detail.

Copy:

- Heading: **Reviewed and ready**
- Body: **The Team completed the work and an independent reviewer approved it.**
- Actions: **Open result**, **View validation**, **View activity**

Acceptance:

- Submitted work is never labeled complete before approval.
- Rejected work remains “Changes requested,” with the latest reviewer direction visible.
- The result identifies its project and trust domain.

## State and recovery journeys

### Returning user

Home shows, in order: pending approvals, active work, changes requested, reviewed results not yet opened, and project health. If nothing needs action, the primary action is **Describe new work** for the most recently used project.

### Empty project

An empty project is a valid state, not an error.

- No Team: **This project is connected. Start a Team when you are ready.**
- Team ready, no work: **Your Team is ready. Describe the first result you want.**
- No visible tickets in a bounded response: **No work is shown in this view. Older work may exist.**

### Disconnected board

Home and Projects preserve the last known project name and state, label it **Disconnected**, show when data was last updated, and offer **Reconnect project**. Other healthy projects remain usable. The interface does not interpret an unavailable central as an empty project.

### Expired or rotated door

Label: **Connection needs a new door**.

Explain that the old door can no longer join the project, then offer **Replace door**. Do not request a token path or reveal prior credential material. After replacement, re-check identity and Team availability before reporting recovery.

### Partial Team start

Show one row per requested member with **Ready**, **Starting**, or **Could not start** and a reason. The primary recovery is **Retry failed members**. Work submission remains disabled until the minimum Team is ready, unless the product has an explicit supported reduced-Team mode.

### Permission denial

Stay on the current task and explain:

- what action was denied;
- which project or setting it affects;
- the required role in plain language;
- whether anything changed;
- the safe next action.

Example: **You can view this project, but only a project administrator can rotate its door. Nothing changed.**

### Paused Team

Pause is visible on Home, Projects, and Team. It must propagate to the actual Team lifecycle rather than merely hiding activity in the UI.

Safe target contract:

- **Pause Team** stops new assignments and asks active members to reach a safe checkpoint.
- The UI shows **Pausing** until every reachable member acknowledges.
- A member that cannot be reached remains visible with **No response**.
- **Resume Team** restores assignment only after connection and role checks pass.

If the backend cannot enforce this contract, the control stays unavailable with **Team pause is not supported by this connection**.

### Stop Team or work

Stop must propagate to every affected member. The interface must not report **Stopped** when it has only sent a local UI command.

Safe target contract:

- confirm the scope: one work item or the whole Team;
- send the supported stop or cancellation request to each affected member;
- show **Stopping** until acknowledgements arrive;
- preserve the reviewed history and reason;
- list unreachable members and offer retry or manual recovery.

## Current capability coverage matrix

Nothing in the current dashboard may disappear silently. The target design may rename, regroup, or progressively disclose a capability.

| Current capability | Target location | Required treatment |
| --- | --- | --- |
| Central availability, coordinator heartbeat, online/busy/available/stale counts | Home; Settings > Connections | Plain summary on Home; trust-domain and raw health detail in Settings |
| Open, claimed, submitted, closed-today counts | Home; Projects; Work | Use human labels and link to filtered Work |
| Waiting for you, schema-driven forms, trusted external handoff, accept/decline/disposition | Home; Approvals; Work detail | One underlying request, shown consistently in all entry points |
| Needs attention findings, starved work, lapsed leases, non-acting offers, push failures | Home; Settings > Diagnostics | Human-impact warnings on Home; protocol detail in Settings |
| Acknowledge and Snooze 24h attention state | Home; Activity | Keep durable local state and reveal when a warning is snoozed |
| Board cards and active project registry | Projects | Rename board-facing summary to project-facing summary; retain board ID in details |
| Project Add flow: registry, board, door principals, policy defaults, fleet clone | Projects > Add project | Show the five actual steps and partial failure; never collapse them into premature success |
| Door inventory, connected seats, copy-once door, rotation | Projects > Connection; Settings > Doors | Beginner recovery in Projects; identifiers, expiry, and rotation detail in Settings |
| Ticket table, description, required fields, latest submission summary, review label | Work; Work detail | Preserve all fields with plain-language headings |
| Ticket annotations and coordinator findings | Work detail; Activity | Keep author, kind, time, omitted count, and bounded-state warning |
| Intake ask, drafted title/category, approve/decline, consumed state | Work > New work; Approvals | Preserve current approval semantics, rate limit, and idempotency |
| Ticket Flow columns | Work; Activity > Flow | Keep status columns and unassigned state |
| Timeline grouped by UTC day and work item | Activity > Timeline | Preserve bounded, read-only semantics and event time |
| Changes after sequence or in the last 24 hours | Activity > Changes | Default to human time; sequence filter stays in advanced controls |
| Provenance Routes, per-member totals, rework rate | Activity > Routes; Team member detail | Preserve truncation notes and principal-collision-safe labels |
| Unified agent pool, per-project roles, live claims, last seen | Team | Rename agents as Team members in the default view; preserve exact agent identifiers in details |
| Local API agent creation, provider/model, Keychain storage | Team > Add member; Settings > Providers | Key material remains protected; unsupported reviewer runtime remains explicit |
| Team member Test, Start, Stop, Restart, copied seat command, 20-line logs | Team; Settings > Diagnostics | Common lifecycle actions in Team; commands and logs behind Settings |
| Show active/stale, retire one, retire inert members | Team > Member management | Explain retirement and reactivation before confirmation |
| Context pressure per return/hour, trends, cumulative bridge diagnostics | Settings > Diagnostics > Overhead | Do not expose token plumbing on Home; link from a human-impact warning |
| Coordinator thresholds, intake mode/categories/rate, integration watch | Settings > Coordinator | Preserve sources, concurrency guard, and restart warning |
| Dispatch claim/offer timing, second opinion, fallback broadcast, gaps, offers, history | Settings > Dispatch | Keep all policy and diagnostic fields; no beginner jargon on Home |
| Seat inventory, capabilities, current offers, preview/apply, backups, prompts | Settings > Seats | Preserve exact-change preview and restart state |
| Discovered-seat import and Doctor | Settings > Seats | Preserve conflict review and inventory-only import behavior |
| Bridge install/upgrade, pinned/latest versions, push checks | Settings > Seats > Runtime | Summarize health elsewhere; keep version and token/CA checks here |
| Registry clone status, dirty/ahead/behind, create/fetch clone | Settings > Projects > Work trees | Preserve refusal to overwrite local changes and exact recovery command |
| Release card, CI/PyPI/GitHub/Central state, stage/publish/restart operations | Settings > Release & operations | Keep confirmation plan, digest, audit, rollback, and unavailable states |
| Multi-central independent rendering and errors | Home project switcher; Settings > Connections | Keep trust domains separate; one unavailable domain must not hide another |
| Global search across projects, work, and Team members | Global | Retain keyboard navigation and grouped results |
| Theme, density, print, keyboard shortcuts, table navigation | Global; Settings > Appearance and accessibility | Preserve current preferences and visible shortcut help |
| Five-second refresh, connection banner, edit-aware pause, bounded/truncated data | Global | Always expose stale, paused, unavailable, and bounded states |

## Capability and implementation boundary

Currently evidenced by the Fleet Dashboard:

- read-only fleet, project, work, Team, activity, route, and overhead projections;
- guarded coordinator config and intake writes;
- loopback human-request resolution;
- local API agent lifecycle controls;
- seat, door, project provisioning, dispatch, Doctor, clone, and release-operation surfaces described in the dashboard and its tests;
- multi-central isolation, bounded reads, redaction, and edit-aware refresh behavior.

Required by this target journey but not proven by this document:

- installing and opening Pursers as an AionUI extension;
- accepting a door directly through a supported AionUI host connection API;
- starting a complete worker-and-reviewer Team as one user action;
- project-wide pause, resume, and stop acknowledgement semantics;
- a unified result artifact view inside AionUI.

Until those dependencies exist, the redesign must preserve the current supported path or show a precise unsupported-state message. It must not invent endpoints, claim GUI proof from mocks, or turn a local visual state into a Team lifecycle claim.

## Copy rules

- Lead with the user's goal: **Connect project**, not **Configure board**.
- Use verbs that match actual consequences: **Prepare work**, **Approve and create**, **Send answer and continue**.
- Reserve **Complete** for independently reviewed work.
- Pair every error with unchanged/changed state and one safe next action.
- Show technical identifiers after the human title, never instead of it.
- Avoid “success” when only one step of a multi-step operation succeeded.
- Keep strings ready for localization: no sentence fragments assembled from variable word order, no meaning encoded only in capitalization, and no hard-coded plural grammar.

## Accessibility and responsive acceptance

- Every action is reachable and operable by keyboard.
- Focus order follows the visible task order. Dialogs trap focus, provide a labeled close action, and return focus to their opener.
- After navigation or a successful action, focus moves to the new page heading or the changed item.
- Status changes use an appropriate live region without repeatedly announcing five-second refreshes.
- Labels, instructions, errors, and required state are programmatically associated with controls.
- Color is never the only signal. Text labels distinguish Ready, Warning, Error, Paused, and Stale.
- Text and interactive controls meet WCAG AA contrast in every supported theme.
- Touch targets remain usable at narrow widths and at 200% zoom.
- Tables either become readable cards or retain labeled horizontal scrolling; content is never clipped without a disclosure.
- Primary navigation remains labeled at phone width. Critical actions do not depend on hover.
- Dates and relative times expose an exact localized value to assistive technology.
- Reduced-motion preferences disable nonessential motion and loading shimmer.

## Human-comprehensibility review matrix

Reviewers should test outcomes, not only API success.

| Scenario | User question the design must answer | Pass condition |
| --- | --- | --- |
| First run | “What do I do first?” | One primary action is visible without technical setup terms |
| Connected, empty project | “Is this ready, and what can I do?” | Connection and Team readiness are distinct; next action is explicit |
| Returning with active work | “What changed while I was away?” | Pending approvals, progress, review changes, and ready results are prioritized |
| Disconnected project | “Did my work disappear?” | Last known state remains visible and is labeled stale/disconnected |
| Rotated door | “Why can’t the Team reconnect?” | Cause, no-change statement, and Replace door action are shown without secret leakage |
| Partial Team start | “Who is ready?” | Each member has an independent state and failed members can be retried |
| Permission denial | “Was anything changed?” | Required role and unchanged state are stated |
| Approval | “What happens if I answer?” | Project, work, consequence, required inputs, and recovery disposition are clear |
| Review rejection | “Is the work done?” | State says Changes requested and shows current reviewer direction |
| Reviewed result | “Can I trust this is final?” | Independent approval, validation, changes, and limitations are visible |
| Pause or stop | “Did the Team actually stop?” | UI distinguishes requested, acknowledged, partial, and unreachable states |
| Bounded data | “Is this everything?” | Returned/omitted or bounded status is explicit |
| WORK/PERSONAL | “Which environment am I acting in?” | Trust domain is visible before connection, work, approval, and operations |
| Keyboard-only | “Can I complete the journey without a pointer?” | Full journey, including approval and recovery, completes with visible focus |
| Narrow screen | “Can I still reach every capability?” | All seven destinations remain reachable and no primary action is clipped |

## Unresolved product decisions and safe defaults

1. **Which Home alternative ships by default?** Use Alternative A until multi-project usage evidence favors Alternative B. Preserve a direct Projects entry in both.
2. **What is the minimum Team?** Derive it from supported runtime roles. Do not imply that a reviewer exists when the runtime reports worker-only.
3. **Can work start with a partial Team?** Default to no. Require an explicit, supported reduced-Team policy before enabling it.
4. **Where is door material stored by AionUI?** Use only a documented host secret facility. If none exists, stop and report the host API gap.
5. **What does Pause mean for an in-flight action?** Default to safe-checkpoint acknowledgement, not immediate process termination.
6. **What does Stop mean for reviewed history?** Preserve history and results; stop future execution and clearly record the reason.
7. **How are notifications delivered?** Keep Home and Approvals authoritative. Add native notifications only when they deep-link to the same request and do not expose sensitive content.
8. **How are old results retained?** Preserve current bounded semantics and make retention/omission explicit. Do not infer deletion from absence.
9. **Can one Home show WORK and PERSONAL at once?** No. Require an explicit trust-domain switch and keep actions scoped to the selected domain.
10. **Should advanced users see technical terms?** Yes, in details and Settings. Default pages remain goal-oriented and plain-language.
