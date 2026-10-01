# Release operator responsibilities

Release work requires a release-train ticket and explicit authority for each
protected action. Verify the frozen candidate, full gates, versions, changelog,
tag target, artifact provenance, and rollback. Publish from the approved tag,
not an unreviewed branch. A normal feature ticket or this skill never authorizes
merge, tag, publish, deployment, restart, or credential rotation.

Canonical guide:
<https://github.com/swisspra/Pursers/blob/main/docs/release-train.md>
