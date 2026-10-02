# Integration operator responsibilities

Integrate only independently approved exact commits onto the configured branch.
Resolve shared-file conflicts deliberately and re-run the required gates on the
actual candidate. Generated locks, checksums, and references use their supported
generators and owner; a worker's manual edit is not a valid refresh.

Keep approval, integration, pull-request creation, merge, and release as distinct
facts. Record the exact input commits, candidate branch and SHA, checks, conflicts,
result, and rollback path.
