# Board butler (shadow mode)

`board_butler.py` is a single resident coordinator seat that drafts evidence-backed
answers to mechanically checkable coordinator questions. It never answers a
question and never mutates a ticket. Its only Central write is a CAS-protected
merge of `would_answer` findings into `coordinator_findings`.

The policy is intentionally fail-closed. Gate waivers, scope changes, release
actions, membership changes, and registry changes always escalate. Unknown or
incomplete evidence also escalates. Mechanical drafts cite one named product or
repository source: `git merge-base`, `ticket_get`, a ticket annotation, or a
seat capability row.

Questions that propose proceeding despite a blocked, skipped, failed, or
never-reached suite use the coverage map declared by `tools/ci_manifest.py`.
The butler compares the submitted cumulative file list with the quoted suite
results. A non-passing suite escalates only when it covers the diff; an
unrelated blocked suite does not turn a docs-only submission into an escalation.

The resident runner acquires an exclusive pidfile lock before reading
credentials or opening Central. One-shot kill-switch and veto commands bypass
that resident lock so they remain operable during normal service; their durable
`coordinator_findings` mutation is still compare-and-swap protected. A second
resident remains lock-rejected and exits nonzero. The runner joins as a
coordinator with `can_work=false` and `can_review=false`, then refuses to run if
another working or reviewing seat shares its principal. Deploy it with its own
credential and state paths; do not reuse a worker or reviewer token.

One-cycle shadow validation:

```sh
PYTHONPATH=packages/client/src \
python3 tools/board-butler/board_butler.py \
  --url https://central.example.invalid/mcp \
  --token-path /PATH/TO/board-butler.jwt \
  --home-board pursers \
  --agent-name board-butler-1 \
  --repo /PATH/TO/Pursers \
  --pid-file /PATH/TO/state/board-butler.pid \
  --cursor-file /PATH/TO/state/board-butler.cursor.json \
  --once --dry-run
```

Without `--once`, the process holds one reconnecting journal/seat subscription
and sleeps inside the push stream until a coordinator-question cue arrives.
There is no polling fallback, repeated ticket list, or cursor-0 catch-up. The
positive cursor file is reused across restarts; a zero or invalid cursor starts
at the current journal watermark. A closed push stream terminates the process
instead of reconnect-spinning. `--dry-run` prints the proposed finding and
makes no Central write or cursor-file update, so the same question remains
available to a later non-dry run.

## Declared configuration

`coordinator_config.board_butler` is a strict versioned document. Unknown or
invalid keys fail closed and queue the question for the coordinator. Effective
settings resolve in this documented order: safe defaults, `global`, the named
`projects` override, then the `boards` override. Later layers change only the
keys they declare. The runner resolves the project name by matching its home
board in `project_registry`; `--project` is only the explicit fallback when a
registry row is unavailable or ambiguous.

```json
{
  "board_butler": {
    "schema_version": 1,
    "global": {
      "mode": "shadow",
      "answer_scope": {
        "ancestry": "escalate",
        "ticket_status": "escalate",
        "seat_capability": "escalate",
        "waiver_applicability": "escalate",
        "corpus_lookup": "escalate",
        "coverage_check": "escalate"
      },
      "required_evidence_kinds": [
        "git_ancestry", "ticket_status", "annotation",
        "seat_capability", "manifest_coverage", "corpus"
      ],
      "ceilings": {"per_hour": 5, "per_ticket": 2, "per_board": 20},
      "hold_before_post_s": 3600,
      "active_windows": [
        {"days": ["mon", "tue"], "start": "00:00", "end": "06:00", "timezone": "UTC"}
      ],
      "kill_switch": true,
      "auto_demote": {"veto_count": 3, "window_s": 3600},
      "classification": {
        "model": null, "endpoint_ref": null, "key_ref": null
      },
      "drafting": {"model": null, "endpoint_ref": null, "key_ref": null}
    },
    "projects": {"Pursers": {"ceilings": {"per_hour": 4}}},
    "boards": {"pursers": {"hold_before_post_s": 7200}}
  }
}
```

The class list is not a general permission switch. `scope_change`,
`gate_waiver`, `release`, `membership`, and `registry` are permanently
escalation-only; validation rejects attempts to set them to `auto`. An empty or
disabled evidence floor is also rejected, as are self-review, merge-to-main,
and inline API-key fields because none belong to the schema. Model and endpoint
are configuration; `key_ref` is an opaque `file:<id>.key` reference into the
private 0600 provider secret directory, never a home path or credential value.
Provider entries may also contain bounded
non-secret `extra_headers`, `key_header`, `key_prefix`, and a relative
`validation_path`. Fleet validates and saves these settings, then the resident
re-resolves them and reads the referenced key at the start of every question
cycle. No process restart or hand edit is required.

When the drafting provider is configured, the resident sends one bounded
OpenAI-compatible chat-completions request to the configured endpoint for each
question that clears the local rate limits. The request uses the exact selected
model, optional headers, and credential read from `key_ref`; none of those secret
bytes enter the prompt. Deterministic policy and evidence still set the verdict,
and the provider supplies only the shadow draft text. A provider failure or a
response that contains the credential fails closed to a fixed, key-free
escalation message.

The three draft ceilings are real queue boundaries. A hit produces a
`butler_queued` finding with an `ESCALATE` verdict instead of dropping the
question. The default hourly value reuses `intake.rate_per_hour` when that
value is valid. Every finding reports the fully resolved settings and their
source layers.

Each draft contains a durable hold record with draft, release, and veto times,
so restarts do not reset the timer. Held drafts are projected into Fleet's
**Waiting for you** surface. Record a veto (including its required reason) or
engage the immediate kill switch with one-shot control commands:

```sh
python3 tools/board-butler/board_butler.py <normal arguments> \
  --veto-question CQ-example --control-reason "evidence is stale"
python3 tools/board-butler/board_butler.py <normal arguments> \
  --kill-switch --control-reason "operator incident"
```

Vetoes inside `auto_demote.window_s` are counted from durable control state;
at `auto_demote.veto_count` the future active-mode eligibility demotes to
shadow and reports the reason. Active windows accept IANA timezones and
weekday names (`mon` through `sun`), including overnight windows.

This deliverable remains shadow-only even if configuration requests `active`:
`effective_mode` is always reported as `shadow`, and there is still no sending
method. `future_active_state` reports whether a later active-mode implementation
would be eligible, outside its window, killed, or auto-demoted. This preserves
the operator's knobs without silently enabling the separate active-mode scope.

Question handling is replay-safe. The durable cursor advances only after the
finding write succeeds, and a repeated question ID reuses its existing finding
without consuming a cap or issuing another write.

Run the suite with:

```sh
python3 -m pytest -q tools/board-butler/tests
```

## 2026-09-16 backlog replay

The authoritative correction `AN-000000001056` identifies 27 coordinator
questions, not 29. The worker-visible Central response returned the exact
question text, kind, and recorded answer for three records on
`TK-02bf4d01d662`; the other 24 ticket projections omitted their question
records. The committed fixture therefore replays those three records and names
all 24 unavailable ticket/question pairs without inventing messages or answers,
as the correction explicitly permits for a truthfully partial corpus.

The available replay classifies one question as `MECHANICAL`, two as
`ESCALATE`, and none as `UNKNOWN`. It disagrees with the recorded disposition
for `CQ-53524d65cdb51016`: ticket-status phrasing wins the current policy match
even though the question ultimately asked the coordinator to decide how
resolved items should appear in the document. That disagreement is retained as
a finding rather than hidden.
