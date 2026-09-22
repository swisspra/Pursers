from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from tools import verify_publish_wheels
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


def _published_release(wheel: Path, payload: bytes) -> dict[str, object]:
    return {
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "filename": wheel.name,
                "url": "https://files.example.invalid/published.whl",
                "digests": {"sha256": hashlib.sha256(payload).hexdigest()},
            }
        ]
    }


def test_pypi_gate_accepts_identical_published_wheel(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    wheel = _wheel(tmp_path, "pursers-client", "2", {"client.py": b"client\n"})
    published = wheel.read_bytes()
    monkeypatch.setattr(
        verify_publish_wheels,
        "_pypi_release",
        lambda *_args, **_kwargs: _published_release(wheel, published),
    )
    monkeypatch.setattr(verify_publish_wheels, "_download", lambda _url: published)

    assert (
        main(["--wheel-dir", str(tmp_path), "--generators-only", "--verify-pypi"])
        == 0
    )
    assert "pypi_artifact_match=pursers-client==2" in capsys.readouterr().out


def test_pypi_gate_rejects_changed_content_without_version_bump(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    wheel = _wheel(tmp_path, "pursers-client", "2", {"client.py": b"changed\n"})
    published = b"published wheel bytes"
    monkeypatch.setattr(
        verify_publish_wheels,
        "_pypi_release",
        lambda *_args, **_kwargs: _published_release(wheel, published),
    )
    monkeypatch.setattr(verify_publish_wheels, "_download", lambda _url: published)

    assert (
        main(["--wheel-dir", str(tmp_path), "--generators-only", "--verify-pypi"])
        != 0
    )
    error = capsys.readouterr().err
    assert "published wheel content mismatch for pursers-client==2" in error
    assert "bump the version" in error


def test_pypi_gate_allows_new_version(tmp_path: Path, monkeypatch, capsys) -> None:
    _wheel(tmp_path, "pursers-client", "3", {"client.py": b"new\n"})
    monkeypatch.setattr(
        verify_publish_wheels,
        "_pypi_release",
        lambda *_args, **_kwargs: None,
    )

    assert (
        main(["--wheel-dir", str(tmp_path), "--generators-only", "--verify-pypi"])
        == 0
    )
    assert "pypi_version_new=pursers-client==3" in capsys.readouterr().out


def test_publish_workflow_checks_pypi_before_both_uploads() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/publish-pypi.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("--verify-pypi") == 2
    assert workflow.index("--verify-pypi") < workflow.index("Publish to PyPI")
    bridge = workflow.split("publish-wait-bridge:", 1)[1]
    assert bridge.index("--verify-pypi") < bridge.index("Publish to PyPI")


def test_publish_workflow_serializes_new_version_check_and_upload() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/publish-pypi.yml"
    ).read_text(encoding="utf-8")

    concurrency = "concurrency:\n  group: publish-pypi\n  cancel-in-progress: false"
    assert workflow.count(concurrency) == 1
    assert workflow.index(concurrency) < workflow.index("jobs:")


def test_serialized_later_run_rejects_artifact_published_by_first_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    first_run = tmp_path / "first"
    second_run = tmp_path / "second"
    first_run.mkdir()
    second_run.mkdir()
    first_wheel = _wheel(
        first_run, "pursers-client", "3", {"client.py": b"first run\n"}
    )
    second_wheel = _wheel(
        second_run, "pursers-client", "3", {"client.py": b"second run\n"}
    )
    assert first_wheel.name == second_wheel.name

    published: bytes | None = None

    def release(*_args, **_kwargs):
        if published is None:
            return None
        return _published_release(first_wheel, published)

    def download(_url: str) -> bytes:
        assert published is not None
        return published

    monkeypatch.setattr(
        verify_publish_wheels,
        "_pypi_release",
        release,
    )
    monkeypatch.setattr(verify_publish_wheels, "_download", download)

    assert (
        main(
            [
                "--wheel-dir",
                str(first_run),
                "--generators-only",
                "--verify-pypi",
            ]
        )
        == 0
    )

    # The concurrency group makes the later dispatch wait. Its pre-upload
    # check therefore runs only after the first dispatch has published.
    published = first_wheel.read_bytes()
    assert (
        main(
            [
                "--wheel-dir",
                str(second_run),
                "--generators-only",
                "--verify-pypi",
            ]
        )
        != 0
    )
    error = capsys.readouterr().err
    assert "published wheel content mismatch for pursers-client==3" in error
    assert "bump the version" in error
