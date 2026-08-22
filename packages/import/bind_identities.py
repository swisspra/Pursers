#!/usr/bin/env python3
"""Operator-reviewed binding of legacy agent records to central principals."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

if __package__:
    from .native_import import (
        CENTRAL_ID,
        _append_event,
        _replace,
        board_path,
        file_hash,
        load_json,
        manifest_path,
    )
    from .transactional_sqlite import TransactionalSQLiteStore
else:  # source-checkout execution
    from native_import import (
        CENTRAL_ID,
        _append_event,
        _replace,
        board_path,
        file_hash,
        load_json,
        manifest_path,
    )
    from transactional_sqlite import TransactionalSQLiteStore


def agent_id(board_id: str, principal_id: str, agent_name: str) -> str:
    logical = json.dumps(
        [board_id, principal_id, agent_name], separators=(",", ":")
    )
    return "AI-" + hashlib.sha256(logical.encode()).hexdigest()


def generate_identity_material(
    central_root: Path,
    board_id: str,
    *,
    quarantined_agent_record_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a private worksheet and directly editable decision template."""
    store = TransactionalSQLiteStore(central_root.resolve())
    board = store.load(board_path(store, board_id), dict)
    agents = board.get("legacy_import", {}).get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("legacy identity inventory is invalid")
    names = [str(item.get("agent_name") or "") for item in agents.values()]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    rows: list[dict[str, Any]] = []
    for record_id, item in sorted(agents.items()):
        if not isinstance(item, dict) or item.get("binding_status") != "legacy_unbound":
            continue
        name = str(item.get("agent_name") or "")
        rows.append(
            {
                "record_id": record_id,
                "agent_name": name,
                "binding_key": f"record:{record_id}",
                "duplicate_name": name in duplicates,
                "currently_imported": True,
                "decision": "PENDING",
            }
        )
    known = {row["record_id"] for row in rows}
    for record_id in sorted(set(quarantined_agent_record_ids or [])):
        if record_id in known:
            continue
        rows.append(
            {
                "record_id": record_id,
                "agent_name": None,
                "binding_key": f"record:{record_id}",
                "duplicate_name": None,
                "currently_imported": False,
                "decision_required_if_restored": True,
                "decision": "PENDING",
            }
        )
    rows.sort(key=lambda item: str(item["record_id"]))
    worksheet = {
        "schema_version": 1,
        "board_id": board_id,
        "entry_count": len(rows),
        "entries": rows,
    }
    worksheet["worksheet_sha256"] = hashlib.sha256(
        json.dumps(worksheet, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    template = {
        "schema_version": 1,
        "board_id": board_id,
        "identity_worksheet_sha256": worksheet["worksheet_sha256"],
        "entry_count": len(rows),
        "bindings": {row["binding_key"]: "PENDING" for row in rows},
    }
    return worksheet, template


def _decision_for(
    bindings: dict[str, str], record_id: str, agent_name: str, duplicate: bool
) -> str | None:
    exact = bindings.get(f"record:{record_id}")
    if exact is not None:
        return exact
    return None if duplicate else bindings.get(agent_name)


def _ticket_identity_fields(
    ticket: dict[str, Any], unique_ids: dict[str, str]
) -> None:
    fields = {
        "created_by": "created_by_agent_id",
        "claimed_by": "claimed_by_agent_id",
        "assigned_to": "assigned_to_agent_id",
        "reviewed_by": "reviewed_by_agent_id",
        "submitted_by": "submitted_by_agent_id",
        "last_abandoned_by": "last_abandoned_by_agent_id",
    }
    for display_field, identity_field in fields.items():
        display = ticket.get(display_field)
        if isinstance(display, str) and display in unique_ids:
            ticket[identity_field] = unique_ids[display]


def bind_identities(
    central_root: Path, board_id: str, bindings_path: Path
) -> dict[str, Any]:
    bindings_raw = load_json(bindings_path, {})
    bindings = bindings_raw.get("bindings", bindings_raw)
    if not isinstance(bindings, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in bindings.items()
    ):
        raise ValueError("bindings.json must map names or record:<id> to principal_id or RETIRE")
    if any(value != "RETIRE" and not CENTRAL_ID.fullmatch(value) for value in bindings.values()):
        raise ValueError("binding decisions must be RETIRE or a supported principal identifier")
    decisions_hash = file_hash(bindings_path)
    store = TransactionalSQLiteStore(central_root.resolve())
    with store.transaction():
        manifest = store.load(manifest_path(store, board_id), dict)
        if manifest.get("status") != "complete":
            raise ValueError("native import must be complete before identity binding")
        board_doc_path = board_path(store, board_id)
        board = store.load(board_doc_path, dict)
        legacy = board.get("legacy_import", {})
        agents = legacy.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("legacy agents are missing")
        binding_state = legacy.setdefault(
            "identity_binding",
            {"applied": {}, "decision_file_hashes": [], "journal_event_ids": []},
        )
        if decisions_hash in binding_state["decision_file_hashes"]:
            report = legacy.get("identity_binding_report", {})
            return {"status": "noop", "decisions_hash": decisions_hash, **report}

        names = [str(item.get("agent_name") or "") for item in agents.values()]
        duplicates = {name for name, count in Counter(names).items() if count > 1}
        original_ambiguous_hashes = {
            str(value)
            for value in legacy.get("ambiguous_agent_name_hashes", [])
        }
        duplicates.update(
            name
            for name in names
            if hashlib.sha256(name.encode("utf-8")).hexdigest()
            in original_ambiguous_hashes
        )
        newly_applied: list[tuple[str, str, str]] = []
        for record_id, item in agents.items():
            name = str(item.get("agent_name") or "")
            decision = _decision_for(bindings, record_id, name, name in duplicates)
            previous = binding_state["applied"].get(record_id)
            if previous is not None:
                if decision is not None and decision != previous["decision"]:
                    raise ValueError(f"binding is immutable for legacy record {record_id}")
                continue
            if decision is None:
                continue
            if not decision.strip():
                raise ValueError(f"empty binding for legacy record {record_id}")
            if decision == "RETIRE":
                item.update(
                    {
                        "binding_status": "retired",
                        "read_only_historical": True,
                        "principal_id": None,
                        "agent_id": None,
                    }
                )
                status = "RETIRE"
            else:
                identity = agent_id(board_id, decision, name)
                item.update(
                    {
                        "binding_status": "bound",
                        "legacy_self_asserted": False,
                        "principal_id": decision,
                        "agent_id": identity,
                    }
                )
                board.setdefault("members", {})[identity] = {
                    "agent_id": identity,
                    "agent_name": name,
                    "principal_id": decision,
                    "role": "worker",
                    "lifecycle_status": "historical",
                    "legacy_bound": True,
                    "legacy_record_id": record_id,
                }
                status = "BOUND"
            binding_state["applied"][record_id] = {
                "agent_name": name,
                "decision": decision,
                "status": status,
            }
            newly_applied.append((record_id, name, status))

        bound_by_name: dict[str, list[str]] = {}
        for item in agents.values():
            if item.get("binding_status") == "bound":
                bound_by_name.setdefault(str(item.get("agent_name")), []).append(
                    str(item["agent_id"])
                )
        unique_ids = {
            name: identities[0]
            for name, identities in bound_by_name.items()
            if len(identities) == 1 and name not in duplicates
        }
        for ticket in board.get("tickets", {}).values():
            _ticket_identity_fields(ticket, unique_ids)

        for record_id, _name, status in newly_applied:
            event = _append_event(
                store,
                board_id,
                actor="identity-binder",
                payload_ref=f"board://{board_id}/identity/{record_id}",
                memory_id_value=f"identity_binding:{record_id}",
                fixture_provenance=f"operator identity decision {status}",
            )
            binding_state["journal_event_ids"].append(event["id"])
        binding_state["decision_file_hashes"].append(decisions_hash)

        report_rows = []
        for record_id, item in sorted(agents.items()):
            status = str(item.get("binding_status") or "legacy_unbound")
            report_rows.append(
                {
                    "legacy_record_id": record_id,
                    "agent_name": item.get("agent_name"),
                    "status": "UNMAPPED" if status == "legacy_unbound" else status.upper(),
                    "duplicate_name_requires_record_key": str(item.get("agent_name")) in duplicates,
                }
            )
        report = {
            "bound": sum(row["status"] == "BOUND" for row in report_rows),
            "retired": sum(row["status"] == "RETIRED" for row in report_rows),
            "unmapped": sum(row["status"] == "UNMAPPED" for row in report_rows),
            "rows": report_rows,
        }
        legacy["identity_binding_report"] = report
        _replace(store, board_doc_path, board)
    return {
        "status": "complete",
        "decisions_hash": decisions_hash,
        "newly_applied": len(newly_applied),
        **report,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("central_root", type=Path)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--bindings", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            bind_identities(args.central_root, args.board_id, args.bindings),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
