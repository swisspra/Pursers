# 07 — Cross-stream acceptance and integration handoff

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md).
**Dispatch:** keep parked until streams 01-06 have reviewed commits available. This is not authorization to merge main, regenerate operator-owned artifacts, release or deploy.

## Steps

- [ ] Record reviewed branch/SHA and acceptance evidence for all six streams across both repositories. Coordinator un-parks only after prerequisite evidence exists.
- [ ] In an isolated integration branch, reconcile shared Central registration, relay, dashboard actors, docs and changelog changes. Preserve each feature's interfaces; escalate incompatible design changes with concrete evidence.
- [ ] Ask the integration operator to refresh required generated outputs through supported generators. Do not manually edit generated hashes/locks/reference artifacts or weaken required gates.
- [ ] Run full strict CI parity on the integrated core: `python tools/ci_manifest.py check`, `collect --output /tmp/pursers-counts.json`, `run --jobs 2`, `verify --input /tmp/pursers-counts.json`. Run website build/check/dry-run in its repository and leak/diff checks for both.
- [ ] Exercise new-user read-only onboarding; authorized worker lease/work; separate reviewer reject/retry/approve; configured integration/delivery; dashboard reconnect; paged history; alias disambiguation; least-privilege denial; and unsupported/legacy host fallback.
- [ ] Confirm no eager full-board/skill loading, no model calls for status updates, no heartbeat-renewal side effect, truthful completion labels, bounded payloads, and unchanged stable identity. Compare measured response/page weight evidence.
- [ ] Check public website and guides against actual released status. Keep planned labels or defer publication until release; verify canonical links and no private content.
- [ ] Produce a concise integration report with commit mapping, literal suite outputs, browser/transport evidence, configuration/migration instructions, rollback, generated-artifact status and unresolved blockers.
- [ ] Submit for independent fleet review. Hand exact reviewed commits and release readiness to operator; do not mark release/deployment complete from local tests.

## Completion gate

The six features coexist and are independently reviewed with end-to-end evidence. Operator can make a concrete merge/release decision without reconstructing ticket history. Any missing dependency remains explicit rather than bypassed.
