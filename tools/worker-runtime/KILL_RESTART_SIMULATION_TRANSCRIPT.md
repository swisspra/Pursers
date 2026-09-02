# COMBINED KILL/RESTART SIMULATION TRANSCRIPT
## Showing Orphan Claim Recovery + Orphan Worktree Removal in One Startup Pass

**Commit:** b9355525003aab37eebb0fc7531d3856293c63a4
**Branch:** api/integrate-worktree-5e4e347
**Base:** main (b7e1a5a + single-claim invariant + worktree isolation 5e4e347)

**Test date:** 2026-09-02
**Python:** 3.12.14 (pytest 9.1.1, anyio 4.14.2)
**Platform:** darwin

---

## TRANSCRIPT 1: Full Worker-Runtime Test Suite

### Command

```
$ cd /Users/swissp/Desktop/Claude/MCP\ Server/Pursers-local && \
  .venv_test/bin/python3 -m pytest \
  tools/worker-runtime/tests/test_pursers_worker.py \
  -v
```

### Full Output

```
============================= test session starts ==============================
platform darwin -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- /Users/swissp/Desktop/Claude/MCP Server/Pursers-local/.venv_test/bin/python3
cachedir: .pytest_cache
rootdir: /Users/swissp/Desktop/Claude/MCP Server/Pursers-local
plugins: anyio-4.14.2
collecting ... collected 74 items

tools/worker-runtime/tests/test_pursers_worker.py::test_fake_server_happy_path_claim_edit_submit_and_secret_free_log PASSED [  1%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[light-light-1] PASSED [  2%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[light-standard-None] PASSED [  4%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[light-heavy-None] PASSED [  5%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[standard-light-1] PASSED [  6%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[standard-standard-1] PASSED [  8%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[standard-heavy-None] PASSED [  9%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[heavy-light-1] PASSED [ 10%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[heavy-standard-1] PASSED [ 12%]
tools/worker-runtime/tests/test_pursers_worker.py::test_tier_filter_matrix[heavy-heavy-1] PASSED [ 13%]
tools/worker-runtime/tests/test_pursers_worker.py::test_absent_tier_defaults_to_standard_and_assigned_only_is_enforced PASSED [ 14%]
tools/worker-runtime/tests/test_pursers_worker.py::test_max_tier_light_skips_heavy_and_claims_light PASSED [ 16%]
tools/worker-runtime/tests/test_pursers_worker.py::test_fresh_light_api_advertises_before_idle_wait_and_blocks_heavy_dispatch PASSED [ 17%]
tools/worker-runtime/tests/test_pursers_worker.py::test_assigned_ticket_is_claimed_before_earlier_unassigned_ticket PASSED [ 18%]
tools/worker-runtime/tests/test_pursers_worker.py::test_stop_interrupts_blocked_board_wait PASSED [ 20%]
tools/worker-runtime/tests/test_pursers_worker.py::test_path_escape_rejected_then_give_up_releases_claim PASSED [ 21%]
tools/worker-runtime/tests/test_pursers_worker.py::test_shell_cannot_inherit_configured_api_key PASSED [ 22%]
tools/worker-runtime/tests/test_pursers_worker.py::test_shell_cannot_return_or_log_seat_token PASSED [ 24%]
tools/worker-runtime/tests/test_pursers_worker.py::test_max_iterations_releases_claim PASSED [ 25%]
tools/worker-runtime/tests/test_pursers_worker.py::test_lease_is_renewed_while_ticket_runs PASSED [ 27%]
tools/worker-runtime/tests/test_pursers_worker.py::test_config_requires_mode_0600_and_never_accepts_inline_keys PASSED [ 28%]
tools/worker-runtime/tests/test_pursers_worker.py::test_static_directive_prefix_is_byte_identical_across_tickets PASSED [ 29%]
tools/worker-runtime/tests/test_pursers_worker.py::test_keychain_config_uses_exact_security_argv_and_never_exposes_secret PASSED [ 31%]
tools/worker-runtime/tests/test_pursers_worker.py::test_keyless_loopback_is_allowed_but_remote_requires_a_key_source PASSED [ 32%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_accepts_only_complete_structured_results[arguments0-expected0] PASSED [ 33%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_accepts_only_complete_structured_results[arguments1-expected1] PASSED [ 35%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[approve] PASSED [ 36%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[garbage1] PASSED [ 37%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[garbage2] PASSED [ 39%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[garbage3] PASSED [ 40%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[garbage4] PASSED [ 41%]
tools/worker-runtime/tests/test_pursers_worker.py::test_parse_review_verdict_rejects_garbage[garbage5] PASSED [ 43%]
tools/worker-runtime/tests/test_pursers_worker.py::test_fake_llm_reviewer_approve_reject_and_garbage[arguments0-approve-1] PASSED [ 44%]
tools/worker-runtime/tests/test_pursers_worker.py::test_fake_llm_reviewer_approve_reject_and_garbage[arguments1-reject-1] PASSED [ 45%]
tools/worker-runtime/tests/test_pursers_worker.py::test_fake_llm_reviewer_approve_reject_and_garbage[arguments2-skipped-0] PASSED [ 47%]
tools/worker-runtime/tests/test_pursers_worker.py::test_session_log_persists_and_clears_bounded_review_state PASSED [ 48%]
tools/worker-runtime/tests/test_pursers_worker.py::test_session_log_runtime_session_fences_stale_review_state PASSED [ 50%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_refuses_verdict_when_submission_changes_during_review PASSED [ 51%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_self_review_probe_skips_before_calling_llm PASSED [ 52%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_write_tool_attempt_is_blocked PASSED [ 54%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_test_command_cannot_mutate_project PASSED [ 55%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_concurrent_review_guard PASSED [ 56%]
tools/worker-runtime/tests/test_pursers_worker.py::test_review_rate_limiter_uses_rolling_hour PASSED [ 58%]
tools/worker-runtime/tests/test_pursers_worker.py::test_submitted_ticket_discovery_spans_all_configured_boards PASSED [ 59%]
tools/worker-runtime/tests/test_pursers_worker.py::test_scratch_board_worker_reject_resubmit_approve_e2e PASSED [ 60%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_resume_path PASSED [ 62%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_release_path PASSED [ 63%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_orphan_worktree_combined PASSED [ 64%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_setup_failure_releases_orphan_claim PASSED [ 66%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_prepare_failure_releases_orphan_claim PASSED [ 67%]
tools/worker-runtime/tests/test_pursers_worker.py::test_claim_guard_refuses_while_holding PASSED [ 68%]
tools/worker-runtime/tests/test_pursers_worker.py::test_release_read_back_mismatch PASSED [ 70%]
tools/worker-runtime/tests/test_pursers_worker.py::test_claim_guard_board_check_refuses_existing_claim PASSED [ 71%]
tools/worker-runtime/tests/test_pursers_worker.py::test_sigterm_path_unchanged PASSED [ 72%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_readonly_allowlist_allowed_matrix PASSED [ 74%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_readonly_allowlist_mutating_blocked PASSED [ 75%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_readonly_allowlist_flag_injection_blocked PASSED [ 77%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_name_status_works_as_flag PASSED [ 78%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_list_is_only_allowed_worktree_subcommand PASSED [ 79%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_branch_mutation_flags_all_blocked PASSED [ 81%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_non_git_commands_blocked PASSED [ 82%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_creation_jails_ticket_and_prompts_for_commit PASSED [ 83%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_cleanup_release_and_submitted_dirty_checkout PASSED [ 85%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_startup_sweeps_only_clean_inactive_orphans PASSED [ 86%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_non_git_passthrough PASSED [ 87%]
tools/worker-runtime/tests/test_pursers_worker.py::test_two_workers_receive_distinct_ticket_worktrees PASSED [ 89%]
tools/worker-runtime/tests/test_pursers_worker.py::test_reviewer_worktree_is_detached_and_readonly_context PASSED [ 90%]
tools/worker-runtime/tests/test_pursers_worker.py::test_worker_commits_in_ticket_branch_then_submit_cleans_worktree PASSED [ 91%]
tools/worker-runtime/tests/test_pursers_worker.py::test_board_api_exposes_registry_refs_and_only_own_active_claims PASSED [ 93%]
tools/worker-runtime/tests/test_pursers_worker.py::test_orphan_worktree_sweep_cleans_killed_process_worktree PASSED [ 94%]
tools/worker-runtime/tests/test_pursers_worker.py::test_orphan_worktree_sweep_preserves_active_claim_worktree PASSED [ 95%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_work_dir_fails_releases_orphan_claim PASSED [ 97%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_integration_ref_fails_releases_orphan_claim PASSED [ 98%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_prepare_fails_releases_orphan_claim PASSED [100%]

============================= 74 passed in 17.18s ==============================
```

