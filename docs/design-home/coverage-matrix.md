# Pursers Home Coverage Matrix

This matrix prevents the approachable Home redesign from silently removing capabilities present across the Personal dashboard, Fleet dashboard, and AionUI extension.

## Proposed Navigation

| Destination | Ordinary-user purpose | Current capabilities retained |
| --- | --- | --- |
| Home | Understand what matters now and take the next safe step | Personal Today health, ticket metrics, current work, active agents, latest handoff, pinned decision, recent activity, connection state, first-run join guidance |
| Projects | Choose or connect a project | Fleet Overview, Boards hub, central/board selection, project registry, add project, disconnected/empty project recovery |
| Work | Describe, follow, and inspect delegated work | Personal Work groups; ticket list/detail; ownership; lease; priority; rejected/review-ready states; board Tickets, Timeline, Changes, Flow, Routes |
| Team | Understand who is working and whether the Team is healthy | Personal Agents; Fleet Agents hub and pool; worker/seat status; one folder per teammate; monitor-only lead; board dispatch; paused/stopped/partial Team states |
| Approvals | Resolve decisions requiring human judgment | Intake approve/decline, submitted review, human request resolution, permission-denied explanation, destructive-operation confirmation, snooze/acknowledge behavior where supported |
| Activity | Review bounded work history and relationships | Personal Activity, event scope/truncation/resync labels, board timeline, provenance routes, Personal Links and memory/file/tag relationships |
| Settings | Configure connections and advanced operations | Extension Join/status; door copy/rotation; coordinator config; seats; dispatch policy; wait bridge; release/registry status; workers; doctor; overhead; diagnostics; theme/density |

## Surface-to-Route Acceptance

| Current surface | Required redesigned destination | Acceptance condition |
| --- | --- | --- |
| Personal Today | Home | Health, next action, current work, teammates, decision/result, connection and bounded-data labels remain visible without technical jargon. |
| Personal Work | Work | All status groups remain reachable: Open, Working, Submitted, Needs attention, Done, Ended, and unknown states. |
| Personal Agents | Team | Status, role, platform, project, ticket, idle/stale, and duplicate-name warnings remain available. |
| Personal Fleet | Projects + Team | Project registry and shared worker pool remain distinct and reachable. |
| Personal Links | Activity | Ticket-memory-file-tag relationships remain searchable and copyable without implying a complete audit graph. |
| Personal Activity | Activity | Bounded feed, cursor/resync, dropped/has-more, synthetic/live scope, and stale/error states remain explicit. |
| Fleet overview | Home + Projects | Cross-central health, attention, waiting-for-user, and board entry points remain available. |
| Fleet board workspace | Work | Tickets, Timeline, Changes, Flow, Routes, intake panel, and findings remain available from a board or ticket detail. |
| Fleet agents/workers | Team + Settings | Roster and health are visible in Team; management/test/start/stop/restart stay in advanced Settings with confirmation. |
| Fleet operations/config | Settings | Coordinator thresholds, policies, plans, suggestions, apply/import, doctor, overhead, release, registry, and bridge controls remain grouped by purpose. |
| Extension Join | Home first-run + Settings | Paste-door join, progress, failure hint, and redacted status remain supported without exposing the secret after submission. |
| Personal MCP App | Shared shell | Host theme/font/safe-area integration, read-only truth, and authorized bounded projections remain intact. |

## State Coverage

| State | Required behavior |
| --- | --- |
| First run | Explain one outcome-oriented path: connect a door, start a Team, describe work, watch progress, answer an approval, receive a reviewed result. Do not show populated work simultaneously. |
| Returning user | Lead with the most important current action, then health, progress, teammates, approvals, and reviewed result. |
| Empty project | Say there is no work yet, explain the next safe action, and keep project switching available. |
| Disconnected board | Retain last-known data only when labeled stale; explain retry/reconnect without fabricating live status. |
| Expired or rotated door | Explain that the connection must be refreshed; never display the prior door or bearer value. |
| Partial Team start | Name which teammates are ready, starting, failed, or unavailable; keep the lead monitor-only. |
| Permission denied | State what cannot be read or changed and offer only supported recovery. |
| Paused or stopped Team | Show propagation status per teammate and distinguish pending, completed, and failed stops. |
| Loading/error | Preserve layout, announce status, avoid false zero counts, and provide bounded retry behavior. |
| Mobile | Single readable column, no horizontal page overflow, all seven destinations reachable, and every visible control at least 44px. |

## Direction Boundary

Direction A, Warm Guided Home, was selected through `HR-d68423777f76a864`. The canvas now includes Home, Projects, Work, Team, Approvals, Activity, and Settings in that visual system. Direction B remains preserved as an unselected alternative and is not an implementation foundation.

The page-level implementation contract and preview map live in `docs/design-home/page-specs.md`. The route boundary is source-backed: `#/seats`, `/api/config`, `/api/intake`, `/api/dispatch`, `/api/workers`, and `/api/doors` are covered; the rejected inventory's `/central/{central}/workers` and `/seats` routes are excluded.
