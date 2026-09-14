from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import zipfile

import pytest

from tools import build_home_runtime_wheelhouse as builder


PYTHON_DETAILS = {
    "executable": "/opt/python3.12",
    "version": "3.12.12",
    "platform": "test-platform",
    "platform_tag": "test_platform_arm64",
}


def _lock_text(source_hash: str) -> str:
    return (
        "# Pursers Home runtime wheelhouse lock. Regenerate deliberately; do not hand-edit.\n"
        "# schema: 1\n"
        "# python: 3.12\n"
        "# platform: test_platform_arm64\n"
        f"# source-requirements-sha256: {source_hash}\n\n"
        f"example==1.2.3 --hash=sha256:{'a' * 64}\n"
    )


def _metadata_wheel(path: Path, name: str, version: str, payload: bytes = b"x") -> Path:
    wheel = path / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{name.replace('-', '_')}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(f"{name}/payload", payload)
    return wheel


def test_stale_lock_rejected_before_resolution(tmp_path: Path) -> None:
    lock = tmp_path / "wheelhouse.lock"
    lock.write_text(_lock_text("0" * 64))

    with pytest.raises(builder.WheelhouseError, match="lock is stale.*--refresh-lock"):
        builder._validate_lock(lock, PYTHON_DETAILS)


def test_lock_requires_exact_versions_and_hashes(tmp_path: Path) -> None:
    lock = tmp_path / "wheelhouse.lock"
    lock.write_text(
        _lock_text(builder._source_requirements_sha256()).replace(
            f"example==1.2.3 --hash=sha256:{'a' * 64}", "example>=1.2.3"
        )
    )

    with pytest.raises(builder.WheelhouseError, match="name==version"):
        builder._validate_lock(lock, PYTHON_DETAILS)


def test_rendered_lock_is_sorted_and_hashes_exact_wheels(tmp_path: Path) -> None:
    zeta = _metadata_wheel(tmp_path, "zeta_pkg", "2.0", b"zeta")
    alpha = _metadata_wheel(tmp_path, "Alpha.Pkg", "1.0", b"alpha")

    rendered = builder._render_lock([zeta, alpha], PYTHON_DETAILS)

    package_lines = [line for line in rendered.splitlines() if not line.startswith("#") and line]
    assert package_lines == [
        f"alpha-pkg==1.0 --hash=sha256:{hashlib.sha256(alpha.read_bytes()).hexdigest()}",
        f"zeta-pkg==2.0 --hash=sha256:{hashlib.sha256(zeta.read_bytes()).hexdigest()}",
    ]


def test_two_builds_from_same_lock_have_identical_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python3.12"
    python.write_bytes(b"python")
    lock = tmp_path / "wheelhouse.lock"
    lock.write_text(_lock_text(builder._source_requirements_sha256()))

    monkeypatch.setattr(builder, "_python_details", lambda unused: PYTHON_DETAILS)
    monkeypatch.setattr(builder, "_source_commit", lambda allow_dirty: ("f" * 40, False))
    monkeypatch.setattr(builder.shutil, "which", lambda command: "/opt/uv")
    monkeypatch.setattr(builder, "_build_environment", lambda unused: {})
    monkeypatch.setattr(builder, "_pip_environment", lambda: {})

    def fake_source_wheels(temp: Path, *unused: object) -> tuple[Path, Path]:
        dist = temp / "source-wheels"
        dist.mkdir()
        client = dist / "pursers_client-0.1.0a23-py3-none-any.whl"
        bridge = dist / "pursers_wait_bridge-0.1.0a16-py3-none-any.whl"
        client.write_bytes(b"deterministic-client")
        bridge.write_bytes(b"deterministic-bridge")
        return client, bridge

    def fake_populate(
        wheelhouse: Path,
        resolver: Path,
        selected_python: Path,
        selected_lock: Path,
        source_wheels: tuple[Path, Path],
        environment: dict[str, str],
    ) -> None:
        del resolver, selected_python, selected_lock, environment
        for wheel in source_wheels:
            shutil.copy2(wheel, wheelhouse / wheel.name)
        (wheelhouse / "example-1.2.3-py3-none-any.whl").write_bytes(
            b"deterministic-dependency"
        )

    monkeypatch.setattr(builder, "_build_source_wheels", fake_source_wheels)
    monkeypatch.setattr(builder, "_populate_locked_wheels", fake_populate)
    monkeypatch.setattr(
        builder,
        "_verify_install",
        lambda *args: {"install": ["locked"], "root_help": "verified"},
    )

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = builder.build(first, python, lock)
    second_manifest = builder.build(second, python, lock)

    assert (first / "SHA256SUMS").read_bytes() == (second / "SHA256SUMS").read_bytes()
    assert first_manifest["artifacts"] == second_manifest["artifacts"]
    assert first_manifest["lock"] == second_manifest["lock"]


def test_build_verifier_ignores_checkout_egg_info_on_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = shutil.which("python3.12")
    if python is None:
        pytest.skip("python3.12 is required")

    client_name, client_version = builder._project(builder.CLIENT_PROJECT)
    fake_source = tmp_path / "checkout-src"
    egg_info = fake_source / "pursers_client.egg-info"
    egg_info.mkdir(parents=True)
    (egg_info / "PKG-INFO").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: {client_name}\n"
        f"Version: {client_version}\n"
    )
    (egg_info / "top_level.txt").write_text("pursers_client\n")
    monkeypatch.setenv("PYTHONPATH", str(fake_source))

    output = tmp_path / "wheelhouse"
    manifest = builder.build(output, Path(python), allow_dirty=True)

    assert "PYTHONPATH" not in builder._build_environment(Path(python))
    assert "PYTHONPATH" not in builder._pip_environment()
    assert manifest["verification"]["imports"] == [
        "pursers_client",
        "pursers_wait_server",
    ]
    assert any(
        artifact["filename"].startswith(
            f"{client_name.replace('-', '_')}-{client_version}-"
        )
        for artifact in manifest["artifacts"]
    )
