#!/usr/bin/env python3
"""Offline, deterministic export/import for one Central board."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CENTRAL_SRC = ROOT / "packages" / "central" / "src" / "pursers_central"
if str(CENTRAL_SRC) not in sys.path:
    sys.path.insert(0, str(CENTRAL_SRC))

from instance_lock import CentralDataLock  # noqa: E402
from journal import _board_token  # noqa: E402
from scrub import Policy, scrub  # noqa: E402


ARCHIVE_FORMAT = "central-board-export"
ARCHIVE_SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _paths(board_id: str) -> dict[str, str]:
    if not isinstance(board_id, str) or not ID_RE.fullmatch(board_id):
        raise ValueError(f"board_id must match {ID_RE.pattern}")
    token = _board_token(board_id)
    return {
        "board": f"boards/{token}.json",
        "journal": f"journals/{token}.json",
        "import": f"imports/{token}.json",
    }


def _normalized_board(board: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(board)
    normalized.pop("generation_token", None)
    normalized.pop("generation_revision", None)
    return normalized


def _counts(board: dict[str, Any], journal: dict[str, Any]) -> dict[str, int]:
    return {
        "journal_events": len(journal.get("rows", [])),
        "members": len(board.get("members", {})),
        "memberships": len(board.get("principal_memberships", {})),
        "memories": len(board.get("memories", [])),
        "tickets": len(board.get("tickets", {})),
    }


def build_manifest(board: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_board(board)
    return {
        "archive_format": ARCHIVE_FORMAT,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "board_id": board.get("board_id"),
        "board_schema_version": int(board.get("schema_version", 0)),
        "counts": _counts(board, journal),
        "hashes": {
            "board_content_sha256": _sha256(normalized),
            "journal_sha256": _sha256(journal),
            "logical_content_sha256": _sha256(
                {"board": normalized, "journal": journal}
            ),
        },
    }


def _read_document(connection: sqlite3.Connection, path: str) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            "SELECT doc FROM documents WHERE path = ?", (path,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError("data directory is not a Central SQLite store") from exc
    if row is None:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError(f"stored document is not an object: {path}")
    return value


def _read_source(data_dir: Path, board_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    db_path = data_dir / "board.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError("Central SQLite store does not exist")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        paths = _paths(board_id)
        board = _read_document(connection, paths["board"])
        if board is None:
            raise ValueError("source board does not exist")
        journal = _read_document(connection, paths["journal"])
    finally:
        connection.close()
    if board.get("board_id") != board_id:
        raise ValueError("board hash collision or corrupt document")
    if journal is None:
        journal = {
            "board_id": board_id,
            "next_seq": 1,
            "compacted_through": 0,
            "rows": [],
        }
    if journal.get("board_id") != board_id:
        raise ValueError("journal board hash collision or corrupt document")
    return board, journal


def make_archive(
    data_dir: str | Path, board_id: str, *, create_lock: bool = False
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    with CentralDataLock(root, create=create_lock):
        board, journal = _read_source(root, board_id)
    return {
        "manifest": build_manifest(board, journal),
        "board": board,
        "journal": journal,
    }


def validate_archive(archive: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(archive, dict):
        raise ValueError("archive must be a JSON object")
    manifest = archive.get("manifest")
    board = archive.get("board")
    journal = archive.get("journal")
    if not all(isinstance(item, dict) for item in (manifest, board, journal)):
        raise ValueError("archive must contain manifest, board, and journal objects")
    expected = build_manifest(board, journal)
    if manifest != expected:
        raise ValueError("archive manifest counts or hashes do not match content")
    if manifest.get("archive_format") != ARCHIVE_FORMAT:
        raise ValueError("unsupported archive format")
    if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ValueError("unsupported archive schema version")
    board_id = manifest.get("board_id")
    _paths(board_id)
    if board.get("board_id") != board_id or journal.get("board_id") != board_id:
        raise ValueError("archive board identifiers disagree")
    return copy.deepcopy(manifest), copy.deepcopy(board), copy.deepcopy(journal)


def _actor_key(key: str) -> bool:
    return "principal" in key or key in {
        "actor",
        "reviewed_by",
        "last_reaped_by",
        "last_abandoned_by",
        "last_reviewer_abandoned_by",
    }


def _principal_values(value: Any, *, actor_context: bool = False) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            context = actor_context or _actor_key(str(key))
            if context and isinstance(item, str) and item.startswith("PR-"):
                found.add(item)
            found.update(_principal_values(item, actor_context=context))
    elif isinstance(value, list):
        for item in value:
            found.update(_principal_values(item, actor_context=actor_context))
    elif actor_context and isinstance(value, str) and value.startswith("PR-"):
        found.add(value)
    return found


def collect_principals(board: dict[str, Any], journal: dict[str, Any]) -> set[str]:
    found = set(board.get("principal_memberships", {}))
    found.update(board.get("principal_revocations", {}))
    found.update(_principal_values(board))
    found.update(_principal_values(journal))
    return {str(item) for item in found if isinstance(item, str) and item}


def _mapped_value(value: Any, mapping: dict[str, str], *, actor_context: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            key: _mapped_value(
                item, mapping, actor_context=actor_context or _actor_key(str(key))
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _mapped_value(item, mapping, actor_context=actor_context) for item in value
        ]
    if actor_context and isinstance(value, str):
        return mapping.get(value, value)
    return value


def _mapped_keyed_principals(value: Any, mapping: dict[str, str]) -> Any:
    if not isinstance(value, dict):
        return value
    mapped: dict[str, Any] = {}
    for old_key, item in value.items():
        new_key = mapping.get(str(old_key), str(old_key))
        if new_key in mapped:
            raise ValueError(f"principal mapping collides at {new_key}")
        mapped[new_key] = _mapped_value(item, mapping, actor_context=True)
    return mapped


def apply_principal_map(
    board: dict[str, Any], journal: dict[str, Any], mapping: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    mapped_board = _mapped_value(copy.deepcopy(board), mapping)
    mapped_journal = _mapped_value(copy.deepcopy(journal), mapping)
    for field in ("principal_memberships", "principal_revocations"):
        if field in board:
            mapped_board[field] = _mapped_keyed_principals(board[field], mapping)
    return mapped_board, mapped_journal


def scrub_report(board: dict[str, Any], journal: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key], f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, str):
            _, matches = scrub(value, Policy(mode="redact"))
            violations.extend(
                {
                    "path": path,
                    "rule": match.rule,
                    "start": match.start,
                    "end": match.end,
                }
                for match in matches
            )

    visit(board, "$.board")
    visit(journal, "$.journal")
    return violations


def parse_principal_maps(values: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values:
        old, separator, new = value.partition("=")
        if not separator or not old or not new:
            raise ValueError("--principal-map must use old=new")
        if old in mapping and mapping[old] != new:
            raise ValueError(f"conflicting principal mapping for {old}")
        mapping[old] = new
    return mapping


def export_board(
    data_dir: str | Path, board_id: str, archive_path: str | Path, *, commit: bool
) -> dict[str, Any]:
    archive = make_archive(data_dir, board_id, create_lock=commit)
    destination = Path(archive_path).resolve()
    if destination.exists():
        raise FileExistsError("archive path already exists")
    violations = scrub_report(archive["board"], archive["journal"])
    result = {
        "ok": True,
        "operation": "export",
        "committed": commit,
        "archive_path": str(destination),
        "manifest": archive["manifest"],
        "scrub_profile": "strict",
        "scrub_violations": violations,
    }
    if commit:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    archive,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return result


def _initialize_target(connection: sqlite3.Connection) -> None:
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise RuntimeError(f"SQLite refused WAL mode: {mode}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            path TEXT PRIMARY KEY,
            doc JSON NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1)
        )
        """
    )


