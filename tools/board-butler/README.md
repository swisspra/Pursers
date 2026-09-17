# Board butler (shadow mode)

`board_butler.py` is a single resident coordinator seat that keeps findings
fresh for every active board in `project_registry` and drafts evidence-backed
answers to mechanically checkable coordinator questions. Each bounded refresh
runs the real `tools/coordinator/coordinator.py` derivation in shadow mode; the
butler never manufactures a timestamp or finding merely to satisfy freshness.
Inactive registry projects are not read or acted on.

Question handling remains shadow-only: the butler never answers a question.
Two ticket actions are permitted, both derived entirely from current board
state and disabled unless the operator opts that board in with
`--act-on-board`: it parks (without canceling) an open ticket after the configured
number of `no_live_candidates` dispatch cycles when the board has no live
`can_work=true` seat, and it records refusal of a proposed escalation target
whose identity cannot work. Every other judgment, including scope, gate,
release, option, and version decisions, remains a draft for a human.

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
  --refresh-seconds 60 \
  --once --dry-run
```

## Fleet service start, status, and emergency stop

The repo-owned fleet entry is `tools/board-butler/launch.sh`. It defaults to
`shadow` and the checked-in
`com.pursers.board-butler.plist.template` pins that mode explicitly. Replace
every `/PATH/TO/...` placeholder in a private staged copy, validate it, then the
operator turns it on with:

```sh
plutil -lint /PATH/TO/private/com.pursers.board-butler.plist
chmod 600 /PATH/TO/private/com.pursers.board-butler.plist
launchctl bootstrap "gui/$(id -u)" /PATH/TO/private/com.pursers.board-butler.plist
```

The template is not installed by the repository or by worker seats. Its
`KeepAlive.SuccessfulExit=false` policy restarts an unexpected crash but does
not loop after a clean fail-closed exit. The launcher publishes only PID, mode,
start time, and last activity to the private mode-0600 `runtime.json`; Fleet
also requires the private singleton pidfile to be locked by that PID and
verifies `/bin/ps` identifies a live, non-zombie `board_butler.py` process
before reporting a running state or sending `SIGTERM`. A stale runtime file is
therefore shown as **Configured · not running**, never as running.

The Settings page distinguishes **Not configured**, **Configured · not
running**, **Running · shadow**, and **Running · active**, and shows the last
observed activity. **Stop butler now** creates the private local `KILLED`
marker before sending `SIGTERM`. A board write already accepted by Central
remains atomic; an interrupted question leaves its cursor unadvanced and is
replayed. The launchd restart sees the marker, exits successfully before
reading the token, and remains stopped. The operator deliberately resumes
shadow mode by removing that marker and running:

```sh
launchctl kickstart "gui/$(id -u)/com.pursers.board-butler"
```

Active mode cannot be reached through the dashboard config or by changing the
mode field alone. First create the separate owned mode-0600 authorization:

```sh
python3 tools/board-butler/authorize_active.py \
  --output /PATH/TO/private/board-butler-state/active-authorized.json \
  --confirm ENABLE-BOARD-BUTLER-ACTIVE
```

Then the operator stages a plist whose `PURSERS_BUTLER_RUNTIME_MODE` is
`active`, reviews the separately declared active board, and reloads the job.
The resident refuses active mode unless the authorization file is valid and at
least one `--act-on-board` is present. Active mode enables only the two
mechanical ticket actions described above; coordinator-question answers remain
shadow drafts.

Repeat `--act-on-board` to opt in additional active registry boards. The acting
set is empty by default, and a configured board that is not active in the
registry is reported as ignored. Findings are still refreshed for every active
registry board regardless of the acting set. After a mechanical action, the
same cycle runs the real derivation again, so a newly parked ticket cannot
remain reported as starved until a later event.

The two operator-approved action classes form the default configured class set;
repeat `--active-action` to narrow that set. Every intended action is first
written durably to `coordinator_findings` with a release time and is shown in
Fleet's **Waiting for you** surface. It cannot execute before
`--action-hold-seconds` elapses, and `--veto-question BA-... --control-reason
<reason>` changes the durable hold to `vetoed`. For a non-home board, run the
control command with that board as `--home-board`. Adding another autonomous
class requires adding it to the configured class set; decision questions do not
become autonomous merely because the process is active.

Without `--once`, the process waits in the journal/seat push stream for a
coordinator-question cue, with a bounded timeout used to run the next registry
refresh. There is no status polling or cursor-0 catch-up. The positive cursor
file is reused across restarts; a zero or invalid cursor starts at the current
journal watermark. `--dry-run` prints the real derived state and any proposed
question finding, performs no ticket action, and makes no Central write or
cursor-file update.

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
`validation_path`. The relative `draft_path` and explicit
`draft_protocol=pursers_json_v1` select the provider-neutral draft contract.
Fleet validates and saves these settings, then the resident
re-resolves them and reads the referenced key at the start of every question
cycle. No process restart or hand edit is required.

When the drafting provider is configured, the resident sends one bounded
`pursers_json_v1` request to the configured relative draft path for each question
that clears the local rate limits. The request contains `protocol`, `model`, a
bounded `input` object, and `max_output_chars`; the response is a JSON object with
a string `draft`. The request uses the exact selected model, optional headers,
and credential read from `key_ref`; none of those secret bytes enter the input.
Deterministic policy and evidence still set the verdict, and the provider supplies
only the shadow draft text. A provider failure or a response that contains the
credential fails closed to a fixed, key-free escalation message.

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

Question answering remains shadow-only even if configuration requests
`active`: `effective_mode` in a draft is always `shadow`, and there is no
sending method. The separately authorized runtime active mode governs only the
two mechanical actions. `future_active_state` still reports whether a future
answer-sending implementation would be eligible, outside its window, killed,
or auto-demoted.

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
