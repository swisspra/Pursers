# Skill-first installation and human AG-UI foundations

Approved scope: ship the portions independent of the changing autonomous harness now. Use public main as the source baseline, with an isolated feature branch. Three Luna roles: implementer, independent task reviewer, integration/UX verifier. Existing PR-body and reconciliation work remains owned by its existing fleet ticket and must pass independent review/tests before integration. No live runtime change or product release is included in this branch.

## Product decisions
- Skill installation is the primary entry for IDE users; a browser route remains possible through the same deterministic setup results.
- Add pursers-install, preserving pursers-start as the authorized connection/identity workflow. Do not turn installation into board authority or configure all IDE profiles.
- Bootstrap operates on an explicit new private installation directory, uses the released pinned Pursers package, preserves existing installs, and emits truthful installed/initialized/not-running/not-ready facts. It must not advertise the still-unpackaged Fleet/harness as installed. Runtime/service activation remains separate until the reviewed harness integration is ready.
- AG-UI v1 here is read-only snapshot observation over the current released Fleet cache. A finite SSE run prevents long-lived streams from occupying the bounded legacy HTTP server. Native journal push and mutation/resume adapters remain an explicit follow-up after harness freeze. Opening/refreshing observation must never execute a model, join a worker, renew a lease, or create work.
- Preserve existing guarded mutation APIs and all existing functionality. Expose unsupported AG-UI commands explicitly, never fake readiness.
- Human UX reduces choices: Add work, project overview, clear next action and publication/QA/security readiness. Existing guarded intake remains the sole submission path.
- Draft publication, integration, final QA and ready-for-team-merge are distinct. A draft with held QA or pending security is never ready for delivery. Missing facts are unknown.
- Preserve framework, logo, routes, themes, keyboard support, zoom 200%, reduced motion, and unsaved input. No new chat, animation framework, or dynamic executable markup.
- Keep full records/evidence accessible; summaries are bounded views, not truncation of storage.

## Delivery boundaries
No version bump, public/private main merge, tag, release, deployment, live credentials/services, VM mutation, or importing organization/customer artifacts. Public docs/code use generic identifiers and /PATH/TO/... placeholders. Documentation and changelog describe the actual partial capability and deferred activation honestly.
