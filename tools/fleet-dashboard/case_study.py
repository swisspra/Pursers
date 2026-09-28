"""Source-backed sequential/parallel case-study aggregation.

The dashboard consumes preregistered run manifests plus bounded board records.  It
never guesses release, eligibility, review-start, finish, cost, or a matched
baseline from current ticket state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from base64 import b32encode
from datetime import datetime, timezone
from typing import Any

_ACTIVE = frozenset({"claimed", "in_progress", "creating_report"})
_SUBMITTED = frozenset({"submitted", "reviewing", "in_review"})
_ACCEPTED_VERDICTS = frozenset({"approve", "approved", "accept", "accepted"})
_ARMS = frozenset({"sequential", "parallel"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_ALIAS_RE = re.compile(r"^(?:project|work)-[a-z2-7]{16}$")
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_MAX_RUNS = 32
_MAX_TICKETS = 500
_MAX_EVENTS = 20_000


class CaseStudyError(ValueError):
    """The private study manifest is invalid or unsafe to interpret."""


def _text(value: Any, label: str, *, limit: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise CaseStudyError(f"{label} must be a non-empty bounded string")
    return value


def _digest(value: Any, label: str) -> str:
    value = _text(value, label, limit=64)
    if not _DIGEST_RE.fullmatch(value):
        raise CaseStudyError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 3)


def _event_phase(event: dict[str, Any]) -> str | None:
    kind = str(event.get("kind") or "")
    status_to = str(event.get("status_to") or "")
    verdict = str(event.get("review_verdict") or "").lower()
    if kind == "ticket_eligible":
        return "eligible"
    if kind == "ticket_offered":
        return "offered"
    if kind in {"ticket_claimed", "ticket_started"} or status_to in _ACTIVE:
        return "started"
    if kind in {"ticket_submitted", "ticket_resubmitted"} or status_to in _SUBMITTED:
        return "submitted"
    if kind in {"review_started", "ticket_review_started"}:
        return "review_started"
    if kind in {"ticket_accepted", "ticket_closed"} or status_to == "closed":
        return "closed"
    if kind in {"review_verdict", "ticket_reviewed"} or verdict:
        return "closed" if verdict in _ACCEPTED_VERDICTS else "reviewed"
    return None


def _source_records(
    board_rows: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], list[dict[str, Any]]],
    set[str],
]:
    tickets: dict[tuple[str, str], dict[str, Any]] = {}
    events: dict[tuple[str, str], list[dict[str, Any]]] = {}
    incomplete_boards: set[str] = set()
    event_count = 0
    for row in board_rows:
        if not isinstance(row, dict):
            continue
        board_id = str(row.get("board_id") or "")
        if not board_id:
            continue
        if (
            row.get("error")
            or row.get("event_window_truncated")
            or row.get("event_resync_required")
        ):
            incomplete_boards.add(board_id)
        snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
        for ticket in snapshot.get("tickets", []):
            if isinstance(ticket, dict) and isinstance(ticket.get("ticket_id"), str):
                tickets[(board_id, ticket["ticket_id"])] = ticket
        for event in row.get("events", []):
            if not isinstance(event, dict) or not isinstance(
                event.get("ticket_id"), str
            ):
                continue
            if event_count >= _MAX_EVENTS:
                incomplete_boards.add(board_id)
                break
            events.setdefault((board_id, event["ticket_id"]), []).append(event)
            event_count += 1
    return tickets, events, incomplete_boards


def _ticket_ref(value: Any, run_id: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CaseStudyError(
            f"run {run_id} tickets must use board_id/ticket_id objects"
        )
    return {
        "board_id": _text(value.get("board_id"), "ticket board_id", limit=80),
        "ticket_id": _text(value.get("ticket_id"), "ticket_id", limit=96),
        "template_digest": _digest(value.get("template_digest"), "template_digest"),
    }


def _manifest_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows = run.get("events")
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > _MAX_EVENTS:
        raise CaseStudyError("run events must be a bounded list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise CaseStudyError("run events must contain objects")
        kind = _text(row.get("kind"), "event kind", limit=64)
        occurred = _time(row.get("occurred_at"))
        if occurred is None:
            raise CaseStudyError("run event occurred_at must be RFC 3339 with timezone")
        result.append(
            {
                "kind": kind,
                "occurred_at": _iso(occurred),
                "ticket_id": row.get("ticket_id")
                if isinstance(row.get("ticket_id"), str)
                else None,
                "board_id": row.get("board_id")
                if isinstance(row.get("board_id"), str)
                else None,
                "source": "study_manifest",
                "quality": "observed",
            }
        )
    return result


def _snapshot_fallback(ticket: dict[str, Any], phase: str) -> datetime | None:
    if phase == "started":
        return _time(ticket.get("claimed_at"))
    if phase == "submitted":
        history = ticket.get("submission_history")
        if isinstance(history, list):
            candidates = [
                _time(item.get("submitted_at"))
                for item in history
                if isinstance(item, dict)
            ]
            candidates = [item for item in candidates if item]
            if candidates:
                return max(candidates)
        return _time(ticket.get("submitted_at"))
    if phase == "closed":
        history = ticket.get("review_history")
        if isinstance(history, list):
            approved = [
                _time(item.get("reviewed_at"))
                for item in history
                if isinstance(item, dict)
                and str(item.get("verdict") or item.get("review_verdict") or "").lower()
                in _ACCEPTED_VERDICTS
            ]
            approved = [item for item in approved if item]
            if approved:
                return max(approved)
        if ticket.get("status") == "closed":
            return _time(ticket.get("reviewed_at") or ticket.get("updated_at"))
    return None


def _timeline_for_ticket(
    ticket: dict[str, Any],
    board_events: list[dict[str, Any]],
    study_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, datetime | None]]:
    rows: list[dict[str, Any]] = []
    for source, items in (
        ("board_journal", board_events),
        ("study_manifest", study_events),
    ):
        for event in items:
            phase = _event_phase(event)
            occurred = _time(event.get("occurred_at"))
            if phase is None or occurred is None:
                continue
            rows.append(
                {
                    "phase": phase,
                    "occurred_at": _iso(occurred),
                    "source": source,
                    "quality": "observed",
                    "seq": event.get("seq")
                    if isinstance(event.get("seq"), int)
                    else None,
                    "actor": event.get("actor")
                    if isinstance(event.get("actor"), str)
                    else None,
                }
            )
    phases: dict[str, datetime | None] = {
        key: None
        for key in (
            "eligible",
            "offered",
            "started",
            "submitted",
            "review_started",
            "reviewed",
            "closed",
        )
    }
    rows.sort(
        key=lambda item: (
            _time(item["occurred_at"]) or datetime.min.replace(tzinfo=timezone.utc),
            item["seq"] or -1,
        )
    )
    for row in rows:
        occurred = _time(row["occurred_at"])
        phase = row["phase"]
        if phase in {"eligible", "offered", "started", "review_started"}:
            phases[phase] = phases[phase] or occurred
        else:
            phases[phase] = occurred
    for phase in ("started", "submitted", "closed"):
        if phases[phase] is not None:
            continue
        fallback = _snapshot_fallback(ticket, phase)
        if fallback is not None:
            phases[phase] = fallback
            rows.append(
                {
                    "phase": phase,
                    "occurred_at": _iso(fallback),
                    "source": "board_snapshot",
                    "quality": "observed_fallback",
                    "seq": None,
                    "actor": None,
                }
            )
    rows.sort(
        key=lambda item: (
            _time(item["occurred_at"]) or datetime.min.replace(tzinfo=timezone.utc)
        )
    )
    return rows, phases


def _peak_concurrency(intervals: list[tuple[datetime, datetime]]) -> int:
    points: list[tuple[datetime, int]] = []
    for start, end in intervals:
        points.extend(((start, 1), (end, -1)))
    active = peak = 0
    for _at, delta in sorted(points, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _metric(value: float | None, missing: str) -> dict[str, Any]:
    return (
        {"status": "available", "seconds": value}
        if value is not None
        else {"status": "unavailable", "reason": missing}
    )


def _aggregate_run(
    run: dict[str, Any],
    tickets: dict[tuple[str, str], dict[str, Any]],
    events: dict[tuple[str, str], list[dict[str, Any]]],
    incomplete_boards: set[str],
) -> dict[str, Any]:
    run_id = _text(run.get("run_id"), "run_id")
    pair_id = _text(run.get("pair_id"), "pair_id")
    arm = _text(run.get("arm"), "arm", limit=16)
    if arm not in _ARMS:
        raise CaseStudyError("arm must be sequential or parallel")
    refs = run.get("tickets")
    if not isinstance(refs, list) or not refs or len(refs) > _MAX_TICKETS:
        raise CaseStudyError(f"run {run_id} tickets must be a non-empty bounded list")
    refs = [_ticket_ref(item, run_id) for item in refs]
    run_events = _manifest_events(run)
    released = next(
        (
            _time(item["occurred_at"])
            for item in run_events
            if item["kind"] == "arm_released"
        ),
        None,
    )
    finished = next(
        (
            _time(item["occurred_at"])
            for item in reversed(run_events)
            if item["kind"] == "arm_finished"
        ),
        None,
    )
    ticket_rows = []
    ownership_intervals: list[tuple[datetime, datetime]] = []
    missing: list[str] = []
    if released is None:
        missing.append(f"{run_id}: required arm_released event was not observed")
    if finished is None:
        missing.append(f"{run_id}: required arm_finished event was not observed")
    elif released is not None and finished < released:
        missing.append(f"{run_id}: arm lifecycle event order is invalid")
    for ref in refs:
        key = (ref["board_id"], ref["ticket_id"])
        ticket = tickets.get(key, {})
        scoped_study_events = [
            item
            for item in run_events
            if item.get("ticket_id") == ref["ticket_id"]
            and item.get("board_id") in {None, ref["board_id"]}
        ]
        timeline, phases = _timeline_for_ticket(
            ticket, events.get(key, []), scoped_study_events
        )
        if not ticket and not timeline:
            missing.append(f"{ref['board_id']}/{ref['ticket_id']}: no source record")
        observed_manifest_phases = {
            phase
            for item in scoped_study_events
            if (phase := _event_phase(item)) is not None
        }
        if (
            ref["board_id"] in incomplete_boards
            and not {
                "started",
                "submitted",
                "closed",
            }
            <= observed_manifest_phases
        ):
            missing.append(f"{ref['board_id']}: incomplete journal window")
        started, submitted, closed = (
            phases["started"],
            phases["submitted"],
            phases["closed"],
        )
        required_phases = (
            "eligible",
            "started",
            "submitted",
            "review_started",
            "closed",
        )
        for phase in required_phases:
            if phases[phase] is None:
                missing.append(
                    f"{ref['board_id']}/{ref['ticket_id']}: "
                    f"required {phase} lifecycle event was not observed"
                )
        observed_required = [phases[phase] for phase in required_phases]
        if all(observed_required) and observed_required != sorted(observed_required):
            missing.append(
                f"{ref['board_id']}/{ref['ticket_id']}: "
                "required lifecycle event order is invalid"
            )
        if started and submitted:
            ownership_intervals.append((started, submitted))
        ticket_rows.append(
            {
                **ref,
                "status": str(ticket.get("status") or "not_observed"),
                "timeline": timeline,
                "metrics": {
                    "queue_time": _metric(
                        _seconds(phases["eligible"], started),
                        "eligibility or start was not observed",
                    ),
                    "ownership_time": _metric(
                        _seconds(started, submitted),
                        "start or submission was not observed",
                    ),
                    "review_queue_time": _metric(
                        _seconds(submitted, phases["review_started"]),
                        "review start was not observed",
                    ),
                    "review_work_time": _metric(
                        _seconds(phases["review_started"], closed),
                        "review start or accepted close was not observed",
                    ),
                    "flow_time": _metric(
                        _seconds(phases["eligible"], closed),
                        "eligibility or accepted close was not observed",
                    ),
                },
                "accepted": closed is not None,
            }
        )
    makespan = _seconds(released, finished)
    complete = (
        all(item["accepted"] for item in ticket_rows)
        and makespan is not None
        and not missing
    )
    configured = run.get("configured_concurrency")
    configured = configured if type(configured) is int and configured > 0 else None
    return {
        "run_id": run_id,
        "pair_id": pair_id,
        "arm": arm,
        "match_digest": _digest(run.get("match_digest"), "match_digest"),
        "configured_concurrency": configured,
        "observed_peak_concurrency": _peak_concurrency(ownership_intervals),
        "released_at": _iso(released),
        "finished_at": _iso(finished),
        "wall_time": _metric(makespan, "arm_released or arm_finished was not observed"),
        "tickets": ticket_rows,
        "accepted_count": sum(item["accepted"] for item in ticket_rows),
        "complete": complete,
        "data_quality": "complete" if complete else "incomplete",
        "missing": sorted(set(missing)),
        "cost": {
            "status": "unavailable",
            "reason": "No source-backed cost telemetry was supplied.",
        },
    }


def _compare(pair_id: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    sequential_runs = [item for item in runs if item["arm"] == "sequential"]
    parallel_runs = [item for item in runs if item["arm"] == "parallel"]
    sequential = sequential_runs[0] if len(sequential_runs) == 1 else None
    parallel = parallel_runs[0] if len(parallel_runs) == 1 else None
    reasons = []
    if len(sequential_runs) != 1:
        reasons.append(
            f"Pair requires exactly one sequential arm; found {len(sequential_runs)}."
        )
    if len(parallel_runs) != 1:
        reasons.append(
            f"Pair requires exactly one parallel arm; found {len(parallel_runs)}."
        )
    if sequential and parallel:
        if sequential["match_digest"] != parallel["match_digest"]:
            reasons.append("Arm match digests differ.")
        if [item["template_digest"] for item in sequential["tickets"]] != [
            item["template_digest"] for item in parallel["tickets"]
        ]:
            reasons.append("Ticket templates or ordering differ.")
        for label, run in (("Sequential", sequential), ("Parallel", parallel)):
            if not run["complete"]:
                missing = "; ".join(run["missing"]) or "unknown evidence gap"
                reasons.append(
                    f"{label} arm is incomplete; required evidence missing: {missing}."
                )
    seq_seconds = sequential and sequential["wall_time"].get("seconds")
    par_seconds = parallel and parallel["wall_time"].get("seconds")
    if (
        not isinstance(seq_seconds, (int, float))
        or not isinstance(par_seconds, (int, float))
        or par_seconds <= 0
    ):
        reasons.append("Both arms require positive source-backed wall time.")
    comparable = not reasons
    return {
        "pair_id": pair_id,
        "status": "comparable" if comparable else "incomparable",
        "reasons": sorted(set(reasons)),
        "sequential_run_id": sequential["run_id"] if sequential else None,
        "parallel_run_id": parallel["run_id"] if parallel else None,
        "speedup": (
            {
                "status": "available",
                "ratio": round(float(seq_seconds) / float(par_seconds), 3),
            }
            if comparable
            else {
                "status": "unavailable",
                "reason": "Matched complete arms are unavailable.",
            }
        ),
    }


def aggregate_case_study(
    board_rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate one preregistered study from actual board and manifest events."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CaseStudyError("case-study manifest schema_version must be 1")
    study_id = _text(manifest.get("study_id"), "study_id")
    manifest_digest = _digest(manifest.get("manifest_digest"), "manifest_digest")
    raw_runs = manifest.get("runs")
    if not isinstance(raw_runs, list) or not 2 <= len(raw_runs) <= _MAX_RUNS:
        raise CaseStudyError("manifest runs must contain 2-32 arms")
    tickets, events, incomplete_boards = _source_records(board_rows)
    runs = [_aggregate_run(run, tickets, events, incomplete_boards) for run in raw_runs]
    if len({item["run_id"] for item in runs}) != len(runs):
        raise CaseStudyError("run_id values must be unique")
    comparisons = [
        _compare(pair_id, [item for item in runs if item["pair_id"] == pair_id])
        for pair_id in sorted({item["pair_id"] for item in runs})
    ]
    return {
        "schema_version": 1,
        "study_id": study_id,
        "manifest_digest": manifest_digest,
        "status": "comparable"
        if comparisons and all(item["status"] == "comparable" for item in comparisons)
        else "incomparable",
        "runs": runs,
        "comparisons": comparisons,
        "source": "preregistered manifest + board journal/snapshot",
    }


def aggregate_case_studies(board_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate deduplicated manifests carried by private Fleet board reads."""
    manifests: dict[str, dict[str, Any]] = {}
    for row in board_rows:
        values = row.get("case_study_manifests") if isinstance(row, dict) else None
        for manifest in values if isinstance(values, list) else []:
            if isinstance(manifest, dict) and isinstance(
                manifest.get("manifest_digest"), str
            ):
                manifests.setdefault(manifest["manifest_digest"], manifest)
    return [
        aggregate_case_study(board_rows, manifest) for manifest in manifests.values()
    ]


