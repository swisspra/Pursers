from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from tools.regenerate_component_lock import BUILD_TOOLCHAIN
from tools.verify_publish_wheels import main


SETUPTOOLS_VERSION = dict(BUILD_TOOLCHAIN)["setuptools"]


def _wheel(path: Path, distribution: str, version: str, files: dict[str, bytes]) -> Path:
    wheel = path / f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            f"Generator: setuptools ({SETUPTOOLS_VERSION})\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for name, payload in files.items():
            archive.writestr(name, payload)
    return wheel


def _fixture_wheels(path: Path, *, client_hash: str | None = None) -> None:
    central = _wheel(path, "pursers-central", "1", {"central.py": b"central\n"})
    client = _wheel(path, "pursers-client", "2", {"client.py": b"client\n"})
    lock = {
        "schema_version": 1,
        "build_toolchain": dict(BUILD_TOOLCHAIN),
        "components": {
            "pursers-central": {
                "version": "1",
                "wheel_sha256": hashlib.sha256(central.read_bytes()).hexdigest(),
            },
            "pursers-client": {
                "version": "2",
                "wheel_sha256": client_hash
                or hashlib.sha256(client.read_bytes()).hexdigest(),
            },
        },
    }
    _wheel(
        path,
        "pursers-personal",
        "3",
        {
            "pursers_personal/resources/component-lock.json": json.dumps(
                lock
            ).encode("utf-8")
        },
    )


def test_publish_wheel_gate_accepts_matching_components(
    tmp_path: Path, capsys
) -> None:
    _fixture_wheels(tmp_path)

    assert main(["--wheel-dir", str(tmp_path)]) == 0
    assert "publish_wheel_verification=pass" in capsys.readouterr().out


def test_publish_wheel_gate_names_mismatched_component(
    tmp_path: Path, capsys
) -> None:
    _fixture_wheels(tmp_path, client_hash="0" * 64)

    assert main(["--wheel-dir", str(tmp_path)]) != 0
    error = capsys.readouterr().err
    assert "pursers-client wheel sha256 mismatch" in error
