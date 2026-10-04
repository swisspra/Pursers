from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

ACP_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ACP_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "runner_installer", ACP_ROOT / "runner_installer.py"
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def archive(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as target:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            target.addfile(info, io.BytesIO(payload))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            target.addfile(info)
    return stream.getvalue()


def resolved(payload: bytes, *, command: str = "bin/agent") -> dict[str, object]:
    return {
        "schema": "pursers_acp_resolved_runner_v1",
        "agent_id": "safe-agent",
        "agent_version": "1.2.3",
        "platform": "darwin-aarch64",
        "registry_revision": "sha256:" + "a" * 64,
        "distribution": {
            "kind": "binary",
            "source": {"archive": "https://example.invalid/agent.tgz"},
            "integrity": {
                "algorithm": "sha256",
                "digest": hashlib.sha256(payload).hexdigest(),
            },
        },
        "launch": {"argv": [command, "acp"], "cwd": "install_root"},
    }


def test_binary_install_is_verified_atomic_and_idempotent(tmp_path: Path) -> None:
    payload = archive({"bin/agent": b"#!/bin/sh\nexit 0\n"})
    selection = resolved(payload)

    preview = installer.installation_preview(selection, tmp_path / "cache")
    assert preview["action"] == "download_verify_extract"
    first = installer.install_binary(
        selection, tmp_path / "cache", fetch=lambda _url, _maximum: payload
    )
    second = installer.install_binary(
        selection,
        tmp_path / "cache",
        fetch=lambda _url, _maximum: pytest.fail("cached install downloaded again"),
    )

    assert first["cached"] is False
    assert second["cached"] is True
    assert first["command"][0].endswith("/bin/agent")
    assert Path(first["command"][0]).is_file()
    assert installer.installation_preview(selection, tmp_path / "cache")["ready"]


def install_for_cache_test(tmp_path: Path) -> tuple[dict[str, object], bytes, Path]:
    payload = archive(
        {"bin/agent": b"#!/bin/sh\nexit 0\n", "share/data": b"verified\n"}
    )
    selection = resolved(payload)
    receipt = installer.install_binary(
        selection, tmp_path / "cache", fetch=lambda _url, _maximum: payload
    )
    return selection, payload, Path(receipt["install_root"])


def assert_invalid_cache(
    tmp_path: Path, selection: dict[str, object]
) -> None:
    preview = installer.installation_preview(selection, tmp_path / "cache")
    assert preview["ready"] is False
    assert preview["action"] == "repair_invalid_cache"
    assert preview["blocked_reason"] == "cached_install_invalid"
    with pytest.raises(installer.InstallError, match="cached_install_invalid"):
        installer.install_binary(
            selection,
            tmp_path / "cache",
            fetch=lambda _url, _maximum: pytest.fail(
                "invalid cache must fail before downloading"
            ),
        )


def test_cached_install_rejects_deleted_command(tmp_path: Path) -> None:
    selection, _payload, root = install_for_cache_test(tmp_path)
    (root / "bin/agent").unlink()

    assert_invalid_cache(tmp_path, selection)


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_cached_install_rejects_non_regular_command(
    tmp_path: Path, replacement: str
) -> None:
    selection, _payload, root = install_for_cache_test(tmp_path)
    command = root / "bin/agent"
    command.unlink()
    if replacement == "symlink":
        command.symlink_to(root / "share/data")
    else:
        command.mkdir()

    assert_invalid_cache(tmp_path, selection)


def test_cached_install_rejects_non_executable_command(tmp_path: Path) -> None:
    selection, _payload, root = install_for_cache_test(tmp_path)
    command = root / "bin/agent"
    command.chmod(command.stat().st_mode & ~0o111)

    assert_invalid_cache(tmp_path, selection)


@pytest.mark.parametrize("relative", ["bin/agent", "share/data"])
def test_cached_install_rejects_content_tampering(
    tmp_path: Path, relative: str
) -> None:
    selection, _payload, root = install_for_cache_test(tmp_path)
    target = root / relative
    target.write_bytes(b"tampered\n")

    assert_invalid_cache(tmp_path, selection)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (archive({"../escape": b"bad"}), "archive_member_unsafe"),
        (
            archive({"bin/agent": b"ok"}, symlink="link"),
            "archive_link_or_special_forbidden",
        ),
    ],
)
def test_binary_install_rejects_archive_escape_and_links(
    tmp_path: Path, payload: bytes, error: str
) -> None:
    with pytest.raises(installer.InstallError, match=error):
        installer.install_binary(
            resolved(payload), tmp_path / "cache", fetch=lambda _url, _maximum: payload
        )


def test_binary_install_requires_registry_integrity(tmp_path: Path) -> None:
    payload = archive({"bin/agent": b"ok"})
    selection = resolved(payload)
    selection["distribution"]["integrity"] = None  # type: ignore[index]

    preview = installer.installation_preview(selection, tmp_path / "cache")
    assert preview["blocked_reason"] == "binary_integrity_required"
    with pytest.raises(installer.InstallError, match="binary_integrity_required"):
        installer.install_binary(selection, tmp_path / "cache")


def test_package_preview_is_non_installing_and_honest(tmp_path: Path) -> None:
    selection = {
        "schema": "pursers_acp_resolved_runner_v1",
        "agent_id": "safe-agent",
        "agent_version": "1.2.3",
        "platform": "linux-x86_64",
        "registry_revision": "sha256:" + "a" * 64,
        "distribution": {
            "kind": "npx",
            "source": {"package": "safe-agent@1.2.3"},
            "integrity": None,
        },
        "launch": {"argv": ["npx", "safe-agent@1.2.3", "--acp"], "cwd": None},
    }
    preview = installer.installation_preview(selection, tmp_path / "cache")
    assert preview["action"] == "use_pinned_package_launcher"
    assert preview["network_at_launch"] is True
    assert not (tmp_path / "cache").exists()