def _alias(key: bytes, kind: str, source: str) -> str:
    digest = hmac.new(
        key, f"case-study-v1\0{kind}\0{source}".encode(), hashlib.sha256
    ).digest()
    prefix = "work" if kind == "ticket" else "project"
    return f"{prefix}-{b32encode(digest[:10]).decode().lower().rstrip('=')}"


def _count_bucket(value: int) -> tuple[str, bool]:
    if value == 0:
        return "none", False
    if value < 5:
        return "none", True
    if value < 10:
        return "few", False
    if value < 25:
        return "several", False
    return "many", False


def _duration_bucket(metric: dict[str, Any]) -> str:
    seconds = metric.get("seconds") if isinstance(metric, dict) else None
    if not isinstance(seconds, (int, float)):
        return "unavailable"
    if seconds < 300:
        return "under_5_minutes"
    if seconds < 900:
        return "under_15_minutes"
    if seconds < 3_600:
        return "under_1_hour"
    if seconds < 14_400:
        return "under_4_hours"
    return "4_hours_or_more"


def _speedup_bucket(metric: dict[str, Any]) -> str:
    ratio = metric.get("ratio") if isinstance(metric, dict) else None
    if not isinstance(ratio, (int, float)):
        return "unavailable"
    if ratio < 0.9:
        return "slower"
    if ratio < 1.1:
        return "similar"
    if ratio < 2:
        return "faster"
    return "at_least_twice_as_fast"