def _target_has_board(data_dir: Path, board_id: str) -> bool:
    db_path = data_dir / "board.sqlite3"
    if not db_path.exists():
        return False
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        paths = _paths(board_id)
        try:
            placeholders = ",".join("?" for _ in paths)
            row = connection.execute(
                f"SELECT 1 FROM documents WHERE path IN ({placeholders}) LIMIT 1",
                tuple(paths.values()),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None
    finally:
        connection.close()


def import_board(
    data_dir: str | Path,
    archive_path: str | Path,
    *,
    principal_map: dict[str, str] | None = None,
    require_full_map: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    source = Path(archive_path).resolve()
    archive = json.loads(source.read_text(encoding="utf-8"))
    source_manifest, board, journal = validate_archive(archive)
    mapping = principal_map or {}
    principals = collect_principals(board, journal)
    unmapped = sorted(principals - mapping.keys())
    mapped_board, mapped_journal = apply_principal_map(board, journal, mapping)
    mapped_manifest = build_manifest(mapped_board, mapped_journal)
    violations = scrub_report(mapped_board, mapped_journal)
    root = Path(data_dir).resolve()
    board_id = str(source_manifest["board_id"])
    with CentralDataLock(root, create=commit):
        if _target_has_board(root, board_id):
            raise FileExistsError("target board already exists")
        result = {
            "ok": True,
            "operation": "import",
            "committed": commit,
            "board_id": board_id,
            "source_manifest": source_manifest,
            "mapped_manifest": mapped_manifest,
            "principal_map": dict(sorted(mapping.items())),
            "unmapped_principals": unmapped,
            "scrub_profile": "strict",
            "scrub_violations": violations,
        }
        if require_full_map and unmapped:
            raise ValueError(
                "full principal mapping required; unmapped principals: "
                + ", ".join(unmapped)
            )
        if commit and violations:
            rules = ", ".join(sorted({item["rule"] for item in violations}))
            raise ValueError(f"import rejected by target scrub profile: {rules}")
        if not commit:
            return result

        previous_revision = mapped_board.get("generation_revision", 0)
        if isinstance(previous_revision, bool) or not isinstance(previous_revision, int):
            raise ValueError("source board generation revision is invalid")
        generation_revision = max(1, previous_revision + 1)
        generation_token = "GEN-" + secrets.token_urlsafe(32)
        mapped_board["generation_token"] = generation_token
        mapped_board["generation_revision"] = generation_revision
        import_document = {
            "board_id": board_id,
            "status": "complete",
            "generation_token": generation_token,
            "generation_revision": generation_revision,
            "source_logical_content_sha256": source_manifest["hashes"][
                "logical_content_sha256"
            ],
            "imported_logical_content_sha256": mapped_manifest["hashes"][
                "logical_content_sha256"
            ],
        }
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(root / "board.sqlite3", isolation_level=None)
        readback_manifest: dict[str, Any]
        try:
            _initialize_target(connection)
            connection.execute("BEGIN IMMEDIATE")
            paths = _paths(board_id)
            placeholders = ",".join("?" for _ in paths)
            if connection.execute(
                f"SELECT 1 FROM documents WHERE path IN ({placeholders}) LIMIT 1",
                tuple(paths.values()),
            ).fetchone():
                raise FileExistsError("target board already exists")
            for path, document in (
                (paths["board"], mapped_board),
                (paths["journal"], mapped_journal),
                (paths["import"], import_document),
            ):
                connection.execute(
                    "INSERT INTO documents(path, doc, version) VALUES (?, ?, 1)",
                    (path, _canonical(document).decode("utf-8")),
                )
            read_board = _read_document(connection, paths["board"])
            read_journal = _read_document(connection, paths["journal"])
            if read_board is None or read_journal is None:
                raise RuntimeError("import read-back is missing written documents")
            readback_manifest = build_manifest(read_board, read_journal)
            if readback_manifest != mapped_manifest:
                raise RuntimeError(
                    "import read-back counts or hashes do not match manifest"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        result.update(
            {
                "generation_revision": generation_revision,
                "generation_token_sha256": hashlib.sha256(
                    generation_token.encode("utf-8")
                ).hexdigest(),
                "readback_manifest": readback_manifest,
            }
        )
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline export/import for one Central board (dry-run by default)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--data-dir", type=Path, required=True)
    export.add_argument("--board-id", required=True)
    export.add_argument("--archive", type=Path, required=True)
    export.add_argument("--commit", action="store_true")
    import_ = subparsers.add_parser("import")
    import_.add_argument("--data-dir", type=Path, required=True)
    import_.add_argument("--archive", type=Path, required=True)
    import_.add_argument("--principal-map", action="append", default=[], metavar="OLD=NEW")
    import_.add_argument("--require-full-map", action="store_true")
    import_.add_argument("--commit", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_board(
                args.data_dir, args.board_id, args.archive, commit=args.commit
            )
        else:
            result = import_board(
                args.data_dir,
                args.archive,
                principal_map=parse_principal_maps(args.principal_map),
                require_full_map=args.require_full_map,
                commit=args.commit,
            )
    except (
        FileNotFoundError,
        FileExistsError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
