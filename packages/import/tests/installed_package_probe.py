"""Isolated exact-wheel Personal import lifecycle probe using synthetic data."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pursers_personal_import
from pursers_personal_import.personal_import import (
    review_import,
    retry_import,
    rollback_import,
    stable_install_state,
    start_import,
    status_import,
    tree_state,
)


def private_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> None:
    fixture = Path(sys.argv[1]).resolve(strict=True)
    external_stable = (
        Path(sys.argv[2]).resolve(strict=True) if len(sys.argv) > 2 else None
    )
    imported = Path(pursers_personal_import.__file__).resolve(strict=True)
    if "site-packages" not in imported.parts:
        raise AssertionError("probe did not import the installed wheel")
    for generic in (
        "bind_identities",
        "native_import",
        "personal_import",
        "safe_tree",
        "scrub",
    ):
        if importlib.util.find_spec(generic) is not None:
            raise AssertionError("wheel exposed a generic top-level module")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve(strict=True)
        os.chmod(root, 0o700)
        source = root / "legacy" / ".agent-mem"
        source.parent.mkdir(mode=0o700)
        shutil.copytree(fixture, source)
        (source / ".board.lock").write_bytes(b"")
        os.chmod(source, 0o700)
        os.chmod(source / ".board.lock", 0o600)

        stable = external_stable
        if stable is None:
            stable = root / "Cellar" / "onboard-memory" / "4.0.4"
            executable = stable / "libexec" / "bin" / "onboard-memory-mcp"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            os.chmod(executable, 0o755)
            command = stable / "bin" / "onboard-memory-mcp"
            command.parent.mkdir()
            command.symlink_to("../libexec/bin/onboard-memory-mcp")
            active = root / "bin" / "onboard-memory-mcp"
            active.parent.mkdir()
            active.symlink_to(
                "../Cellar/onboard-memory/4.0.4/libexec/bin/onboard-memory-mcp"
            )
            (stable / "INSTALL_RECEIPT.json").write_text(
                json.dumps(
                    {
                        "source": {
                            "spec": "stable",
                            "versions": {"stable": "4.0.4"},
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        source_before = tree_state(source)
        stable_before = stable_install_state(stable)
        run = root / "run"
        destination = root / "central"
        state = start_import(
            source,
            destination,
            run,
            board_id="board-installed-probe",
            owner_principal_id="principal-installed-probe",
            owner_agent_name="agent-installed-probe",
            stable_install_root=stable,
            confirm_central_stopped=True,
        )
        if state["phase"] != "review_required" or destination.exists():
            raise AssertionError("installed workflow bypassed mandatory review")
        worksheet = json.loads(
            (run / "evidence" / "quarantine-worksheet.json").read_text()
        )
        decisions = run / "evidence" / "probe-decisions.json"
        private_json(
            decisions,
            {
                "schema_version": 1,
                "board_id": state["board_id"],
                "worksheet_sha256": worksheet["worksheet_sha256"],
                "entry_count": worksheet["entry_count"],
                "status": "REVIEWED-SIGNED-READY",
                "review_metadata": {"reviewed_at": "2026-08-19T00:00:00+00:00"},
                "entries": [
                    {**row, "decision": "drop"} for row in worksheet["entries"]
                ],
            },
        )
        template = json.loads(
            (run / "evidence" / "identity-bindings-template.json").read_text()
        )
        bindings = run / "evidence" / "probe-bindings.json"
        mapping = {key: "RETIRE" for key in template["bindings"]}
        private_json(
            bindings,
            {
                "schema_version": 1,
                "board_id": state["board_id"],
                "identity_worksheet_sha256": template[
                    "identity_worksheet_sha256"
                ],
                "entry_count": len(mapping),
                "bindings": mapping,
            },
        )
        installed = review_import(
            run,
            bindings_path=bindings,
            decisions_path=decisions,
            confirm_central_stopped=True,
        )
        if installed["phase"] != "installed":
            raise AssertionError("installed workflow did not complete")
        if status_import(run, confirm_central_stopped=True)["integrity"] != "verified":
            raise AssertionError("installed workflow status was not verified")
        if retry_import(run, confirm_central_stopped=True)["phase"] != "installed":
            raise AssertionError("installed workflow retry was not idempotent")
        if rollback_import(run, confirm_central_stopped=True)["phase"] != "rolled_back":
            raise AssertionError("installed workflow rollback failed")
        if tree_state(source) != source_before or stable_install_state(stable) != stable_before:
            raise AssertionError("installed workflow changed source or stable v4")
        if destination.exists():
            raise AssertionError("rollback did not restore the absent baseline")
        for proof in (run / "install-receipt.json", run / "rollback-receipt.json"):
            if stat.S_IMODE(proof.stat().st_mode) != 0o600:
                raise AssertionError("installed receipt is not private")

    print(
        json.dumps(
            {
                "status": "PASS",
                "version": pursers_personal_import.__version__,
                "installed_import": True,
                "generic_top_level_modules": 0,
                "mandatory_review": True,
                "status_verified": True,
                "retry_idempotent": True,
                "rollback_verified": True,
                "stable_install_scope": (
                    "external-installed-4.0.4"
                    if external_stable is not None
                    else "synthetic-4.0.4-sentinel"
                ),
                "remote_calls": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
