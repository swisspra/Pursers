# Separate delivery branch implementation plan

Direction: mapped read-only base -> ticket work and independent review -> dedicated
Pursers delivery branch -> customer-owned final merge.

1. Validate shared policy and refs; preserve registry compatibility and source mapping.
2. Add guarded delivery preview/apply with admin checks, CAS and create-only Git refs.
   Permit pause controls without moving active leases; block live routing changes.
3. Route onboarding, worker bases and PR writeback consistently. Add a deterministic
   integration gate with durable attempts, exact validation and uncertain-result
   reconciliation. No completion into the mapped base or environment branches.
4. Add Projects configuration and Settings navigation; preserve drafts and show the
   exact preview. Surface confirmed handoff readiness separately from PR creation.
5. Exercise fake connector failures, changed refs, wrong targets, restart recovery,
   branch-creation races and browser interactions. Run affected suites and CI parity.
6. Document defaults, mapping, access requirements, adapter limitations and recovery.
   Deploy only verified code; preserve existing PR targets and source-index history.
   Native automatic integration stays disabled when exact validation is unavailable.
