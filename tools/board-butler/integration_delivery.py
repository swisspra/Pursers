"""Deterministic integration gate. Human promotion branches cannot be completed."""
from __future__ import annotations
import re
from collections.abc import Mapping
from pursers_client.delivery_workflow import parse_delivery_workflow

SHA = re.compile(r'^[0-9a-f]{40}$')


def blocked(reason):
    return {'state': 'integration_blocked', 'reason': reason}


async def integrate(policy, *, approved_sha, source_ref, target_sha, remote, checks,
                    prior_attempt, reserve, complete):
    p = parse_delivery_workflow(policy)
    if not p or p['mode'] != 'integration':
        return blocked('integration_not_configured')
    if (remote.get('targetRefName') != 'refs/heads/' + p['integration_branch']
            or remote.get('sourceRefName') != source_ref
            or source_ref in {'refs/heads/'+p[k] for k in ('integration_branch','base_branch')}):
        return blocked('PR_target_or_source_changed_existing_PR_preserved')
    if (not SHA.fullmatch(approved_sha or '')
            or remote.get('lastMergeSourceCommit', {}).get('commitId') != approved_sha):
        return blocked('source_changed_fresh_independent_review_required')
    if remote.get('status') == 'completed':
        if prior_attempt and prior_attempt != approved_sha + ':' + str(remote.get('lastMergeTargetCommit', {}).get('commitId', '')):
            return blocked('completed_target_differs_from_validated_attempt')
        merged = remote.get('lastMergeCommit', {}).get('commitId', '')
        if not SHA.fullmatch(merged):
            return blocked('merge_commit_unconfirmed')
        return {'state': 'integration_merged', 'merge_sha': merged}
    if remote.get('status') != 'active' or remote.get('isDraft'):
        return blocked('PR_not_active_or_is_draft')
    if prior_attempt:
        return {'state': 'pr_uncertain', 'reason': 'completion_attempt_exists_reconcile_only'}
    if p['collection_paused'] or not p['auto_integrate']:
        return {'state': 'integration_pending', 'reason': 'collection_paused' if p['collection_paused'] else 'automatic_integration_disabled'}
    if (not SHA.fullmatch(target_sha or '')
            or remote.get('lastMergeTargetCommit', {}).get('commitId') != target_sha):
        return blocked('target_changed_revalidation_required')
    if remote.get('mergeStatus') != 'succeeded':
        return blocked('conflict_or_merge_calculation_pending')
    if any(r.get('vote', 0) < 0 for r in remote.get('reviewers', [])):
        return blocked('reviewer_has_unresolved_negative_vote')
    if not isinstance(checks, Mapping):
        return blocked('validation_unavailable')
    validation = checks.get('validation', {})
    if (not isinstance(validation, Mapping) or validation.get('source_sha') != approved_sha
            or validation.get('target_sha') != target_sha or validation.get('passed') is not True):
        return blocked('exact_source_target_validation_required')
    policies = checks.get('policies')
    if not isinstance(policies, list) or not policies:
        return blocked('required_branch_policies_unavailable')
    required = [v for v in policies if isinstance(v, Mapping) and v.get('enabled') is True and v.get('blocking') is True]
    if not required or any(v.get('status') not in {'approved', 'notApplicable'} for v in required):
        return blocked('required_policy_not_satisfied')
    await reserve(approved_sha + ':' + target_sha)
    await complete()
    return {'state': 'integration_pending', 'reason': 'completion_requested_awaiting_confirmation'}
