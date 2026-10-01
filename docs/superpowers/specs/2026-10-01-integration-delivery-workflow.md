# Separate delivery branch workflow

## Outcome
Pursers reads the base branch supplied by each repository/source mapping and
creates a dedicated delivery branch from its verified SHA. Workers use isolated
ticket branches; independently approved PRs target only the delivery branch.
The customer handles every final merge. No environment branch or downstream
promotion PR is managed by this workflow.

## Contract
A validated `delivery_workflow` registry policy defines `base_branch`,
`integration_branch`, `auto_integrate` and `collection_paused`. The default delivery
name is `pursers-integration`, avoiding the existing `pursers/<ticket>` namespace.
Base and delivery refs must be distinct. `integration_ref` becomes the work base;
source analysis retains its original mapping. Existing direct projects and PRs
are unchanged until explicitly migrated. New-project defaults inherit each
mapping's base, not another project's base.

## Configuration and UI
Projects offers repository selection, a visible route, delivery/base branch fields,
automatic integration and pause controls, and defaults for new onboarding.
Settings links to the same editor. Preview/apply requires board administration,
registry CAS, fresh remote refs and explicit confirmation. Routing changes require
active work to drain; pausing an existing route does not stop worker leases.
Create-only branch mutation cannot overwrite an existing ref. Form drafts survive
refresh. Delivery stages distinguish PR creation, blocked/pending integration, and
confirmed work ready for the customer's team. Credentials are never projected.

## Integration gate
One candidate per board per pass; uncertain attempts must reconcile first.
Require exact repository, target, approved source SHA, current target validation,
merge readiness, independent approval and successful required branch policies.
Reserve a durable attempt before completing, never bypass policies, and never
blindly retry. Unknown validation blocks. No LLM is called by reconciliation.
The checks adapter must provide validation for both exact SHAs; missing support
must remain visible, not silently replaced by a model judgement.

## Operational boundaries
One authorized Butler writer owns each repository/index. Existing external PRs
are not retargeted automatically. Cross-host merge arbitration and conflict repair
are separate work. Pause integration before handing off a stable branch. A confirmed
merge is not deployment or evidence that the original Sonar scan has cleared.
