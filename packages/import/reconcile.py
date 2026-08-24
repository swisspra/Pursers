#!/usr/bin/env python3
"""Masked quarantine worksheet generation and operator-decision application."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

if __package__:
    from .native_import import (
        _append_event,
        _replace,
        board_path,
        file_hash,
        load_json,
        map_archive_memory,
        map_memory,
        map_state,
        map_ticket,
        memory_id,
        redact_record,
        safe_report_id,
        source_archive,
        source_memories,
        source_tickets,
    )
    from .transactional_sqlite import TransactionalSQLiteStore
else:  # source-checkout execution
    from native_import import (
        _append_event,
        _replace,
        board_path,
        file_hash,
        load_json,
        map_archive_memory,
        map_memory,
        map_state,
        map_ticket,
        memory_id,
        redact_record,
        safe_report_id,
        source_archive,
        source_memories,
        source_tickets,
    )
    from transactional_sqlite import TransactionalSQLiteStore


ACTIONS = {"accept-as-is", "redact-span", "drop"}
SIGNED_REVIEW_STATUS = "REVIEWED-SIGNED-READY"
POLICY_DECISIONS_STATUS = "POLICY-AUTO-DECIDED"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_secure_json(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_key(item: dict[str, Any]) -> str:
    identity = {
        "record_type": item["record_type"],
        "record_id": item["record_id"],
        "field_identity": item.get("field_identity", item["field"]),
        "rules": item["rules"],
    }
    return "QR-" + canonical_hash(identity)[:20]


def _decision_map(
    decisions_doc: dict[str, Any], worksheet: dict[str, Any], board_id: str
) -> tuple[dict[str, str], str]:
    """Validate legacy or operator-signed decisions without rewriting the signed file."""
    if decisions_doc.get("worksheet_sha256") != worksheet.get("worksheet_sha256"):
        raise ValueError("decisions file does not match worksheet_sha256")

    expected_rows = {
        item["record_key"]: item for item in worksheet.get("entries", [])
    }
    if len(expected_rows) != len(worksheet.get("entries", [])):
        raise ValueError("worksheet contains duplicate record_key values")

    if "entries" in decisions_doc:
        status = decisions_doc.get("status")
        if status not in {SIGNED_REVIEW_STATUS, POLICY_DECISIONS_STATUS}:
            raise ValueError(
                "entry decisions status must be REVIEWED-SIGNED-READY or POLICY-AUTO-DECIDED"
            )
        if decisions_doc.get("board_id") != board_id:
            raise ValueError("entry decisions board_id does not match target board")
        rows = decisions_doc.get("entries")
        if not isinstance(rows, list):
            raise ValueError("signed review entries must be a list")
        if decisions_doc.get("entry_count") != len(rows):
            raise ValueError("signed review entry_count does not match entries")
        if status == SIGNED_REVIEW_STATUS:
            review_metadata = decisions_doc.get("review_metadata")
            if (
                not isinstance(review_metadata, dict)
                or not isinstance(review_metadata.get("reviewed_at"), str)
                or not review_metadata["reviewed_at"].strip()
            ):
                raise ValueError("signed review requires review_metadata.reviewed_at")
            decisions_format = "signed-review-entries-v1"
        else:
            policy_sha256 = decisions_doc.get("policy_sha256")
            if not isinstance(policy_sha256, str) or not SHA256_HEX.fullmatch(
                policy_sha256
            ):
                raise ValueError("policy decisions require policy_sha256")
            decisions_format = "policy-auto-decisions-v1"
        action_field = "decision"
    else:
        rows = decisions_doc.get("decisions")
        if not isinstance(rows, list):
            raise ValueError("decisions must be a list")
        action_field = "action"
        decisions_format = "legacy-decisions-v1"

    decision_map: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("each decision row must be an object")
        record_key = item.get("record_key")
        if not isinstance(record_key, str) or not record_key:
            raise ValueError("each decision row requires record_key")
        if record_key in decision_map:
            raise ValueError(f"duplicate decision record_key: {record_key}")
        if decisions_format in {
            "signed-review-entries-v1",
            "policy-auto-decisions-v1",
        }:
            expected = expected_rows.get(record_key)
            if expected is not None:
                for field in (
                    "record_type",
                    "record_id",
                    "field",
                    "field_identity",
                    "rules",
                ):
                    if item.get(field) != expected.get(field):
                        raise ValueError(
                            f"signed review identity does not match worksheet for {record_key}"
                        )
        decision_map[record_key] = item.get(action_field)

    if set(decision_map) != set(expected_rows):
        raise ValueError("decisions must cover every worksheet record exactly once")
    if any(action not in ACTIONS for action in decision_map.values()):
        raise ValueError("action must be accept-as-is, redact-span, or drop")
    return decision_map, decisions_format


def generate_worksheet(
    central_root: Path, board_id: str, output: Path | None = None
) -> dict[str, Any]:
    store = TransactionalSQLiteStore(central_root.resolve())
    board = store.load(board_path(store, board_id), dict)
    if not board:
        raise ValueError("board not found")
    report = board.get("legacy_import", {}).get("quarantine", [])
    entries = []
    for item in report:
        rules = sorted(str(rule) for rule in item.get("rules", []))
        entries.append(
            {
                "record_key": _record_key(item),
                "record_type": item["record_type"],
                "record_id": item["record_id"],
                "field": item["field"],
                "field_identity": item.get("field_identity"),
                "rules": rules,
                "context_free_preview": " ".join(
                    f"[MATCHED-SPAN-MASKED:{rule.upper()}]" for rule in rules
                ),
                "suggested_action": "redact-span",
                "decision": "PENDING",
            }
        )
    entries.sort(key=lambda row: (row["record_type"], row["record_id"], row["field"]))
    worksheet = {
        "schema_version": 1,
        "board_id": board_id,
        "entry_count": len(entries),
        "entries": entries,
    }
    worksheet["worksheet_sha256"] = canonical_hash(worksheet)
    if output is not None:
        write_secure_json(output, worksheet)
    return worksheet


def _raw_record(source: Path, record_type: str, record_id: str) -> Any:
    candidates: list[tuple[str, Any]] = []
    if record_type == "memory":
        candidates = [
            (str(item.get("id")), item) for item in source_memories(source)
        ]
    elif record_type == "archive":
        candidates = [
            (map_archive_memory(item)["memory_id"], item)
            for item in source_archive(source)
        ]
    elif record_type == "agent":
        candidates = list(load_json(source / "agents.json", {}).items())
    elif record_type == "ticket":
        candidates = [
            (str(item.get("id") or item.get("ticket_id")), item)
            for item in source_tickets(source)
        ]
    elif record_type == "state":
        candidates = (
            [("state", load_json(source / "state.json", {}))]
            if record_id == "state"
            else []
        )
    elif record_type == "ticket_artifact":
        root = source / "tickets"
        candidates = [
            (
                path.relative_to(root).as_posix(),
                path.read_text(encoding="utf-8"),
            )
            for path in sorted(root.rglob("*.md"))
        ]
    matches = [
        value
        for candidate_id, value in candidates
        if candidate_id == record_id or safe_report_id(candidate_id) == record_id
    ]
    if len(matches) > 1:
        raise ValueError("masked source record identifier is ambiguous")
    return matches[0] if matches else None


def _apply_record(
    board: dict[str, Any],
    record_type: str,
    record_id: str,
    value: Any,
    action: str,
    record_keys: list[str],
) -> None:
    if record_type == "memory":
        target_id = memory_id(record_id)
        board["memories"] = [
            item for item in board.get("memories", []) if item.get("memory_id") != target_id
        ]
        if action != "drop":
            normalized = copy.deepcopy(value)
            normalized["id"] = record_id
            mapped = map_memory(normalized)
            mapped["quarantine_record_keys"] = list(record_keys)
            mapped["tags"] = list(dict.fromkeys([*mapped.get("tags", []), *record_keys]))
            board["memories"].append(mapped)
        return
    if record_type == "archive":
        board["memories"] = [
            item
            for item in board.get("memories", [])
            if item.get("memory_id") != record_id
        ]
        if action != "drop":
            mapped = map_archive_memory(value)
            mapped["quarantine_record_keys"] = list(record_keys)
            mapped["tags"] = list(
                dict.fromkeys([*mapped.get("tags", []), *record_keys])
            )
            board["memories"].append(mapped)
        return
    if record_type == "agent":
        agents = board["legacy_import"].setdefault("agents", {})
        agents.pop(record_id, None)
        if action != "drop":
            value.update(
                {
                    "legacy_self_asserted": True,
                    "principal_id": None,
                    "agent_id": None,
                    "binding_status": "legacy_unbound",
                    "quarantine_record_keys": list(record_keys),
                }
            )
            agents[record_id] = value
        return
    if record_type == "ticket":
        board.setdefault("tickets", {}).pop(record_id, None)
        index_id = "QUARANTINE-INDEX-" + hashlib.sha256(
            f"ticket:{record_id}".encode()
        ).hexdigest()[:16]
        board.setdefault("memories", [])[:] = [
            item for item in board["memories"] if item.get("memory_id") != index_id
        ]
        if action != "drop":
            normalized = copy.deepcopy(value)
            normalized.pop("ticket_id", None)
            normalized["id"] = record_id
            mapped = map_ticket(normalized)
            mapped["quarantine_record_keys"] = list(record_keys)
            board["tickets"][record_id] = mapped
            board["memories"].append(
                {
                    "schema_version": 2,
                    "memory_id": index_id,
                    "title": f"Quarantine lookup for {record_id}",
                    "content": (
                        "Content-free reconciliation index for imported ticket "
                        f"{record_id}."
                    ),
                    "scope": "project",
                    "author_principal_id": "legacy_unbound",
                    "author_agent_id": None,
                    "author_agent_name": "quarantine-reconciler",
                    "memory_type": "context",
                    "tags": list(record_keys),
                    "related_files": [],
                    "related_tickets": [record_id],
                    "priority": 0,
                    "pinned": False,
                    "quarantine_record_keys": list(record_keys),
                    "migration_provenance": "quarantine-reconciliation-index",
                }
            )
        return
    if record_type == "state":
        board["state"] = (
            {}
            if action == "drop"
            else map_state(value, board["legacy_import"]["owner_provisioned_at"])
        )
        return
    if record_type == "ticket_artifact":
        artifacts = board["legacy_import"].setdefault("ticket_artifacts", {})
        artifacts.pop(record_id, None)
        if action != "drop":
            artifacts[record_id] = value
        return
    raise ValueError(f"unsupported quarantine record type: {record_type}")


def apply_decisions(
    source: Path,
    central_root: Path,
    board_id: str,
    worksheet_path: Path,
    decisions_path: Path,
) -> dict[str, Any]:
    source = source.resolve()
    worksheet = load_json(worksheet_path, {})
    decisions_doc = load_json(decisions_path, {})
    decision_map, decisions_format = _decision_map(
        decisions_doc, worksheet, board_id
    )
    decisions_hash = file_hash(decisions_path)
    store = TransactionalSQLiteStore(central_root.resolve())
    with store.transaction():
        target = board_path(store, board_id)
        board = store.load(target, dict)
        if not board:
            raise ValueError("board not found")
        reconciliation = board["legacy_import"].setdefault(
            "reconciliation", {"runs": [], "applied_record_keys": {}}
        )
        if any(run["decisions_sha256"] == decisions_hash for run in reconciliation["runs"]):
            return {
                "status": "noop",
                "decisions_sha256": decisions_hash,
                "changes": 0,
            }
        worksheet_rows = {item["record_key"]: item for item in worksheet["entries"]}
        actions_by_record: dict[tuple[str, str], set[str]] = {}
        for key, action in decision_map.items():
            row = worksheet_rows[key]
            actions_by_record.setdefault(
                (row["record_type"], row["record_id"]), set()
            ).add(action)
        if any(len(actions) != 1 for actions in actions_by_record.values()):
            raise ValueError("all fields of one top-level record must use the same action")

        audit_rows = []
        for (record_type, record_id), actions in sorted(actions_by_record.items()):
            action = next(iter(actions))
            keys = sorted(
                key
                for key, row in worksheet_rows.items()
                if row["record_type"] == record_type and row["record_id"] == record_id
            )
            for key in keys:
                previous = reconciliation["applied_record_keys"].get(key)
                if previous is not None and previous != action:
                    raise ValueError(f"reconciliation decision is immutable for {key}")
            raw = _raw_record(source, record_type, record_id)
            if raw is None:
                raise ValueError(f"source record not found: {record_type}/{record_id}")
            if action == "redact-span" and record_type == "state":
                if not isinstance(raw, dict):
                    raise ValueError("state source record must be an object")
                # Preserve top-level structural keys for map_state(), which
                # replaces sensitive keys with their stable safe hash. Nested
                # keys and all values still receive normal redaction.
                value = {
                    key: redact_record(child) for key, child in raw.items()
                }
            else:
                value = (
                    redact_record(raw)
                    if action == "redact-span"
                    else copy.deepcopy(raw)
                )
            _apply_record(board, record_type, record_id, value, action, keys)
            event = _append_event(
                store,
                board_id,
                actor="quarantine-reconciler",
                payload_ref=f"board://{board_id}/quarantine/{keys[0]}",
                memory_id_value=f"quarantine_decision:{keys[0]}",
                fixture_provenance=f"operator quarantine decision {action}",
            )
            for key in keys:
                reconciliation["applied_record_keys"][key] = action
            audit_rows.append(
                {
                    "record_type": record_type,
                    "record_id": record_id,
                    "record_keys": keys,
                    "action": action,
                    "journal_event_id": event["id"],
                }
            )
        reconciliation["runs"].append(
            {
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "decisions_sha256": decisions_hash,
                "decisions_format": decisions_format,
                **(
                    {"policy_sha256": decisions_doc["policy_sha256"]}
                    if decisions_format == "policy-auto-decisions-v1"
                    else {}
                ),
                "rows": audit_rows,
            }
        )
        _replace(store, target, board)
    return {
        "status": "complete",
        "decisions_sha256": decisions_hash,
        "changes": len(audit_rows),
        "actions": {
            action: sum(row["action"] == action for row in audit_rows)
            for action in sorted(ACTIONS)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    worksheet_parser = subparsers.add_parser("worksheet")
    worksheet_parser.add_argument("central_root", type=Path)
    worksheet_parser.add_argument("--board-id", required=True)
    worksheet_parser.add_argument("--output", required=True, type=Path)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("source", type=Path)
    apply_parser.add_argument("central_root", type=Path)
    apply_parser.add_argument("--board-id", required=True)
    apply_parser.add_argument("--worksheet", required=True, type=Path)
    apply_parser.add_argument("--decisions", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "worksheet":
        result = generate_worksheet(args.central_root, args.board_id, args.output)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "entry_count": result["entry_count"],
                    "worksheet_sha256": result["worksheet_sha256"],
                    "output": str(args.output.resolve()),
                    "matched_content_emitted": False,
                },
                sort_keys=True,
            )
        )
    else:
        print(
            json.dumps(
                apply_decisions(
                    args.source,
                    args.central_root,
                    args.board_id,
                    args.worksheet,
                    args.decisions,
                ),
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
