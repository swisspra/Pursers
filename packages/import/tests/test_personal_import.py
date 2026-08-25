"""End-user Personal import safety, recovery, and rollback evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import personal_import as personal_import_module
from native_import import archive_record_key, board_path, canonical_db_hash
from personal_import import (
    _destination_lock_path,
    archive_backfill,
    generate_policy_decisions,
    review_import,
    retry_import,
    rollback_import,
    stable_install_state,
    start_import,
    status_import,
    tree_state,
)
from prepare_apply_rehearsal import _tree_state as snapshot_tree_state
from prepare_apply_rehearsal import prepare as prepare_frozen_copy
from reconcile import _decision_map
from safe_tree import MAX_FILE_BYTES
from transactional_sqlite import TransactionalSQLiteStore


FIXTURE = Path(__file__).parent / "fixtures" / "source" / ".agent-mem"
ROOT = Path(__file__).parent.parent
CRASH_WORKER = Path(__file__).parent / "personal_import_crash_worker.py"
OWNER_PRINCIPAL = "principal-synthetic"
OWNER_AGENT = "agent-synthetic"
BOARD_ID = "board-synthetic"


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "legacy" / ".agent-mem"
    source.parent.mkdir()
    shutil.copytree(FIXTURE, source)
    (source / ".board.lock").write_bytes(b"")
    os.chmod(source, 0o700)
    os.chmod(source / ".board.lock", 0o600)
    return source


def make_stable_install(tmp_path: Path) -> Path:
    root = tmp_path / "Cellar" / "onboard-memory" / "4.0.4"
    executable = root / "libexec" / "bin" / "onboard-memory-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(executable, 0o755)
    command = root / "bin" / "onboard-memory-mcp"
    command.parent.mkdir()
    command.symlink_to("../libexec/bin/onboard-memory-mcp")
    active_command = tmp_path / "bin" / "onboard-memory-mcp"
    active_command.parent.mkdir()
    active_command.symlink_to(
        "../Cellar/onboard-memory/4.0.4/libexec/bin/onboard-memory-mcp"
    )
    (root / "INSTALL_RECEIPT.json").write_text(
        json.dumps(
            {
                "installed_on_request": True,
                "source": {
                    "spec": "stable",
                    "versions": {"stable": "4.0.4"},
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return root


def start(
    tmp_path: Path,
    *,
    empty_destination: bool = False,
    checkpoint=None,
    archive_entries: list[dict] | None = None,
) -> tuple[Path, Path, Path, Path, dict]:
    source = make_source(tmp_path)
    if archive_entries is not None:
        (source / "archive.json").write_text(
            json.dumps({"entries": archive_entries}), encoding="utf-8"
        )
    (source / "tickets" / "existing-empty-directory").mkdir(mode=0o700)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    if empty_destination:
        destination.mkdir(mode=0o700)
    run = tmp_path / "import-run"
    state = start_import(
        source,
        destination,
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
        checkpoint=checkpoint,
    )
    if state["phase"] == "review_required":
        bindings, decisions = make_review_files(run)
        state = review_import(
            run,
            bindings_path=bindings,
            decisions_path=decisions,
            confirm_central_stopped=True,
            checkpoint=checkpoint,
        )
    return source, stable, destination, run, state


def test_v4_archive_is_imported_with_provenance(tmp_path: Path) -> None:
    archived = {
        "id": "old-memory-7",
        "agent_name": "legacy-main",
        "title": "Archived launch context",
        "content": "\n" + "z" * 12_000 + "searchable archive phrase\n",
        "archived_at": "2025-01-02T03:04:05+00:00",
        "tags": ["launch"],
    }

    _source, _stable, destination, _run, state = start(
        tmp_path, archive_entries=[archived]
    )

    assert state["phase"] == "installed"
    store = TransactionalSQLiteStore(destination)
    board = store.load(board_path(store, BOARD_ID), dict)
    matches = [item for item in board["memories"] if item.get("archived")]
    assert len(matches) == 1
    assert matches[0]["content"] == archived["content"]
    assert matches[0]["archive_source_id"] == archived["id"]
    assert matches[0]["archived_at"] == archived["archived_at"]
    assert matches[0]["migration_provenance"] == "v4-archive-import"
    assert "archived" in matches[0]["tags"]


def test_archive_backfill_twice_is_idempotent_and_keeps_existing_rows(
    tmp_path: Path,
) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    archive_file = source / "archive.json"
    archive_file.write_text(
        json.dumps(
            [
                {
                    "id": "backfill-1",
                    "title": "Backfilled archive",
                    "content": "backfill searchable phrase",
                    "archived_at": "2024-06-01T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = TransactionalSQLiteStore(destination)
    before = store.load(board_path(store, BOARD_ID), dict)["memories"]

    first = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
    )
    after_first = store.load(board_path(store, BOARD_ID), dict)["memories"]
    second = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
    )
    after_second = store.load(board_path(store, BOARD_ID), dict)["memories"]

    assert first["inserted"] == 1
    assert second["status"] == "noop"
    assert second["inserted"] == 0
    assert after_first == after_second
    assert after_first[: len(before)] == before
    assert len(after_first) == len(before) + 1


def test_archive_backfill_internal_counts_home_paths_per_record_byte_exact(
    tmp_path: Path,
) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    records = [
        {
            "id": "home-path-a",
            "content": "workspace=/Users/synthetic-a/project\n",
        },
        {
            "id": "home-path-b",
            "content": "workspace=/Users/synthetic-b/project\n",
        },
    ]
    archive_file = source / "archive.json"
    archive_file.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(ValueError, match="posix_home"):
        archive_backfill(
            archive_file,
            destination,
            board_id=BOARD_ID,
            confirm_central_stopped=True,
        )

    first = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
        scrub_profile="internal",
    )
    second = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
        scrub_profile="internal",
    )
    store = TransactionalSQLiteStore(destination)
    board = store.load(board_path(store, BOARD_ID), dict)
    inserted = {
        item["archive_source_id"]: item
        for item in board["memories"]
        if item.get("archive_source_id") in {"home-path-a", "home-path-b"}
    }

    assert first["inserted"] == 2
    assert first["posix_home_allowed_count"] == 2
    assert set(inserted) == {"home-path-a", "home-path-b"}
    for raw in records:
        assert inserted[raw["id"]]["content"] == raw["content"]
        assert inserted[raw["id"]]["legacy_record"]["content"] == raw["content"]
    assert second["status"] == "noop"
    assert second["already_present"] == 2
    assert second["posix_home_allowed_count"] == 2


@pytest.mark.parametrize("scrub_profile", ["strict", "internal"])
def test_archive_backfill_rejects_secrets_under_every_profile(
    tmp_path: Path,
    scrub_profile: str,
) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    archive_file = source / "archive.json"
    archive_file.write_text(
        json.dumps(
            [
                {
                    "id": "secret",
                    "content": "Bearer TESTTOKEN_123456",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = TransactionalSQLiteStore(destination)
    before = store.load(board_path(store, BOARD_ID), dict)

    with pytest.raises(ValueError, match="bearer_token"):
        archive_backfill(
            archive_file,
            destination,
            board_id=BOARD_ID,
            confirm_central_stopped=True,
            scrub_profile=scrub_profile,
        )
    assert store.load(board_path(store, BOARD_ID), dict) == before


def test_archive_backfill_explicit_secret_redaction_is_provenanced_and_idempotent(
    tmp_path: Path,
) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    raw = {
        "id": "redacted-secret",
        "content": (
            "workspace=/Users/synthetic-account/project; "
            "authorization: Bearer TESTTOKEN_123456"
        ),
    }
    clean_raw = {
        "id": "neighboring-clean-record",
        "content": "This synthetic record has no secret material.",
    }
    record_key = archive_record_key(raw)
    clean_record_key = archive_record_key(clean_raw)
    archive_file = source / "archive.json"
    archive_file.write_text(json.dumps([raw, clean_raw]), encoding="utf-8")

    first = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
        scrub_profile="internal",
        redact_secrets_records=[record_key],
    )
    store = TransactionalSQLiteStore(destination)
    board_after_first = store.load(board_path(store, BOARD_ID), dict)
    second = archive_backfill(
        archive_file,
        destination,
        board_id=BOARD_ID,
        confirm_central_stopped=True,
        scrub_profile="internal",
        redact_secrets_records=[record_key],
    )
    board_after_second = store.load(board_path(store, BOARD_ID), dict)
    inserted = next(
        item
        for item in board_after_second["memories"]
        if item["memory_id"] == record_key
    )
    clean_inserted = next(
        item
        for item in board_after_second["memories"]
        if item["memory_id"] == clean_record_key
    )

    assert inserted["content"] == (
        "workspace=/Users/synthetic-account/project; "
        "authorization: Bearer [REDACTED:BEARER_TOKEN]"
    )
    assert inserted["content"].count("[REDACTED:BEARER_TOKEN]") == 1
    assert inserted["redacted_rules"] == ["bearer_token"]
    assert clean_inserted["content"] == clean_raw["content"]
    assert "redacted_rules" not in clean_inserted
    assert first["inserted"] == 2
    assert first["posix_home_allowed_count"] == 1
    assert first["redacted_records"] == [record_key]
    assert second["status"] == "noop"
    assert second["inserted"] == 0
    assert second["already_present"] == 2
    assert second["redacted_records"] == [record_key]
    assert board_after_second == board_after_first


def test_archive_backfill_rejects_listed_clean_record(tmp_path: Path) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    raw = {"id": "clean-record", "content": "clean archive content"}
    archive_file = source / "archive.json"
    archive_file.write_text(json.dumps([raw]), encoding="utf-8")
    store = TransactionalSQLiteStore(destination)
    before = store.load(board_path(store, BOARD_ID), dict)

    with pytest.raises(ValueError, match="has no secret violations"):
        archive_backfill(
            archive_file,
            destination,
            board_id=BOARD_ID,
            confirm_central_stopped=True,
            redact_secrets_records=[archive_record_key(raw)],
        )
    assert store.load(board_path(store, BOARD_ID), dict) == before


def test_archive_backfill_rejects_unknown_redaction_record_key(
    tmp_path: Path,
) -> None:
    source, _stable, destination, _run, state = start(tmp_path)
    assert state["phase"] == "installed"
    raw = {"id": "known-clean-record", "content": "clean archive content"}
    archive_file = source / "archive.json"
    archive_file.write_text(json.dumps([raw]), encoding="utf-8")
    store = TransactionalSQLiteStore(destination)
    before = store.load(board_path(store, BOARD_ID), dict)

    with pytest.raises(
        ValueError,
        match="redact-secrets record keys not found: ARCHIVE-typo",
    ):
        archive_backfill(
            archive_file,
            destination,
            board_id=BOARD_ID,
            confirm_central_stopped=True,
            redact_secrets_records=["ARCHIVE-typo"],
        )
    assert store.load(board_path(store, BOARD_ID), dict) == before


def test_archive_backfill_cli_threads_scrub_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_backfill(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "noop"}

    monkeypatch.setattr(personal_import_module, "archive_backfill", fake_backfill)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal_import.py",
            "archive-backfill",
            str(tmp_path / "archive.json"),
            str(tmp_path / "central"),
            "--board-id",
            BOARD_ID,
            "--scrub-profile",
            "internal",
            "--redact-secrets-record",
            "ARCHIVE-first",
            "--redact-secrets-record",
            "ARCHIVE-second",
            "--confirm-central-stopped",
        ],
    )

    personal_import_module.main()

    assert captured["kwargs"] == {
        "board_id": BOARD_ID,
        "confirm_central_stopped": True,
        "scrub_profile": "internal",
        "redact_secrets_records": ["ARCHIVE-first", "ARCHIVE-second"],
    }
    assert json.loads(capsys.readouterr().out) == {"status": "noop"}


def make_review_files(run: Path) -> tuple[Path | None, Path | None]:
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    evidence = run / "evidence"
    worksheet = json.loads(
        (evidence / "quarantine-worksheet.json").read_text(encoding="utf-8")
    )
    decisions_path: Path | None = None
    if worksheet["entry_count"]:
        decisions_path = evidence / "operator-decisions.json"
        decisions_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "board_id": state["board_id"],
                    "worksheet_sha256": worksheet["worksheet_sha256"],
                    "entry_count": worksheet["entry_count"],
                    "status": "REVIEWED-SIGNED-READY",
                    "review_metadata": {
                        "reviewed_at": "2026-08-19T00:00:00+00:00"
                    },
                    "entries": [
                        {**item, "decision": "drop"}
                        for item in worksheet["entries"]
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(decisions_path, 0o600)

    template_path = evidence / "identity-bindings-template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    store = TransactionalSQLiteStore(run / "staging-central")
    board = store.load(board_path(store, state["board_id"]), dict)
    agents = board.get("legacy_import", {}).get("agents", {})
    bindings_path: Path | None = None
    if any(
        item.get("binding_status") == "legacy_unbound"
        for item in agents.values()
    ):
        bindings_path = evidence / "operator-bindings.json"
        bindings_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "board_id": state["board_id"],
                    "identity_worksheet_sha256": template[
                        "identity_worksheet_sha256"
                    ],
                    "entry_count": len(agents),
                    "bindings": {
                        key: "RETIRE"
                        for key, value in template["bindings"].items()
                        if value == "PENDING" and key.removeprefix("record:") in agents
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(bindings_path, 0o600)
    return bindings_path, decisions_path


def start_policy_run(tmp_path: Path) -> tuple[Path, dict]:
    source = make_source(tmp_path)
    memories_path = source / "memories.json"
    memories = json.loads(memories_path.read_text(encoding="utf-8"))
    memories.append(
        {
            "id": "M-mixed-policy",
            "agent_name": "legacy-main",
            "memory_type": "context",
            "title": "Path /Users/synthetic-user/private",
            "content": "Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "pinned": False,
        }
    )
    memories_path.write_text(json.dumps(memories), encoding="utf-8")
    run = tmp_path / "import-run"
    state = start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=make_stable_install(tmp_path),
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    return run, state


def write_policy(
    tmp_path: Path,
    run: Path,
    *,
    overrides: dict[str, str] | None = None,
    worksheet_sha256: str | None = None,
) -> tuple[Path, dict]:
    worksheet = json.loads(
        (run / "evidence" / "quarantine-worksheet.json").read_text(
            encoding="utf-8"
        )
    )
    actions = {
        rule: "drop"
        for entry in worksheet["entries"]
        for rule in entry["rules"]
    }
    actions.update(overrides or {})
    policy = {
        "schema_version": 1,
        "status": "POLICY-SIGNED-READY",
        "board_id": BOARD_ID,
        "worksheet_sha256": worksheet_sha256 or worksheet["worksheet_sha256"],
        "rules": actions,
    }
    path = tmp_path / "POLICY-signed.json"
    path.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, worksheet


def complete_review(run: Path, checkpoint=None) -> dict:
    bindings, decisions = make_review_files(run)
    return review_import(
        run,
        bindings_path=bindings,
        decisions_path=decisions,
        confirm_central_stopped=True,
        checkpoint=checkpoint,
    )


def test_decide_cli_escalates_mixed_record_and_emits_valid_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, _state = start_policy_run(tmp_path)
    policy_path, worksheet = write_policy(
        tmp_path,
        run,
        overrides={"posix_home": "accept-as-is", "bearer_token": "redact-span"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal_import.py",
            "decide",
            str(run),
            "--policy",
            str(policy_path),
        ],
    )

    personal_import_module.main()

    summary = json.loads(capsys.readouterr().out)
    decisions_path = run / "evidence" / summary["output_name"]
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decision_map, decisions_format = _decision_map(
        decisions, worksheet, BOARD_ID
    )
    mixed_keys = [
        entry["record_key"]
        for entry in worksheet["entries"]
        if entry["record_id"] == "M-mixed-policy"
    ]
    assert len(mixed_keys) >= 2
    assert {decision_map[key] for key in mixed_keys} == {"redact-span"}
    assert decisions_format == "policy-auto-decisions-v1"
    assert decisions["status"] == "POLICY-AUTO-DECIDED"
    assert decisions["policy_sha256"] == hashlib.sha256(
        policy_path.read_bytes()
    ).hexdigest()
    assert stat.S_IMODE(decisions_path.stat().st_mode) == 0o600


def test_policy_worksheet_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    run, _state = start_policy_run(tmp_path)
    policy_path, _worksheet = write_policy(
        tmp_path,
        run,
        worksheet_sha256="0" * 64,
    )

    with pytest.raises(ValueError, match="does not match worksheet_sha256"):
        generate_policy_decisions(run, policy_path=policy_path)


def test_policy_cannot_auto_accept_secret_class_rule(tmp_path: Path) -> None:
    run, _state = start_policy_run(tmp_path)
    policy_path, _worksheet = write_policy(
        tmp_path,
        run,
        overrides={"aws_access_key_id": "accept-as-is"},
    )

    with pytest.raises(ValueError, match="secret-class"):
        generate_policy_decisions(run, policy_path=policy_path)


def write_worker_config(
    tmp_path: Path,
    source: Path,
    stable: Path,
    destination: Path,
    run: Path,
) -> Path:
    config = tmp_path / "crash-worker.json"
    config.write_text(
        json.dumps(
            {
                "source": str(source),
                "stable": str(stable),
                "destination": str(destination),
                "run": str(run),
                "board_id": BOARD_ID,
                "owner_principal_id": OWNER_PRINCIPAL,
                "owner_agent_name": OWNER_AGENT,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config


def add_worker_review_inputs(config: Path, run: Path) -> None:
    bindings, decisions = make_review_files(run)
    value = json.loads(config.read_text(encoding="utf-8"))
    value["bindings"] = str(bindings) if bindings else None
    value["decisions"] = str(decisions) if decisions else None
    config.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def run_crash_worker(
    operation: str, config: Path, checkpoint: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CRASH_WORKER), operation, str(config)]
    if checkpoint is not None:
        command.append(checkpoint)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        command,
        cwd="/private/tmp",
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_copy_only_import_schema_retry_and_absent_rollback(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    source_before = tree_state(source)
    stable_before = stable_install_state(stable)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"

    state = start_import(
        source,
        destination,
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    assert not destination.exists()
    assert state["review_gate"] == {
        "quarantine_records": 1,
        "unmapped_legacy_agents": 3,
        "resolved": False,
    }
    identity_worksheet = run / "evidence" / "identity-binding-worksheet.json"
    identity_template = run / "evidence" / "identity-bindings-template.json"
    assert identity_worksheet.is_file() and identity_template.is_file()
    assert stat.S_IMODE(identity_worksheet.stat().st_mode) == 0o600
    assert stat.S_IMODE(identity_template.stat().st_mode) == 0o600
    assert json.loads(identity_template.read_text())["entry_count"] == 3
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"
    state = complete_review(run)
    assert state["phase"] == "installed"
    assert tree_state(source) == source_before
    assert stable_install_state(stable) == stable_before
    assert not (source / "WRITE_FENCE.json").exists()
    assert not (source / "PROMOTED.json").exists()
    assert state["provenance"]["live_source_passed_to_native_import"] is False
    assert state["provenance"]["original_source_hash"] != state["provenance"][
        "frozen_import_hash"
    ]
    full_backup = run / state["full_backup_relative"]
    import_copy = run / state["snapshot_relative"]
    assert (full_backup / "watch-legacy-main.json").is_file()
    assert not (import_copy / "watch-legacy-main.json").exists()
    assert (import_copy / "WRITE_FENCE.json").is_file()

    store = TransactionalSQLiteStore(destination)
    board = store.load(board_path(store, BOARD_ID), dict)
    membership = board["principal_memberships"][OWNER_PRINCIPAL]
    assert board["schema_version"] == 6
    assert board["next_admission_revision"] == 1
    assert membership["role"] == "admin"
    assert membership["source"] == "offline_import_provisioning"
    assert membership["created_at"] == membership["updated_at"]
    assert all(
        item["binding_status"] == "retired"
        for item in board["legacy_import"]["agents"].values()
    )
    assert state["review_gate"]["resolved"] is True
    assert state["review_history"][-1]["unmapped"] == 0
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"
    before_retry = state["canonical_db_sha256"]
    retried = retry_import(run, confirm_central_stopped=True)
    assert retried["last_retry"] == {
        "status": "noop",
        "canonical_db_sha256": before_retry,
        "source_copy_window_unchanged": True,
        "stable_install_import_window_unchanged": True,
    }
    rolled_back = rollback_import(run, confirm_central_stopped=True)
    assert rolled_back["phase"] == "rolled_back"
    assert not destination.exists()
    assert (run / "rollback-quarantine" / "imported-central").is_dir()
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


def test_empty_baseline_backup_and_changed_tree_are_preserved(tmp_path: Path) -> None:
    _source, _stable, destination, run, state = start(
        tmp_path, empty_destination=True
    )
    assert state["backup"]["kind"] == "empty"
    assert state["backup"]["preserved"] is True
    (destination / "post-import-change.txt").write_text("new Central data\n")
    rolled_back = rollback_import(run, confirm_central_stopped=True)
    assert rolled_back["rollback"]["current_tree_changed_after_install"] is True
    assert destination.is_dir() and list(destination.iterdir()) == []
    quarantined = run / "rollback-quarantine" / "imported-central"
    assert (quarantined / "post-import-change.txt").read_text() == "new Central data\n"


def test_live_and_stable_changes_after_bounded_window_do_not_block_rollback(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    changed = False

    def mutate_after_copy(name: str) -> None:
        nonlocal changed
        if name == "after-freeze-before-state" and not changed:
            changed = True
            (source / "later-v4-write.txt").write_text("legitimate later write\n")

    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    state = start_import(
        source,
        destination,
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
        checkpoint=mutate_after_copy,
    )
    assert state["phase"] == "review_required"
    state = complete_review(run)
    assert state["phase"] == "installed"
    assert (source / "later-v4-write.txt").is_file()
    assert not (
        run / state["full_backup_relative"] / "later-v4-write.txt"
    ).exists()
    (stable / "later-package-manager-change.txt").write_text("external update\n")
    assert retry_import(run, confirm_central_stopped=True)["phase"] == "installed"
    assert rollback_import(run, confirm_central_stopped=True)["phase"] == "rolled_back"
    assert (source / "later-v4-write.txt").is_file()


@pytest.mark.parametrize(
    "checkpoint_name",
    [
        "after-initializing-state-before-run-rename",
        "after-initialized-state",
        "after-freeze-before-state",
        "after-stage-before-state",
        "after-review-required-state",
    ],
)
def test_import_crash_points_resume(
    tmp_path: Path, checkpoint_name: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    triggered = False

    def fail_once(name: str) -> None:
        nonlocal triggered
        if name == checkpoint_name and not triggered:
            triggered = True
            raise RuntimeError("injected checkpoint")

    with pytest.raises(RuntimeError, match="injected checkpoint"):
        start_import(
            source,
            tmp_path / "central",
            run,
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
            checkpoint=fail_once,
        )
    recovered = retry_import(run, confirm_central_stopped=True)
    assert recovered["phase"] == "review_required"
    recovered = complete_review(run)
    assert recovered["phase"] == "installed"
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


@pytest.mark.parametrize(
    "checkpoint_name",
    ["after-reviewing-state", "after-installing-state", "after-destination-move"],
)
def test_review_and_install_exception_points_resume(
    tmp_path: Path, checkpoint_name: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    state = start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    triggered = False

    def fail_once(name: str) -> None:
        nonlocal triggered
        if name == checkpoint_name and not triggered:
            triggered = True
            raise RuntimeError("injected checkpoint")

    with pytest.raises(RuntimeError, match="injected checkpoint"):
        complete_review(run, fail_once)
    assert retry_import(run, confirm_central_stopped=True)["phase"] == "installed"
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


@pytest.mark.parametrize(
    "checkpoint_name",
    [
        "after-backup-move",
        "after-rolling-back-state",
        "after-rollback-quarantine-move",
        "after-backup-restore",
    ],
)
def test_empty_backup_and_rollback_crash_points_resume(
    tmp_path: Path, checkpoint_name: str
) -> None:
    if checkpoint_name == "after-backup-move":
        source = make_source(tmp_path)
        stable = make_stable_install(tmp_path)
        destination = tmp_path / "central"
        destination.mkdir(mode=0o700)
        run = tmp_path / "import-run"
        triggered = False

        def fail_once(name: str) -> None:
            nonlocal triggered
            if name == checkpoint_name and not triggered:
                triggered = True
                raise RuntimeError("injected checkpoint")

        state = start_import(
            source,
            destination,
            run,
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
        )
        assert state["phase"] == "review_required"
        with pytest.raises(RuntimeError, match="injected checkpoint"):
            complete_review(run, fail_once)
        assert retry_import(run, confirm_central_stopped=True)["phase"] == "installed"
    else:
        _source, _stable, destination, run, _state = start(
            tmp_path, empty_destination=True
        )
        triggered = False

        def fail_once(name: str) -> None:
            nonlocal triggered
            if name == checkpoint_name and not triggered:
                triggered = True
                raise RuntimeError("injected checkpoint")

        with pytest.raises(RuntimeError, match="injected checkpoint"):
            rollback_import(
                run,
                confirm_central_stopped=True,
                checkpoint=fail_once,
            )
        assert rollback_import(run, confirm_central_stopped=True)["phase"] == "rolled_back"
        assert destination.is_dir() and list(destination.iterdir()) == []
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


@pytest.mark.parametrize(
    "checkpoint_name",
    [
        "after-initializing-state-before-run-rename",
        "after-freeze-before-state",
        "after-stage-before-state",
        "after-backup-move",
        "after-destination-move",
    ],
)
def test_real_process_death_during_import_recovers_in_new_process(
    tmp_path: Path, checkpoint_name: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    if checkpoint_name == "after-backup-move":
        destination.mkdir(mode=0o700)
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    source_before = tree_state(source)
    stable_before = stable_install_state(stable)

    install_checkpoint = checkpoint_name in {
        "after-backup-move",
        "after-destination-move",
    }
    if install_checkpoint:
        staged = run_crash_worker("start", config)
        assert staged.returncode == 0, (staged.stdout, staged.stderr)
        assert json.loads(staged.stdout) == {"phase": "review_required"}
        add_worker_review_inputs(config, run)
        killed = run_crash_worker("review", config, checkpoint_name)
    else:
        killed = run_crash_worker("start", config, checkpoint_name)
    assert killed.returncode == 97, (killed.stdout, killed.stderr)
    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    if json.loads(recovered.stdout) == {"phase": "review_required"}:
        add_worker_review_inputs(config, run)
        recovered = run_crash_worker("review", config)
        assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    verified = run_crash_worker("status", config)
    assert verified.returncode == 0, (verified.stdout, verified.stderr)
    assert json.loads(recovered.stdout) == {"phase": "installed"}
    assert json.loads(verified.stdout) == {"phase": "installed"}
    assert tree_state(source) == source_before
    assert stable_install_state(stable) == stable_before
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


@pytest.mark.parametrize(
    "copy_relative",
    [
        "source-snapshot/.agent-mem",
        "full-source-backup/.agent-mem",
        "promoted-board/.agent-mem",
    ],
)
def test_hard_death_completed_freeze_tamper_is_quarantined_and_rebuilt(
    tmp_path: Path, copy_relative: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    killed = run_crash_worker("start", config, "after-freeze-before-state")
    assert killed.returncode == 97
    marker = b"tampered-after-hard-death"
    target = run / "frozen" / copy_relative / "memories.json"
    target.write_bytes(marker)
    os.chmod(target, 0o600)
    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "review_required"}
    assert marker not in (run / "frozen" / copy_relative / "memories.json").read_bytes()
    quarantined = list((run / "recovery-quarantine").glob("invalid-completed-freeze-*"))
    assert len(quarantined) == 1
    assert marker in (quarantined[0] / copy_relative / "memories.json").read_bytes()


@pytest.mark.parametrize(
    "copy_relative",
    [
        "source-snapshot/.agent-mem",
        "full-source-backup/.agent-mem",
        "promoted-board/.agent-mem",
    ],
)
@pytest.mark.parametrize(
    "tamper_kind",
    [
        "empty-directory",
        "directory-mode",
        "root-mode",
        "remove-directory",
        "rename-directory",
    ],
)
def test_hard_death_completed_freeze_directory_tamper_is_quarantined_and_rebuilt(
    tmp_path: Path, copy_relative: str, tamper_kind: str
) -> None:
    source = make_source(tmp_path)
    (source / "tickets" / "existing-empty-directory").mkdir(mode=0o700)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    killed = run_crash_worker("start", config, "after-freeze-before-state")
    assert killed.returncode == 97
    copy_root = run / "frozen" / copy_relative
    if tamper_kind == "empty-directory":
        injected = copy_root / "injected-empty-directory"
        injected.mkdir(mode=0o700)
    elif tamper_kind == "directory-mode":
        injected = copy_root / "tickets"
        os.chmod(injected, 0o755)
    elif tamper_kind == "root-mode":
        injected = copy_root
        os.chmod(injected, 0o755)
    elif tamper_kind == "remove-directory":
        injected = copy_root / "tickets" / "existing-empty-directory"
        injected.rmdir()
    else:
        injected = copy_root / "tickets" / "existing-empty-directory-renamed"
        os.replace(copy_root / "tickets" / "existing-empty-directory", injected)

    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "review_required"}
    rebuilt = run / "frozen" / copy_relative
    if tamper_kind == "empty-directory":
        assert not (rebuilt / "injected-empty-directory").exists()
    elif tamper_kind == "directory-mode":
        assert stat.S_IMODE((rebuilt / "tickets").stat().st_mode) == 0o700
    elif tamper_kind == "root-mode":
        assert stat.S_IMODE(rebuilt.stat().st_mode) == 0o700
    elif tamper_kind == "remove-directory":
        assert (rebuilt / "tickets" / "existing-empty-directory").is_dir()
    else:
        assert (rebuilt / "tickets" / "existing-empty-directory").is_dir()
        assert not (
            rebuilt / "tickets" / "existing-empty-directory-renamed"
        ).exists()
    quarantined = list((run / "recovery-quarantine").glob("invalid-completed-freeze-*"))
    assert len(quarantined) == 1
    quarantined_copy = quarantined[0] / copy_relative
    if tamper_kind == "empty-directory":
        assert (quarantined_copy / "injected-empty-directory").is_dir()
    elif tamper_kind == "directory-mode":
        assert stat.S_IMODE((quarantined_copy / "tickets").stat().st_mode) == 0o755
    elif tamper_kind == "root-mode":
        assert stat.S_IMODE(quarantined_copy.stat().st_mode) == 0o755
    elif tamper_kind == "remove-directory":
        assert not (
            quarantined_copy / "tickets" / "existing-empty-directory"
        ).exists()
    else:
        assert (
            quarantined_copy / "tickets" / "existing-empty-directory-renamed"
        ).is_dir()
        assert not (
            quarantined_copy / "tickets" / "existing-empty-directory"
        ).exists()


@pytest.mark.parametrize(
    "proof_field",
    [
        "full_files",
        "live_source_write",
        "private_modes",
        "source_snapshot",
        "full_source_backup",
        "promoted_board_root",
        "unexpected_field",
    ],
)
def test_hard_death_completed_freeze_proof_forgery_is_quarantined_and_rebuilt(
    tmp_path: Path, proof_field: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    config = write_worker_config(
        tmp_path, source, stable, tmp_path / "central", run
    )
    killed = run_crash_worker("start", config, "after-freeze-before-state")
    assert killed.returncode == 97
    proof_path = run / "frozen" / "snapshot-proof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    replacements = {
        "full_files": 999_999,
        "live_source_write": "forged",
        "private_modes": "forged",
        "source_snapshot": str(
            run / "frozen" / "full-source-backup" / ".agent-mem"
        ),
        "full_source_backup": str(
            run / "frozen" / "source-snapshot" / ".agent-mem"
        ),
        "promoted_board_root": str(
            run / "frozen" / "source-snapshot" / ".agent-mem"
        ),
        "unexpected_field": "forged",
    }
    proof[proof_field] = replacements[proof_field]
    proof_path.write_text(
        json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(proof_path, 0o600)

    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "review_required"}
    quarantined = list(
        (run / "recovery-quarantine").glob("invalid-completed-freeze-*")
    )
    assert len(quarantined) == 1
    restored = json.loads(
        (run / "frozen" / "snapshot-proof.json").read_text(encoding="utf-8")
    )
    assert restored["live_source_write"] == "none"
    assert restored["private_modes"] == "dirs=0700,files=0600"
    assert "unexpected_field" not in restored


def test_hard_death_self_resealed_frozen_payload_forgery_is_rebuilt(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    config = write_worker_config(
        tmp_path, source, stable, tmp_path / "central", run
    )
    killed = run_crash_worker("start", config, "after-freeze-before-state")
    assert killed.returncode == 97
    proof_path = run / "frozen" / "snapshot-proof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    forged_title = "forged frozen title"
    for label, relative in (
        ("source_snapshot", "source-snapshot/.agent-mem"),
        ("promoted_board", "promoted-board/.agent-mem"),
    ):
        copy_root = run / "frozen" / relative
        memories_path = copy_root / "memories.json"
        memories = json.loads(memories_path.read_text(encoding="utf-8"))
        memories[0]["title"] = forged_title
        memories_path.write_text(
            json.dumps(memories, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(memories_path, 0o600)
        proof["completed_copy_seals"][label] = list(
            snapshot_tree_state(copy_root)
        )
    proof_path.write_text(
        json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(proof_path, 0o600)

    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "review_required"}
    quarantined = list(
        (run / "recovery-quarantine").glob("invalid-completed-freeze-*")
    )
    assert len(quarantined) == 1
    for relative in (
        "source-snapshot/.agent-mem",
        "promoted-board/.agent-mem",
    ):
        restored = json.loads(
            (run / "frozen" / relative / "memories.json").read_text(
                encoding="utf-8"
            )
        )
        assert all(item.get("title") != forged_title for item in restored)


@pytest.mark.parametrize("tamper_kind", ["content", "mode"])
def test_prepared_state_promoted_copy_tamper_fails_closed(
    tmp_path: Path, tamper_kind: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    killed = run_crash_worker("start", config, "after-prepared-state")
    assert killed.returncode == 97
    target = run / "frozen" / "promoted-board" / ".agent-mem" / "memories.json"
    if tamper_kind == "content":
        target.write_bytes(b"tampered-prepared-copy")
        os.chmod(target, 0o600)
    else:
        os.chmod(target, 0o644)
    with pytest.raises(RuntimeError, match="sealed initial promoted source copy"):
        retry_import(run, confirm_central_stopped=True)
    with pytest.raises(RuntimeError, match="sealed initial promoted source copy"):
        status_import(run, confirm_central_stopped=True)
    assert not destination.exists()


def test_transitioning_promoted_copy_byte_tamper_fails_status_and_retry(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    killed = run_crash_worker("start", config, "after-promoted-transition-state")
    assert killed.returncode == 97
    target = run / "frozen" / "promoted-board" / ".agent-mem" / "memories.json"
    target.write_bytes(b"tampered-transition-copy")
    os.chmod(target, 0o600)
    with pytest.raises(RuntimeError, match="transitioning promoted source copy changed"):
        status_import(run, confirm_central_stopped=True)
    with pytest.raises(RuntimeError, match="transitioning promoted source copy changed"):
        retry_import(run, confirm_central_stopped=True)
    assert not destination.exists()


def test_partial_snapshot_proof_after_hard_death_is_recoverable(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    config = write_worker_config(
        tmp_path, source, stable, tmp_path / "central", run
    )
    killed = run_crash_worker("start", config, "after-freeze-before-state")
    assert killed.returncode == 97
    proof = run / "frozen" / "snapshot-proof.json"
    proof.write_bytes(b'{"status":')
    os.chmod(proof, 0o600)
    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "review_required"}
    assert json.loads((run / "frozen" / "snapshot-proof.json").read_text())[
        "status"
    ] == "complete"


@pytest.mark.parametrize(
    "checkpoint_name",
    ["after-rollback-quarantine-move", "after-backup-restore"],
)
def test_real_process_death_during_rollback_recovers_in_new_process(
    tmp_path: Path, checkpoint_name: str
) -> None:
    source, stable, destination, run, _state = start(
        tmp_path, empty_destination=True
    )
    config = write_worker_config(tmp_path, source, stable, destination, run)
    source_before = tree_state(source)

    killed = run_crash_worker("rollback", config, checkpoint_name)
    assert killed.returncode == 97, (killed.stdout, killed.stderr)
    recovered = run_crash_worker("rollback", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    verified = run_crash_worker("status", config)
    assert verified.returncode == 0, (verified.stdout, verified.stderr)
    assert json.loads(recovered.stdout) == {"phase": "rolled_back"}
    assert json.loads(verified.stdout) == {"phase": "rolled_back"}
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert tree_state(source) == source_before
    assert status_import(run, confirm_central_stopped=True)["integrity"] == "verified"


@pytest.mark.parametrize(
    "checkpoint_name",
    ["after-quarantine-review", "after-identity-review", "after-reviewed-state"],
)
def test_real_process_death_during_review_recovers_in_new_process(
    tmp_path: Path, checkpoint_name: str
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    config = write_worker_config(tmp_path, source, stable, destination, run)
    staged = run_crash_worker("start", config)
    assert staged.returncode == 0, (staged.stdout, staged.stderr)
    assert json.loads(staged.stdout) == {"phase": "review_required"}
    add_worker_review_inputs(config, run)
    killed = run_crash_worker("review", config, checkpoint_name)
    assert killed.returncode == 97, (killed.stdout, killed.stderr)
    recovered = run_crash_worker("retry", config)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout) == {"phase": "installed"}
    state = status_import(run, confirm_central_stopped=True)
    assert state["integrity"] == "verified"
    assert state["review_history"][-1]["unmapped"] == 0
    assert canonical_db_hash(destination) == state["canonical_db_sha256"]
    store = TransactionalSQLiteStore(destination)
    board = store.load(board_path(store, BOARD_ID), dict)
    assert all(
        item["binding_status"] != "legacy_unbound"
        for item in board["legacy_import"]["agents"].values()
    )


def test_invalid_review_input_does_not_wedge_corrected_review(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    state = start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    bindings, decisions = make_review_files(run)
    assert decisions is not None
    invalid = json.loads(decisions.read_text())
    invalid["entries"] = []
    invalid["entry_count"] = 0
    decisions.write_text(json.dumps(invalid), encoding="utf-8")
    os.chmod(decisions, 0o600)
    with pytest.raises(ValueError, match="cover every worksheet"):
        review_import(
            run,
            bindings_path=bindings,
            decisions_path=decisions,
            confirm_central_stopped=True,
        )
    assert status_import(run, confirm_central_stopped=True)["phase"] == "review_required"
    assert complete_review(run)["phase"] == "installed"


def test_quarantine_restored_agent_requires_explicit_record_binding(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    agents_path = source / "agents.json"
    agents = json.loads(agents_path.read_text(encoding="utf-8"))
    secret_record_id = "private-agent@example.invalid"
    agents[secret_record_id] = {
        "agent_name": "restored-agent",
        "status": "active",
        "note": "synthetic AKIAABCDEFGHIJKLMNOP",
    }
    agents_path.write_text(json.dumps(agents, sort_keys=True), encoding="utf-8")
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    state = start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    evidence = run / "evidence"
    worksheet = json.loads(
        (evidence / "quarantine-worksheet.json").read_text(encoding="utf-8")
    )
    restored_rows = [
        item
        for item in worksheet["entries"]
        if item["record_type"] == "agent"
        and item["record_id"].startswith("sha256-")
    ]
    assert restored_rows
    masked_record_id = restored_rows[0]["record_id"]
    assert secret_record_id not in json.dumps(worksheet)
    decisions = evidence / "restore-agent-decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "board_id": BOARD_ID,
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "entry_count": worksheet["entry_count"],
                "status": "REVIEWED-SIGNED-READY",
                "review_metadata": {"reviewed_at": "2026-08-19T00:00:00+00:00"},
                "entries": [
                    {
                        **item,
                        "decision": (
                            "redact-span"
                            if item["record_type"] == "agent"
                            and item["record_id"] == masked_record_id
                            else "drop"
                        ),
                    }
                    for item in worksheet["entries"]
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(decisions, 0o600)
    bindings, _default_decisions = make_review_files(run)
    assert bindings is not None
    with pytest.raises(ValueError, match="every legacy agent"):
        review_import(
            run,
            bindings_path=bindings,
            decisions_path=decisions,
            confirm_central_stopped=True,
        )
    assert status_import(run, confirm_central_stopped=True)["phase"] == "review_required"

    binding_doc = json.loads(bindings.read_text(encoding="utf-8"))
    binding_doc["bindings"][f"record:{masked_record_id}"] = "RETIRE"
    binding_doc["entry_count"] = len(binding_doc["bindings"])
    bindings.write_text(json.dumps(binding_doc, sort_keys=True), encoding="utf-8")
    os.chmod(bindings, 0o600)
    completed = review_import(
        run,
        bindings_path=bindings,
        decisions_path=decisions,
        confirm_central_stopped=True,
    )
    assert completed["phase"] == "installed"
    board = TransactionalSQLiteStore(tmp_path / "central").load(
        board_path(TransactionalSQLiteStore(tmp_path / "central"), BOARD_ID), dict
    )
    restored = board["legacy_import"]["agents"][masked_record_id]
    assert restored["binding_status"] == "retired"
    assert restored["principal_id"] is None and restored["agent_id"] is None
    assert secret_record_id not in json.dumps(board)


@pytest.mark.parametrize(
    "secret_record_id",
    ["private-agent@example.invalid", "/Users/private-account/agent"],
)
def test_secret_bearing_agent_record_id_with_clean_body_is_masked_and_reviewed(
    tmp_path: Path, secret_record_id: str
) -> None:
    source = make_source(tmp_path)
    agents_path = source / "agents.json"
    agents = json.loads(agents_path.read_text(encoding="utf-8"))
    agents[secret_record_id] = {
        "agent_name": "clean-restored-agent",
        "status": "active",
    }
    agents_path.write_text(json.dumps(agents, sort_keys=True), encoding="utf-8")
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    run = tmp_path / "import-run"
    state = start_import(
        source,
        destination,
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    evidence = run / "evidence"
    worksheet = json.loads(
        (evidence / "quarantine-worksheet.json").read_text(encoding="utf-8")
    )
    masked = next(
        row["record_id"]
        for row in worksheet["entries"]
        if row["record_type"] == "agent" and row["field"] == "record_id"
    )
    assert masked.startswith("sha256-")
    for path in (
        evidence / "quarantine-worksheet.json",
        evidence / "identity-binding-worksheet.json",
        evidence / "identity-bindings-template.json",
    ):
        assert secret_record_id not in path.read_text(encoding="utf-8")
    decisions = evidence / "clean-id-decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "board_id": BOARD_ID,
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "entry_count": worksheet["entry_count"],
                "status": "REVIEWED-SIGNED-READY",
                "review_metadata": {"reviewed_at": "2026-08-19T00:00:00+00:00"},
                "entries": [
                    {
                        **row,
                        "decision": (
                            "accept-as-is"
                            if row["record_type"] == "agent"
                            and row["record_id"] == masked
                            else "drop"
                        ),
                    }
                    for row in worksheet["entries"]
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(decisions, 0o600)
    bindings, _ = make_review_files(run)
    assert bindings is not None
    binding_doc = json.loads(bindings.read_text(encoding="utf-8"))
    binding_doc["bindings"][f"record:{masked}"] = "RETIRE"
    binding_doc["entry_count"] = len(binding_doc["bindings"])
    bindings.write_text(json.dumps(binding_doc, sort_keys=True), encoding="utf-8")
    os.chmod(bindings, 0o600)
    assert review_import(
        run,
        bindings_path=bindings,
        decisions_path=decisions,
        confirm_central_stopped=True,
    )["phase"] == "installed"
    store = TransactionalSQLiteStore(destination)
    board = store.load(board_path(store, BOARD_ID), dict)
    serialized = json.dumps(board, sort_keys=True)
    assert secret_record_id not in serialized
    assert masked in board["legacy_import"]["agents"]


def test_quarantine_scans_all_sibling_fields_before_review_and_redacts_all(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    memories_path = source / "memories.json"
    memories = json.loads(memories_path.read_text(encoding="utf-8"))
    memories.insert(
        0,
        {
            "id": "M-multi-rule",
            "agent_name": "legacy-main",
            "memory_type": "context",
            "title": "synthetic /Users/private-account/project",
            "content": "synthetic AKIAABCDEFGHIJKLMNOP",
            "pinned": False,
        },
    )
    memories_path.write_text(json.dumps(memories), encoding="utf-8")
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    state = start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    worksheet = json.loads(
        (run / "evidence" / "quarantine-worksheet.json").read_text()
    )
    target_rows = [
        row for row in worksheet["entries"] if row["record_id"] == "M-multi-rule"
    ]
    assert {(row["field"], tuple(row["rules"])) for row in target_rows} == {
        ("title", ("posix_home",)),
        ("content", ("aws_access_key_id",)),
    }
    bindings, _ = make_review_files(run)
    incomplete = run / "evidence" / "incomplete-decisions.json"
    private_json_rows = [
        {**row, "decision": "redact-span"}
        for row in worksheet["entries"]
        if row["record_key"] != target_rows[-1]["record_key"]
    ]
    incomplete.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "board_id": BOARD_ID,
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "entry_count": len(private_json_rows),
                "status": "REVIEWED-SIGNED-READY",
                "review_metadata": {"reviewed_at": "2026-08-19T00:00:00+00:00"},
                "entries": private_json_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(incomplete, 0o600)
    with pytest.raises(ValueError, match="cover every worksheet"):
        review_import(
            run,
            bindings_path=bindings,
            decisions_path=incomplete,
            confirm_central_stopped=True,
        )

    complete = run / "evidence" / "complete-decisions.json"
    complete.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "board_id": BOARD_ID,
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "entry_count": worksheet["entry_count"],
                "status": "REVIEWED-SIGNED-READY",
                "review_metadata": {"reviewed_at": "2026-08-19T00:00:00+00:00"},
                "entries": [
                    {
                        **row,
                        "decision": (
                            "redact-span"
                            if row["record_id"] == "M-multi-rule"
                            else "drop"
                        ),
                    }
                    for row in worksheet["entries"]
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(complete, 0o600)
    installed = review_import(
        run,
        bindings_path=bindings,
        decisions_path=complete,
        confirm_central_stopped=True,
    )
    assert installed["phase"] == "installed"
    store = TransactionalSQLiteStore(tmp_path / "central")
    board = store.load(board_path(store, BOARD_ID), dict)
    restored = next(
        item for item in board["memories"] if item.get("legacy_memory_id") == "M-multi-rule"
    )
    rendered = json.dumps(restored)
    assert "/Users/private-account" not in rendered
    assert "AKIAABCDEFGHIJKLMNOP" not in rendered


def test_reviewing_status_rejects_missing_staging_tree(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    run = tmp_path / "import-run"
    start_import(
        source,
        tmp_path / "central",
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )

    def stop_after_plan(name: str) -> None:
        if name == "after-reviewing-state":
            raise RuntimeError("stop after review plan")

    with pytest.raises(RuntimeError, match="stop after review plan"):
        complete_review(run, stop_after_plan)
    shutil.rmtree(run / "staging-central")
    with pytest.raises(ValueError, match="Central data root is missing"):
        status_import(run, confirm_central_stopped=True)


def test_missing_terminal_receipts_repair_only_on_mutating_retry(tmp_path: Path) -> None:
    _source, _stable, _destination, run, _state = start(tmp_path)
    install_receipt = run / "install-receipt.json"
    install_receipt.unlink()
    with pytest.raises(RuntimeError, match="install receipt is missing"):
        status_import(run, confirm_central_stopped=True)
    assert retry_import(run, confirm_central_stopped=True)["phase"] == "installed"
    assert install_receipt.is_file()
    rollback_import(run, confirm_central_stopped=True)
    rollback_receipt = run / "rollback-receipt.json"
    rollback_receipt.unlink()
    with pytest.raises(RuntimeError, match="rollback receipt is missing"):
        status_import(run, confirm_central_stopped=True)
    assert retry_import(run, confirm_central_stopped=True)["phase"] == "rolled_back"
    assert rollback_receipt.is_file()


def test_status_and_retry_reject_missing_promoted_copy(tmp_path: Path) -> None:
    _source, _stable, _destination, run, _state = start(tmp_path)
    shutil.rmtree(run / "frozen" / "promoted-board" / ".agent-mem")
    with pytest.raises(ValueError, match="tree root is missing|missing"):
        status_import(run, confirm_central_stopped=True)
    with pytest.raises(ValueError, match="tree root is missing|missing"):
        retry_import(run, confirm_central_stopped=True)


def test_nonempty_target_links_hardlinks_and_spoofed_stable_are_rejected(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    destination = tmp_path / "central"
    destination.mkdir(mode=0o700)
    (destination / "existing-board").write_text("must preserve")
    with pytest.raises(ValueError, match="non-empty Central data roots"):
        start_import(
            source,
            destination,
            tmp_path / "run-nonempty",
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
        )
    assert (destination / "existing-board").read_text() == "must preserve"

    linked_destination = tmp_path / "linked-central"
    linked_destination.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        start_import(
            source,
            linked_destination,
            tmp_path / "run-link",
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
        )

    hardlink = source / "hard-linked.json"
    os.link(source / "memories.json", hardlink)
    with pytest.raises(ValueError, match="hard-linked file"):
        start_import(
            source,
            tmp_path / "another-central",
            tmp_path / "run-hardlink",
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
        )

    spoof = tmp_path / "not-cellar" / "4.0.4"
    spoof.mkdir(parents=True)
    with pytest.raises(ValueError, match="Cellar root"):
        stable_install_state(spoof)


def test_world_writable_control_parent_is_rejected(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    stable = make_stable_install(tmp_path)
    unsafe = tmp_path / "unsafe-control"
    unsafe.mkdir(mode=0o777)
    os.chmod(unsafe, 0o777)
    with pytest.raises(ValueError, match="must not be group/other writable"):
        start_import(
            source,
            unsafe / "central",
            unsafe / "run",
            board_id=BOARD_ID,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_agent_name=OWNER_AGENT,
            stable_install_root=stable,
            confirm_central_stopped=True,
        )


def test_frozen_copy_rejects_file_root_and_lock_swap_races_without_leak(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-secret.txt"
    secret = b"EXTERNAL-SECRET-MUST-NOT-COPY"
    external.write_bytes(secret)

    for race_kind in ("file", "root", "lock"):
        case = tmp_path / race_kind
        case.mkdir(mode=0o700)
        source = make_source(case)
        run = case / "frozen-run"
        fired = False

        def race(event: str, relative: str) -> None:
            nonlocal fired, source
            if fired or event != "after-entry-stat":
                return
            if race_kind == "file" and relative == "memories.json":
                fired = True
                (source / "memories.json").rename(source / "memories.original")
                (source / "memories.json").symlink_to(external)
            elif race_kind == "lock" and relative == ".board.lock":
                fired = True
                (source / ".board.lock").rename(source / ".board.lock.original")
                (source / ".board.lock").symlink_to(external)
            elif race_kind == "root":
                fired = True
                detached = source.with_name(".agent-mem-detached")
                source.rename(detached)
                source.symlink_to(external.parent, target_is_directory=True)

        with pytest.raises((OSError, RuntimeError, ValueError)):
            prepare_frozen_copy(
                source,
                run,
                BOARD_ID,
                _race_hook=race,
            )
        assert fired is True
        if run.exists():
            for item in run.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    assert secret not in item.read_bytes()


def test_tree_state_rejects_stat_to_open_symlink_swap_and_size_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    root.mkdir(mode=0o700)
    target = root / "value.bin"
    target.write_bytes(b"safe")
    external = tmp_path / "external.bin"
    external.write_bytes(b"external-secret")
    fired = False

    def race(event: str, relative: str) -> None:
        nonlocal fired
        if not fired and event == "after-entry-stat" and relative == "value.bin":
            fired = True
            target.rename(root / "value.original")
            target.symlink_to(external)

    with pytest.raises((OSError, RuntimeError, ValueError)):
        tree_state(root, _race_hook=race)
    assert fired is True

    bounded = tmp_path / "bounded"
    bounded.mkdir(mode=0o700)
    with (bounded / "oversized.bin").open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="supported size bound"):
        tree_state(bounded)


def test_symlinked_ancestors_are_rejected_for_all_custody_paths(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    source = make_source(real)
    stable = make_stable_install(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    cases = (
        (alias / "legacy" / ".agent-mem", tmp_path / "central-source", tmp_path / "run-source", stable),
        (source, alias / "central-destination", tmp_path / "run-destination", stable),
        (source, tmp_path / "central-run", alias / "run-alias", stable),
        (source, tmp_path / "central-stable", tmp_path / "run-stable", alias / "Cellar" / "onboard-memory" / "4.0.4"),
    )
    for index, (case_source, destination, run, case_stable) in enumerate(cases):
        with pytest.raises(ValueError, match="symlink|real directory"):
            start_import(
                case_source,
                destination,
                run,
                board_id=f"board-symlink-{index}",
                owner_principal_id=OWNER_PRINCIPAL,
                owner_agent_name=OWNER_AGENT,
                stable_install_root=case_stable,
                confirm_central_stopped=True,
            )

    state = start_import(
        source,
        real / "central-owned",
        real / "run-owned",
        board_id="board-owned",
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert state["phase"] == "review_required"
    with pytest.raises(ValueError, match="symlink"):
        status_import(alias / "run-owned", confirm_central_stopped=True)
    with pytest.raises(ValueError, match="symlink|real directory"):
        retry_import(alias / "run-owned", confirm_central_stopped=True)


def test_cli_success_and_errors_never_emit_paths_or_input_secrets(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "Users" / "private-account"
    private_root.mkdir(parents=True, mode=0o700)
    os.chmod(private_root, 0o700)
    source = make_source(private_root)
    stable = make_stable_install(private_root)
    destination = private_root / "central"
    run = private_root / "import-run"
    start_import(
        source,
        destination,
        run,
        board_id=BOARD_ID,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_agent_name=OWNER_AGENT,
        stable_install_root=stable,
        confirm_central_stopped=True,
    )
    assert complete_review(run)["phase"] == "installed"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    success = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal_import.py"),
            "status",
            str(run),
            "--confirm-central-stopped",
        ],
        cwd="/private/tmp",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert success.returncode == 0
    assert json.loads(success.stdout)["status"] == "installed"
    assert str(private_root) not in success.stdout + success.stderr
    assert "private-account" not in success.stdout + success.stderr

    parser_failure = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal_import.py"),
            "status",
            str(run),
            "--unknown-secret",
            "/Users/private-account/Bearer-SECRET-INPUT-VALUE",
        ],
        cwd="/private/tmp",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert parser_failure.returncode == 2 and parser_failure.stdout == ""
    assert "private-account" not in parser_failure.stderr
    assert "SECRET-INPUT-VALUE" not in parser_failure.stderr
    assert json.loads(parser_failure.stderr)["error_code"] == "INVALID_ARGUMENTS"

    malformed_case = private_root / "malformed-case"
    malformed_case.mkdir(mode=0o700)
    malformed = make_source(malformed_case)
    payload = json.loads((malformed / "memories.json").read_text())
    payload[0]["priority"] = "Bearer SECRET-INPUT-VALUE"
    (malformed / "memories.json").write_text(json.dumps(payload), encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal_import.py"),
            "import",
            str(malformed),
            str(private_root / "malformed-central"),
            "--run-dir",
            str(private_root / "malformed-run"),
            "--board-id",
            BOARD_ID,
            "--owner-principal-id",
            OWNER_PRINCIPAL,
            "--owner-agent-name",
            OWNER_AGENT,
            "--stable-install-root",
            str(stable),
            "--confirm-central-stopped",
        ],
        cwd="/private/tmp",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 2 and failed.stdout == ""
    rendered = failed.stderr
    for forbidden in (
        str(private_root),
        "private-account",
        "SECRET-INPUT-VALUE",
        "Bearer",
        "Traceback",
    ):
        assert forbidden not in rendered
    assert json.loads(rendered)["error_code"] == "IMPORT_FAILED"

    state_path = run / "state.json"
    corrupt = json.loads(state_path.read_text())
    corrupt["run_root"] = "/Users/private-account/secret-run"
    state_path.write_text(json.dumps(corrupt), encoding="utf-8")
    os.chmod(state_path, 0o600)
    corrupted = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal_import.py"),
            "status",
            str(run),
            "--confirm-central-stopped",
        ],
        cwd="/private/tmp",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert corrupted.returncode == 2 and corrupted.stdout == ""
    assert "private-account" not in corrupted.stderr
    assert "Traceback" not in corrupted.stderr
    assert json.loads(corrupted.stderr)["error_code"] == "IMPORT_FAILED"


@pytest.mark.parametrize(
    "message",
    [
        "decisions must be an owned 0600 bounded regular file",
        "binding decisions do not match the identity worksheet",
    ],
)
def test_cli_surfaces_known_value_error_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    def fail(*_args, **_kwargs):
        raise ValueError(message)

    monkeypatch.setattr(personal_import_module, "status_import", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal_import.py",
            "status",
            "import-run",
            "--confirm-central-stopped",
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        personal_import_module.main()

    captured = capsys.readouterr()
    assert stopped.value.code == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error_code"] == "IMPORT_FAILED"
    assert payload["reason"] == message


def test_cli_sanitizes_value_error_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args, **_kwargs):
        raise ValueError(
            "invalid input at /Users/private-account/secret-run; "
            "Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )

    monkeypatch.setattr(personal_import_module, "status_import", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal_import.py",
            "status",
            "import-run",
            "--confirm-central-stopped",
        ],
    )

    with pytest.raises(SystemExit):
        personal_import_module.main()

    payload = json.loads(capsys.readouterr().err)
    rendered = json.dumps(payload)
    assert payload["reason"].startswith("invalid input at [REDACTED:PATH]")
    assert "private-account" not in rendered
    assert "secret-run" not in rendered
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in rendered


def test_cli_unexpected_exception_stays_generic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("unexpected /Users/private-account/secret-run")

    monkeypatch.setattr(personal_import_module, "status_import", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "personal_import.py",
            "status",
            "import-run",
            "--confirm-central-stopped",
        ],
    )

    with pytest.raises(SystemExit):
        personal_import_module.main()

    payload = json.loads(capsys.readouterr().err)
    assert payload["error_code"] == "IMPORT_FAILED"
    assert "reason" not in payload
    assert "private-account" not in json.dumps(payload)


@pytest.mark.parametrize("lock_kind", ["run", "destination"])
def test_concurrent_operation_lock_is_fail_closed(tmp_path: Path, lock_kind: str) -> None:
    _source, _stable, destination, run, _state = start(tmp_path)
    lock_path = (
        run / ".run.lock"
        if lock_kind == "run"
        else _destination_lock_path(destination)
    )
    worker = (
        "import fcntl,sys; p=open(sys.argv[1],'rb+'); "
        "fcntl.flock(p.fileno(),fcntl.LOCK_EX); print('ready',flush=True); "
        "sys.stdin.read(1)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="another .* operation is active"):
            retry_import(run, confirm_central_stopped=True)
    finally:
        assert process.stdin is not None
        process.stdin.write("x")
        process.stdin.flush()
        process.wait(timeout=5)
    assert process.returncode == 0


def test_cli_crash_switch_is_not_shipped() -> None:
    source = (Path(__file__).parent.parent / "native_import.py").read_text()
    assert "NATIVE_IMPORT_KILL_PHASE" not in source
    assert "SIGKILL" not in source