### Exit Status

```
$ echo $?
0
```

**Result: 74 passed, 0 failed, 0 deselected, 0 errors ✓**

---

## TRANSCRIPT 2: Combined Orphan Claim + Worktree Recovery Test

### Command

```
$ cd /Users/swissp/Desktop/Claude/MCP\ Server/Pursers-local && \
  .venv_test/bin/python3 -m pytest \
  tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_orphan_worktree_combined \
  -v --tb=long
```

### Full Output

```
============================= test session starts ==============================
platform darwin -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- /Users/swissp/Desktop/Claude/MCP Server/Pursers-local/.venv_test/bin/python3
cachedir: .pytest_cache
rootdir: /Users/swissp/Desktop/Claude/MCP Server/Pursers-local
plugins: anyio-4.14.2
collecting ... collected 1 item

tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_orphan_worktree_combined PASSED [100%]

============================== 1 passed in 0.87s ===============================
```

**Exit status: 0 (0 = success)**

**Result: 1 passed ✓**

---

## TRANSCRIPT 3: All Orphan / Startup-Sweep Related Tests

### Command

```
$ cd /Users/swissp/Desktop/Claude/MCP\ Server/Pursers-local && \
  .venv_test/bin/python3 -m pytest \
  tools/worker-runtime/tests/test_pursers_worker.py \
  -k "orphan or startup_sweep" -v --tb=short
```

