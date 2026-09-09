from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import zipfile

import pytest

from tools import home_acceptance_handoff as handoff


SHA = "a" * 40
RUNTIME_SHA = "f5a24301262bcbe56d993276362dc74d6f1728ad"


def _file(path: Path, data: bytes = b"x", executable: bool = False) -> Path:
    path.write_bytes(data)
    path.chmod(0o700 if executable else 0o600)
    return path


def _args(tmp_path: Path):
    checkout = tmp_path / "candidate"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=checkout, check=True)
    _file(checkout / "tracked")
    subprocess.run(["git", "add", "tracked"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=checkout, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("webui/candidate.json", json.dumps({"candidate_commit": commit}))
    observer = _file(tmp_path / "runner.py", b"runner")
    backend = _file(tmp_path / "browser_observer.py", b"observer")
    harness = _file(tmp_path / "harness.py", b"harness")
    host = tmp_path / "AionUi.app"
    core = host / "Contents" / "Resources" / "bundled-aioncore" / "darwin-arm64" / "aioncore"
    core.parent.mkdir(parents=True)
    _file(core, executable=True)
    with (host / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "com.aionui.app", "CFBundleShortVersionString": "2.2.1"}, stream)
    codesign = _file(
        tmp_path / "codesign",
        b"#!/bin/sh\ncase \"$1\" in (-dvvv) printf '%s\\n' 'Identifier=com.aionui.app' 'TeamIdentifier=52JQX2HUSC' 'Authority=Developer ID Application: AionUi Inc. (52JQX2HUSC)' 'Notarization Ticket=stapled' 'CDHash=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';; esac\n",
        executable=True,
    )
    values = {
        "candidate_checkout": str(checkout), "commit": commit, "candidate_zip": str(package),
        "sandbox_root": str(tmp_path / "handoff"), "board": "sandbox-home-acceptance",
        "origin": "http://127.0.0.1:25808", "page_path": "/api/extensions/pursers/assets/webui/index.html",
        "observer_runner": str(observer),
        "observer_sha256": hashlib.sha256(b"runner").hexdigest(),
        "observer_backend": str(backend), "observer_backend_sha256": hashlib.sha256(b"observer").hexdigest(),
        "observer_harness": str(harness), "observer_harness_sha256": hashlib.sha256(b"harness").hexdigest(),
        "observer_install_dir": str(tmp_path / "reviewer-owned-observer"),
        "helper": str(_file(tmp_path / "helper.cjs")), "helper_sha256": hashlib.sha256(b"x").hexdigest(),
        "node": str(_file(tmp_path / "node", executable=True)),
        "bridge_bin": str(_file(tmp_path / "bridge", executable=True)),
        "aioncore_bin": str(core),
        "ego_browser": str(_file(tmp_path / "ego-browser", executable=True)),
        "host_bundle": str(host), "host_cdhash": "b" * 40, "codesign": str(codesign),
        "host_version": "2.2.1", "runtime_commit": RUNTIME_SHA, "core_version": "0.2.1",
        "task_space": "acceptance-test",
    }
    return type("Args", (), values)()


def test_prepare_writes_private_reproducible_handoff(tmp_path: Path, capsys) -> None:
    args = _args(tmp_path)
    result = handoff.prepare(args)
    root = Path(result["handoff"])
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("helper-token", "fresh_install.assertions.json", "handoff.json"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    for name in ("start-aioncore.sh", "start-helper.sh", "reviewer-commands.sh", "cleanup.sh"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o700
        subprocess.run(["sh", "-n", str(root / name)], check=True)
    manifest = json.loads((root / "handoff.json").read_text())
    token = (root / "helper-token").read_text().strip()
    assert len(token) == 64
    assert token not in json.dumps(manifest)
    assert manifest["candidate"]["commit"] == args.commit
    commands = (root / "reviewer-commands.sh").read_text()
    assert "install-observer" in commands
    assert "doctor" in commands
    assert "capture" in commands
    assert "validate" in commands
    assert "I_UNDERSTAND_SANDBOX_ONLY" in commands
    assert "test_live_host.py" in commands
    assert all(name in commands for name in handoff.OBSERVATIONS)
    assert "http://127.0.0.1:25808/api/extensions/pursers/assets/webui/index.html" in commands
    assert (root / "extensions" / "pursers-home" / "webui" / "candidate.json").is_file()
    assert not Path(args.observer_install_dir).exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("board", ["pursers", "production-home", "sandbox-"])
def test_refuses_non_sandbox_board(tmp_path: Path, board: str) -> None:
    args = _args(tmp_path)
    args.board = board
    with pytest.raises(handoff.HandoffError, match="board"):
        handoff.prepare(args)


def test_refuses_non_loopback_origin(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.origin = "https://example.com:25808"
    with pytest.raises(handoff.HandoffError, match="loopback"):
        handoff.prepare(args)


def test_refuses_zip_bound_to_different_commit(tmp_path: Path) -> None:
    args = _args(tmp_path)
    with zipfile.ZipFile(args.candidate_zip, "w") as archive:
        archive.writestr("webui/candidate.json", json.dumps({"candidate_commit": SHA}))
    with pytest.raises(handoff.HandoffError, match="ZIP commit"):
        handoff.prepare(args)


def test_refuses_sandbox_inside_candidate(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.sandbox_root = str(Path(args.candidate_checkout) / "private")
    with pytest.raises(handoff.HandoffError, match="sandbox root"):
        handoff.prepare(args)


def test_refuses_unapproved_observer(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.observer_sha256 = "0" * 64
    with pytest.raises(handoff.HandoffError, match="observer runner SHA-256"):
        handoff.prepare(args)


def test_refuses_aioncore_outside_signed_bundle(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.aioncore_bin = str(_file(tmp_path / "outside-core", executable=True))
    with pytest.raises(handoff.HandoffError, match="signed host bundle"):
        handoff.prepare(args)