def project_public_case_study(
    private: dict[str, Any], alias_key: bytes
) -> dict[str, Any]:
    """Create a closed, coarse projection with no source identifiers or times."""
    if not isinstance(alias_key, bytes) or len(alias_key) < 32:
        raise CaseStudyError("public alias key must contain at least 256 bits")
    study_id = _text(private.get("study_id"), "study_id")
    comparisons = []
    runs_by_id = {
        item["run_id"]: item
        for item in private.get("runs", [])
        if isinstance(item, dict)
    }
    for comparison in private.get("comparisons", []):
        if not isinstance(comparison, dict):
            continue
        public_runs = []
        for arm, key in (
            ("sequential", "sequential_run_id"),
            ("parallel", "parallel_run_id"),
        ):
            run = runs_by_id.get(comparison.get(key))
            if not run:
                continue
            count, suppressed = _count_bucket(len(run.get("tickets", [])))
            public_run: dict[str, Any] = {
                "arm": arm,
                "work": count,
                "suppressed": suppressed,
                "wall_time": _duration_bucket(run.get("wall_time", {})),
                "peak_concurrency": _count_bucket(
                    int(run.get("observed_peak_concurrency") or 0)
                )[0],
                "complete": bool(run.get("complete")),
            }
            if not suppressed:
                public_run["work_items"] = [
                    {
                        "alias": _alias(
                            alias_key,
                            "ticket",
                            f"{study_id}\0{item['board_id']}\0{item['ticket_id']}",
                        ),
                        "state": "completed" if item.get("accepted") else "ended",
                        "flow_time": _duration_bucket(
                            item.get("metrics", {}).get("flow_time", {})
                        ),
                    }
                    for item in run.get("tickets", [])
                ]
            public_runs.append(public_run)
        comparisons.append(
            {
                "alias": _alias(
                    alias_key, "pair", f"{study_id}\0{comparison.get('pair_id')}"
                ),
                "status": comparison.get("status")
                if comparison.get("status") in {"comparable", "incomparable"}
                else "incomparable",
                "speedup": _speedup_bucket(comparison.get("speedup", {})),
                "runs": public_runs,
            }
        )
    result = {
        "schema_version": 1,
        "mode": "public",
        "study": _alias(alias_key, "study", study_id),
        "status": private.get("status")
        if private.get("status") in {"comparable", "incomparable"}
        else "incomparable",
        "comparisons": comparisons,
    }
    _assert_public_projection(result, private)
    return result