### Full Output

```
============================= test session starts ==============================
platform darwin -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0 -- /Users/swissp/Desktop/Claude/MCP Server/Pursers-local/.venv_test/bin/python3
cachedir: .pytest_cache
rootdir: /Users/swissp/Desktop/Claude/MCP Server/Pursers-local
plugins: anyio-4.14.2
collecting ... collected 74 items / 63 deselected / 11 selected

tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_resume_path PASSED [  9%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_release_path PASSED [ 18%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_orphan_worktree_combined PASSED [ 27%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_setup_failure_releases_orphan_claim PASSED [ 36%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_prepare_failure_releases_orphan_claim PASSED [ 45%]
tools/worker-runtime/tests/test_pursers_worker.py::test_git_worktree_startup_sweeps_only_clean_inactive_orphans PASSED [ 54%]
tools/worker-runtime/tests/test_pursers_worker.py::test_orphan_worktree_sweep_cleans_killed_process_worktree PASSED [ 63%]
tools/worker-runtime/tests/test_pursers_worker.py::test_orphan_worktree_sweep_preserves_active_claim_worktree PASSED [ 72%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_work_dir_fails_releases_orphan_claim PASSED [ 81%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_integration_ref_fails_releases_orphan_claim PASSED [ 90%]
tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_prepare_fails_releases_orphan_claim PASSED [100%]

====================== 11 passed, 63 deselected in 5.63s =======================
```

**Exit status: 0 (0 = success)**

**Result: 11 passed, 0 failed, 63 deselected ✓**

---

## COMBINED KILL/RESTART SCENARIO — How It Works

Below is the logical simulation of what happens when a process is killed mid-flight
and restarts, as verified by the test `test_startup_sweep_orphan_worktree_combined`.

### Scenario

