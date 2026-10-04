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