def _assert_public_projection(public: dict[str, Any], private: dict[str, Any]) -> None:
    encoded = json.dumps(public, sort_keys=True, separators=(",", ":"))
    if (
        _ISO_RE.search(encoded)
        or "/" in encoded
        or "TK-" in encoded
        or "AI-" in encoded
        or "PR-" in encoded
    ):
        raise CaseStudyError(
            "public case-study projection contains a forbidden source value"
        )
    aliases = []
    if isinstance(public.get("study"), str):
        aliases.append(public["study"])
    for comparison in public.get("comparisons", []):
        aliases.append(comparison.get("alias"))
        for run in comparison.get("runs", []):
            aliases.extend(item.get("alias") for item in run.get("work_items", []))
    if any(
        not isinstance(value, str) or not _PUBLIC_ALIAS_RE.fullmatch(value)
        for value in aliases
    ):
        raise CaseStudyError("public case-study alias is invalid")
    private_ids = [str(private.get("study_id") or "")]
    for run in private.get("runs", []):
        private_ids.extend(
            (str(run.get("run_id") or ""), str(run.get("pair_id") or ""))
        )
        for ticket in run.get("tickets", []):
            private_ids.extend(
                (str(ticket.get("board_id") or ""), str(ticket.get("ticket_id") or ""))
            )
    if any(value and value in encoded for value in private_ids):
        raise CaseStudyError("public case-study projection leaked a source identifier")