A worker process is running, holding claim `TK-orphaned` and an active worktree
checkout. The process is killed (SIGKILL) mid-flight:

* The board still shows the ticket as claimed by the dead process
* The worktree checkout exists on disk
* Neither the claim nor the worktree has a live owner

On restart, `_startup_sweep()` recovers BOTH in a single pass.

### Step-by-Step Execution

```
Phase 1: Initialize git repo and create orphan worktree
  -> GitWorktreeManager.prepare(repo, "TK-orphaned", "main")
  -> Creates worktree at: repo/.git/pursers-worktrees/worker-one-tk-orphaned/
  -> Branch: api/worker-one-orphaned
  -> Logs: worktree_created

Phase 2: Simulate orphaned claim on board
  -> Board lists TK-orphaned as claimed by "AI-worker-one"
  -> Board.live_claims is empty (process is dead)

Phase 3: _startup_sweep() executes this sequence:
  1. _find_own_claims() -> finds TK-orphaned -> logs startup_sweep_found_orphans
  2. Enters try block:
     a. work_dir() -> returns repo path
     b. integration_ref() -> returns "main"
     c. worktrees.prepare() -> finds existing worktree, logs worktree_reused
     d. run_ticket() -> fails (no LLM), caught internally
        -> Logs: hard_failure
        -> Calls _release("LLM or runtime hard failure")
        -> Returns "released"
     e. finally: cleanup(session, submitted=False)
        -> Worktree is clean, removed
        -> Logs: worktree_removed

Phase 4: Verification
  - Claim was released exactly once
  - Orphan worktree was removed from disk
  - Log contains: startup_sweep_found_orphans, startup_sweep_resume,
                  hard_failure, worktree_removed
```

### Session Log Events (in order)

```
{"event":"startup_sweep_found_orphans","agent_id":"AI-worker-one","ticket_id":"TK-orphaned","board_id":"board-one"}
{"event":"startup_sweep_resume","agent_id":"AI-worker-one","ticket_id":"TK-orphaned","board_id":"board-one"}
{"event":"worktree_reused","ticket_id":"TK-orphaned","work_dir":".../pursers-worktrees/worker-one-tk-orphaned","branch":"api/worker-one-orphaned","readonly":false}
{"event":"hard_failure","ticket_id":"TK-orphaned","error":"'object' object has no attribute 'complete'"}
{"event":"worktree_removed","work_dir":".../pursers-worktrees/worker-one-tk-orphaned","branch":"api/worker-one-orphaned","submitted":false}
```

---

## SUPPORTING TESTS — Failure Path Coverage

| Test | What Fails | What's Verified |
|------|-----------|-----------------|
| `test_startup_sweep_work_dir_fails_releases_orphan_claim` | `work_dir()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_integration_ref_fails_releases_orphan_claim` | `integration_ref()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_prepare_fails_releases_orphan_claim` | `worktrees.prepare()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_setup_failure_releases_orphan_claim` | Setup failure before run | Claim released via "orphaned by restart" |

All pass because `outcome = "released"` is initialized **before** the try block:

```python
async def _startup_sweep(self) -> None:
    outcome = "released"  # <- Initialized before try block
    try:
        ...
        outcome = await self.run_ticket(...)
    finally:
        await self.worktrees.cleanup(session, submitted=(outcome == "submitted"))
```

---

## CONCLUSION

**Combined kill/restart recovery verified in a single startup pass:**

* [x] Orphan claim detected via `_find_own_claims()` using `ticket_list(status="claimed", claimed_by_agent_id=...)`
* [x] Orphan worktree detected via `GitWorktreeManager.prepare()` (reuses existing worktree)
* [x] Claim released via `_release("LLM or runtime hard failure")`
* [x] Worktree removed via `cleanup(session, submitted=False)`
* [x] All logging events present in session log
* [x] No UnboundLocalError on setup failures (outcome initialized before try)
* [x] 74 tests pass, 0 failed, 0 deselected
* [x] 11 orphan/startup_sweep tests pass
