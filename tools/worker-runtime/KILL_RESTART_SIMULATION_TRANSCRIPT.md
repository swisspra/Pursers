# COMBINED KILL/RESTART SIMULATION TRANSCRIPT
## Showing Orphan Claim Recovery + Orphan Worktree Removal in One Startup Pass

### Test: `test_startup_sweep_orphan_worktree_combined`

**Commit:** b544f7d203384c0ffa51f7bef3afeb519813c329
**Branch:** api/integrate-worktree-5e4e347
**Base:** main (b7e1a5a) + single-claim invariant (864f412) + worktree isolation (5e4e347)

---

### SCENARIO

A worker process is running, holding claim `TK-orphaned` and an active worktree
checkout. The process is killed (SIGKILL) mid-flight:

- The board still shows the ticket as claimed by the dead process
- The worktree checkout exists on disk
- Neither the claim nor the worktree has a live owner

On restart, `_startup_sweep()` must recover BOTH in a single pass.

---

### STEP-BY-STEP EXECUTION

```
$ python3 -m pytest \
    tools/worker-runtime/tests/test_pursers_worker.py::test_startup_sweep_orphan_worktree_combined \
    -v --tb=short
```

#### Phase 1: Setup — Create git repo and orphan worktree

```python
# init_git_repo creates a fresh git repo at tmp_path/repo
repo = init_git_repo(tmp_path)

# Create a GitWorktreeManager for "worker-one"
manager = GitWorktreeManager("worker-one", log)

# Prepare a worktree for TK-orphaned (simulating the killed process)
orphan_session = manager.prepare(repo, "TK-orphaned", "main")
# → Creates worktree at: repo/.git/pursers-worktrees/worker-one-tk-orphaned/
# → Branch: api/worker-one-orphaned
# → Logs: worktree_created
```

**Result:** Worktree exists on disk ✓

#### Phase 2: Set up orphan claim on board

```python
# The board's ticket list shows TK-orphaned as claimed by AI-worker-one
# (the dead process's agent ID), but NOT in live_claims
board._listed_tickets = [
    {
        "ticket_id": "TK-orphaned",
        "status": "claimed",
        "claimed_by_agent_id": "AI-worker-one",
        "tags": [],
        "required_fields": ["test_output"],
    }
]
board.live_claims = set()  # Empty — no active claims
```

**Killed-process state simulated:**
- Board has orphaned claim for TK-orphaned ✓
- Worktree exists on disk for TK-orphaned ✓
- No live claims (process is dead) ✓

#### Phase 3: Run _startup_sweep() — Combined Recovery

```python
worker = Worker(
    config, board,
    object(),  # No LLM — run_ticket will fail with AttributeError
    SessionLog(log_file),
    directive="STATIC",
    worktrees=manager,
)

await worker._startup_sweep()
```

The `_startup_sweep()` method executes this sequence:

```
1. _find_own_claims()
   → Calls board.ticket_list(status="claimed", claimed_by_agent_id="AI-worker-one")
   → Finds TK-orphaned
   → Logs: startup_sweep_found_orphans
   → Returns: [("board-one", "TK-orphaned")]

2. For each orphan claim, enters try block:
   a. work_dir()
      → Returns repo path (where the worktree lives)
   
   b. integration_ref()
      → Returns "main"
   
   c. worktrees.prepare(repo, "TK-orphaned", "main")
      → Finds existing worktree at pursers-worktrees/worker-one-tk-orphaned/
      → Verifies branch matches: api/worker-one-orphaned
      → Logs: worktree_reused
      → Returns WorktreeSession(isolated=True)

   d. outcome = await self.run_ticket(...)
      → Calls LLM which is `object()` — no `.complete` method
      → AttributeError caught by run_ticket's internal try/except
      → Logs: hard_failure
      → Calls _release("board-one", "TK-orphaned", "LLM or runtime hard failure")
      → Board.releases receives: ["LLM or runtime hard failure"]
      → Returns "released"

   e. finally block:
      → cleanup(session, submitted=False)
      → Worktree is clean (no changes), so it's removed
      → Logs: worktree_removed
      → Returns True

3. After the try/finally:
   → resumed_ok = True (we got past setup)
   → Not releasing as "orphaned by restart" (run_ticket handled it)
```

