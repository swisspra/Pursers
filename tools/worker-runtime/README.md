# Headless worker and reviewer runtime

`pursers-worker` runs one continuously re-arming board seat against an
OpenAI-compatible chat-completions endpoint. It uses the same JWT seat,
registry, claim, lease, submission, and independent-review flow as an
interactive worker. Provision the seat first with `seat_admin`.

The config is private local input, never board state or a web form. Both the
config and referenced secret files must be regular files with mode `0600`.

```toml
boards = "registry"

[seat]
agent_name = "worker-api-1"
role = "worker" # worker | reviewer; default worker
central_url = "https://127.0.0.1:8766/mcp"
token_file = "/private/path/seat.jwt"

[claim]
max_tier = "standard" # light | standard | heavy; default heavy
require_assigned_only = false
roles = ["frontend", "backend"] # optional role slugs; empty = generalist

[review]
max_reviews_per_hour = 12 # reviewer safety limit; default 12

[llm]
base_url = "https://proxy.example/v1"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model = "configured-model"
max_tokens = 4096
max_iterations = 40
command_timeout_s = 120
```

```bash
chmod 600 /private/path/worker.toml /private/path/seat.jwt
export OPENAI_COMPATIBLE_API_KEY='set-in-private-shell-or-secret-store'
python tools/worker-runtime/pursers_worker.py /private/path/worker.toml
```

Instead of `api_key_env`, use `api_key_file = "/private/path/proxy.key"`.
Never put a key inline in the config. `boards` may be `"registry"` or a JSON
array / TOML string array of board IDs.

The model receives the static worker directive first, then board context, then
the dynamic ticket. `claim.roles` are optional role slugs (e.g. `frontend`,
`backend`). A specialist seat claims only untagged tickets or tickets whose
`role:` tags intersect its configured roles. A generalist (empty `claim.roles`)
retains the original behavior and claims every tier-eligible ticket. Tickets
with valid `role:` tags unseen by a specialist seat are skipped. Priority among
claimable tickets is assigned > role-match > untagged; tier limits still apply.
Reviewer mode ignores role tags. Available tools are bounded file reads/writes,
timeboxed shell commands jailed to the registry work directory, submit, and give-up.
Commands (not their output) are recorded in a mode-`0600` local session log.
The shell receives a non-secret environment with `HOME` and `TMPDIR` reset to
the assigned work directory. Configured token/key values are redacted from
model-visible output and logs; on macOS, the subprocess is additionally denied
read access to the configured secret files. The runtime never reviews or merges
its own work.

## Per-ticket worktrees

For a registered Git project, the worker creates a dedicated worktree after it
claims a ticket and checks out the board's integration ref on
`api/<normalized-agent-name>-<normalized-ticket-suffix>`. The checkout lives at
`<git-common-dir>/pursers-worktrees/<normalized-agent-name>-<normalized-ticket-id>`;
registered non-Git directories are used directly instead.

When ticket processing ends, submitted worktrees are removed. Clean
unsubmitted worktrees are also removed, while dirty unsubmitted worktrees are
retained for recovery. At startup, the worker resumes a single outstanding
claim when possible, then sweeps its managed worktree directory: clean
worktrees without an active claim are removed and dirty ones are retained.

For API review, provision a dedicated board reviewer seat with a different
principal/token from every worker, then set `seat.role = "reviewer"`. The
reviewer discovers submitted tickets across all configured boards and never
claims, renews, edits, releases, or submits ticket work. Its static
`REVIEWER-DIRECTIVE-API.md` message is sent before board and ticket context for
cache-friendly prompts. The model may only read jailed files, run allowlisted
read-only inspection/test commands, and return a strictly parsed structured
approve/reject verdict. Tests run in a write-denied project on macOS or a
disposable project copy elsewhere. Self-authored or provenance-free
submissions, invalid verdicts, and rate-limited reviews emit `FINDING
reviewer-runtime` on stderr and are never approved. A light reviewer skips
standard and heavy tickets. Every verdict is bound to the bounded latest
submission's `submitted_at`, author principal, and deterministic payload digest;
if a worker resubmits while the model is reviewing, that verdict is discarded
and the new revision remains eligible for a fresh pass. The mode-`0600` JSONL
log emits matching `review_started` and `review_finished` lifecycle records with
`board_id`, `ticket_id`, and the revision digest for bounded local dashboards.
