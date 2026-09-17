from __future__ import annotations

import json
import plistlib
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]


def test_launchd_template_is_uninstalled_and_shadow_first() -> None:
    template = ROOT / "com.pursers.board-butler.plist.template"
    document = plistlib.loads(template.read_bytes())
    environment = document["EnvironmentVariables"]

    assert document["Label"] == "com.pursers.board-butler"
    assert document["ProgramArguments"] == [
        "/bin/sh",
        "/PATH/TO/Pursers/tools/board-butler/launch.sh",
    ]
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert environment["PURSERS_BUTLER_RUNTIME_MODE"] == "shadow"
    assert environment["PURSERS_BUTLER_TOKEN_PATH"].startswith("/PATH/TO/")
    assert "API_KEY" not in json.dumps(document)


def test_repo_fleet_entry_cannot_activate_from_config_alone() -> None:
    launcher = (ROOT / "launch.sh").read_text(encoding="utf-8")

    assert "runtime_mode=${PURSERS_BUTLER_RUNTIME_MODE:-shadow}" in launcher
    assert "set -- --runtime-mode shadow" in launcher
    assert "PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE" in launcher
    assert "--active-authorization-file" in launcher
    assert "--act-on-board" in launcher


def test_authorize_active_requires_literal_confirmation_and_writes_0600(
    tmp_path: Path,
) -> None:
    script = ROOT / "authorize_active.py"
    target = tmp_path / "active.json"
    denied = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(target),
            "--confirm",
            "active",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert denied.returncode != 0
    assert not target.exists()

    allowed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(target),
            "--confirm",
            "ENABLE-BOARD-BUTLER-ACTIVE",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "mode": "active",
        "authorized": True,
    }
