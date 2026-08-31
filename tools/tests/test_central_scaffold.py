from __future__ import annotations

import json
import plistlib
import shlex
import socket
import stat
from pathlib import Path

from tools import central_scaffold


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _init(tmp_path: Path, capsys, *, label: str = "com.pursers.work") -> Path:
    root = tmp_path / "central-work"
    assert (
        central_scaffold.main(
            [
                "init",
                "--root",
                str(root),
                "--name",
                label,
                "--port",
                str(_free_port()),
            ]
        )
        == 0
    )
    capsys.readouterr()
    return root


def test_init_creates_secret_free_layout_permissions_and_runbook(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "central-personal"
    port = _free_port()
    label = "com.pursers.personal"

    assert central_scaffold.main(
        ["init", "--root", str(root), "--name", label, "--port", str(port)]
    ) == 0
    output = capsys.readouterr().out

    assert _mode(root) == 0o700
    for name in ("data", "jwt", "logs"):
        assert (root / name).is_dir()
        assert _mode(root / name) == 0o700
    assert sorted(path.name for path in (root / "jwt").iterdir()) == ["README.md"]
    assert json.loads((root / "project-registry.json").read_text()) == {
        "schema_version": 1,
        "projects": {},
    }
    profile = (root / "profile.env").read_text()
    assert f"CENTRAL_PORT={port}" in profile
    assert "PURSERS_CENTRAL_WHEEL=FILL-ME" in profile
    assert "PURSERS_CENTRAL_WHEEL_SHA256=FILL-ME" in profile
    plist = plistlib.loads((root / f"{label}.plist").read_bytes())
    assert plist["Label"] == label
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [str(root / "launch-central.sh")]
    assert _mode(root / "launch-central.sh") == 0o700

    assert "RUNBOOK" in output
    for required in (
        "Generate JWKS/keys",
        "Mint principal tokens",
        "Fill profile.env",
        "fresh venv",
        "launchctl bootstrap",
        "seat_admin.py add",
        "--token-path <SEAT_TOKEN_FILE>",
        "board_move.py import",
    ):
        assert required in output
    assert "/" + "Users/" not in output


def test_check_reports_every_placeholder_and_missing_operator_file(
    tmp_path: Path, capsys
) -> None:
    root = _init(tmp_path, capsys)

    assert central_scaffold.main(["check", "--root", str(root)]) == 1
    output = capsys.readouterr().out

    assert "layout=ok" in output
    assert "CENTRAL_JWT_ISSUER" in output
    for component in central_scaffold.WHEEL_COMPONENTS:
        assert f"{component}_WHEEL" in output
        assert f"{component}_WHEEL_SHA256" in output
    assert "jwt/issuer_key.pem" in output
    assert "jwt/jwks.json" in output
    assert "check=incomplete" in output


def test_check_refuses_a_listening_port(tmp_path: Path, capsys) -> None:
    root = tmp_path / "central-listening"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = int(server.getsockname()[1])
        assert central_scaffold.main(
            [
                "init",
                "--root",
                str(root),
                "--name",
                "com.pursers.listening",
                "--port",
                str(port),
            ]
        ) == 0
        capsys.readouterr()
        assert central_scaffold.main(["check", "--root", str(root)]) == 2

    output = capsys.readouterr().out
    assert f"port=listening-refused:{port}" in output
    assert "configured port already has a listener" in output


def test_init_refuses_existing_root_without_touching_it(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "already-here"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("untouched")

    assert central_scaffold.main(
        [
            "init",
            "--root",
            str(root),
            "--name",
            "com.pursers.existing",
            "--port",
            str(_free_port()),
        ]
    ) == 2

    assert sentinel.read_text() == "untouched"
    assert sorted(path.name for path in root.iterdir()) == ["keep.txt"]
    assert "refusing to initialize an existing root" in capsys.readouterr().err


def test_init_refuses_nested_instance_root(tmp_path: Path, capsys) -> None:
    parent = _init(tmp_path, capsys)
    nested = parent / "data" / "another-central"

    assert central_scaffold.main(
        [
            "init",
            "--root",
            str(nested),
            "--name",
            "com.pursers.nested",
            "--port",
            str(_free_port()),
        ]
    ) == 2

    assert not nested.exists()
    assert "inside an existing instance root" in capsys.readouterr().err


def test_check_passes_after_operator_placeholders_are_filled(
    tmp_path: Path, capsys
) -> None:
    root = _init(tmp_path, capsys)
    profile = root / "profile.env"
    profile.write_text(profile.read_text().replace("FILL-ME", "VERIFIED-VALUE"))
    secret_marker = "TEST-KEY-CONTENT-MUST-NOT-BE-PRINTED"
    (root / "jwt" / "issuer_key.pem").write_text(secret_marker)
    (root / "jwt" / "issuer_key.pem").chmod(0o600)
    (root / "jwt" / "jwks.json").write_text('{"keys": []}\n')

    assert central_scaffold.main(["check", "--root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "placeholders=none" in output
    assert "operator_files_missing=none" in output
    assert "check=ok" in output
    assert secret_marker not in output


def test_root_with_spaces_produces_a_sourceable_profile(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "central work domain"
    port = _free_port()
    assert central_scaffold.main(
        [
            "init",
            "--root",
            str(root),
            "--name",
            "com.pursers.spaces",
            "--port",
            str(port),
        ]
    ) == 0
    capsys.readouterr()

    values = central_scaffold._load_profile(root / "profile.env")
    assert values["CENTRAL_DATA_DIR"] == str(root / "data")
    assert values["CENTRAL_JWKS_PATH"] == str(root / "jwt" / "jwks.json")
    launcher = (root / "launch-central.sh").read_text()
    assert shlex.quote(str(root / "profile.env")) in launcher