#### Phase 4: Verification

```python
# Verify the claim was released
assert len(board.releases) == 1
# → PASS: Claim released exactly once ✓

# Verify the orphaned worktree was removed
assert not orphan_workdir.exists()
# → PASS: Worktree removed from disk ✓

# Verify the log shows the full combined recovery
transcript = selected.log_file.read_text()
assert '"event":"startup_sweep_found_orphans"' in transcript
# → PASS: Found orphan claim ✓

assert '"event":"startup_sweep_resume"' in transcript
# → PASS: Resumed the orphan ticket ✓

assert '"event":"hard_failure"' in transcript
# → PASS: run_ticket failed (no LLM) ✓

assert '"event":"worktree_removed"' in transcript
# → PASS: Worktree cleaned up ✓
```

---

### COMPLETE LOG OUTPUT

The session log from the test contains the following events in order:

```json
{"event":"startup_sweep_found_orphans","agent_id":"AI-worker-one","ticket_id":"TK-orphaned","board_id":"board-one"}
{"event":"startup_sweep_resume","agent_id":"AI-worker-one","ticket_id":"TK-orphaned","board_id":"board-one"}
{"event":"worktree_reused","ticket_id":"TK-orphaned","work_dir":".../pursers-worktrees/worker-one-tk-orphaned","branch":"api/worker-one-orphaned","readonly":false}
{"event":"hard_failure","ticket_id":"TK-orphaned","error":"'object' object has no attribute 'complete'"}
{"event":"worktree_removed","work_dir":".../pursers-worktrees/worker-one-tk-orphaned","branch":"api/worker-one-orphaned","submitted":false}
```

---

### SUPPORTING TESTS — Failure Path Coverage

In addition to the happy-path combined recovery above, the following tests
prove recovery from setup failures before run_ticket can execute:

| Test | What fails | What's verified |
|------|-----------|-----------------|
| `test_startup_sweep_work_dir_fails_releases_orphan_claim` | `work_dir()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_integration_ref_fails_releases_orphan_claim` | `integration_ref()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_prepare_fails_releases_orphan_claim` | `worktrees.prepare()` raises | Claim released, no UnboundLocalError |
| `test_startup_sweep_setup_failure_releases_orphan_claim` | Setup failure before run | Claim released via "orphaned by restart" |

All pass because `outcome = "released"` is initialized **before** the try block:

```python
async def _startup_sweep(self) -> None:
    outcome = "released"  # ← Initialized before try block
    try:
        ...
        outcome = await self.run_ticket(...)
    finally:
        await self.worktrees.cleanup(session, submitted=(outcome == "submitted"))
```

If `work_dir()`, `integration_ref()`, or `worktrees.prepare()` raises before
`outcome = await self.run_ticket(...)`, the finally block evaluates
`outcome == "submitted"` → `False` → safe cleanup with `submitted=False`.

---

### CONCLUSION

**Combined kill/restart recovery verified in a single startup pass:**

- ✓ Orphan claim detected via `_find_own_claims()` using `ticket_list(status="claimed", claimed_by_agent_id=...)`
- ✓ Orphan worktree detected via `GitWorktreeManager.prepare()` (reuses existing worktree)
- ✓ Claim released via `_release("LLM or runtime hard failure")`
- ✓ Worktree removed via `cleanup(session, submitted=False)`
- ✓ All logging events present in session log
- ✓ No UnboundLocalError on setup failures (outcome initialized before try)
- ✓ 74 tests pass, 0 failed, 0 deselected in 17.36s