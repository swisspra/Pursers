"""Fail-safe recovery contracts for interrupted first profile creation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import pursers_client.personal_profile as profile_module
from pursers_client import (
    ProfileSecurityError,
    ensure_personal_profile,
    load_personal_profile,
    profile_path_for_project,
    read_capability,
)


def private_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return parent


def project(parent: Path, name: str) -> Path:
    value = parent / name
    value.mkdir(mode=0o700)
    return value


def write_private(path: Path, content: bytes = b"partial") -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def test_interrupted_first_creation_recovers_without_reusing_partial_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    project_root = project(parent, "interrupted")
    original_write = profile_module._atomic_write_private
    calls = 0

    def interrupt_second_write(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic first-create interruption")
        original_write(*args, **kwargs)

    monkeypatch.setattr(
        profile_module,
        "_atomic_write_private",
        interrupt_second_write,
    )
    with pytest.raises(RuntimeError, match="synthetic first-create interruption"):
        ensure_personal_profile(project_root, profiles_root=profiles)

    profile_path = profile_path_for_project(project_root, profiles)
    partial_files = tuple(
        path for path in profile_path.parent.iterdir() if path.is_file()
    )
    assert len(partial_files) == 1
    partial_bytes = partial_files[0].read_bytes()
    monkeypatch.setattr(profile_module, "_atomic_write_private", original_write)

    recovered = ensure_personal_profile(project_root, profiles_root=profiles)

    assert load_personal_profile(recovered.profile_path) == recovered
    assert read_capability(recovered)
    assert all(not path.exists() for path in partial_files)
    assert partial_bytes not in recovered.private_key_path.read_bytes()
    names = {path.name for path in recovered.profile_path.parent.iterdir()}
    assert {"profile.json", "central-data"} <= names
    assert not any(name.startswith(".") and name.endswith(".tmp") for name in names)
    assert len([name for name in names if name.startswith("credential-1-")]) == 3


def test_concurrent_retry_converges_after_validated_partial_creation(
    tmp_path: Path,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, "concurrent-recovery")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    (profile_path.parent / "central-data").mkdir(mode=0o700)
    write_private(
        profile_path.parent / ("credential-1-" + "a" * 24 + ".key.pem")
    )
    write_private(profile_path.parent / (".profile.json." + "b" * 24 + ".tmp"))

    def initialize(_index: int) -> tuple[Path, str, str, str]:
        value = ensure_personal_profile(project_root, profiles_root=profiles)
        return (
            value.profile_path,
            value.board_id,
            value.principal_id,
            read_capability(value),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(initialize, range(16)))

    assert len(set(results)) == 1
    assert load_personal_profile(profile_path).board_id == results[0][1]


def test_recovery_preserves_owned_integration_lock(tmp_path: Path) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, "integration-lock")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    integration_lock = profile_path.parent / "integration.lock"
    write_private(integration_lock, b"existing-owned-lock")
    partial = profile_path.parent / ("credential-1-" + "e" * 24 + ".jwt")
    write_private(partial, b"partial-token")

    recovered = ensure_personal_profile(project_root, profiles_root=profiles)

    assert load_personal_profile(recovered.profile_path) == recovered
    assert integration_lock.read_bytes() == b"existing-owned-lock"
    assert stat.S_IMODE(integration_lock.stat().st_mode) == 0o600
    assert not partial.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "unsafe-mode"])
def test_unsafe_integration_lock_blocks_recovery_without_deletion(
    tmp_path: Path,
    kind: str,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, f"unsafe-integration-lock-{kind}")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    integration_lock = profile_path.parent / "integration.lock"
    external = parent / f"external-integration-lock-{kind}"
    if kind == "symlink":
        write_private(external, b"external-lock")
        integration_lock.symlink_to(external)
    elif kind == "hardlink":
        write_private(external, b"external-lock")
        os.link(external, integration_lock)
    else:
        integration_lock.write_bytes(b"unsafe-mode-lock")
        integration_lock.chmod(0o644)

    with pytest.raises(ProfileSecurityError):
        ensure_personal_profile(project_root, profiles_root=profiles)

    assert integration_lock.exists() or integration_lock.is_symlink()
    assert not profile_path.exists()
    if external.exists():
        assert external.read_bytes() == b"external-lock"


def test_incomplete_profile_directory_symlink_is_never_recovered(
    tmp_path: Path,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, "symlink-profile")
    profile_path = profile_path_for_project(project_root, profiles)
    external = parent / "external"
    external.mkdir(mode=0o700)
    sentinel = external / "keep.txt"
    write_private(sentinel, b"do-not-touch")
    profile_path.parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(ProfileSecurityError):
        ensure_personal_profile(project_root, profiles_root=profiles)

    assert sentinel.read_bytes() == b"do-not-touch"
    assert not (external / "profile.json").exists()


def test_foreign_file_blocks_recovery_without_deletion(tmp_path: Path) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, "foreign-file")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    apparent_partial = (
        profile_path.parent / ("credential-1-" + "d" * 24 + ".jwt")
    )
    write_private(apparent_partial, b"validated-partial")
    foreign = profile_path.parent / "notes.txt"
    write_private(foreign, b"user-owned")

    with pytest.raises(ProfileSecurityError, match="unrecognized entries"):
        ensure_personal_profile(project_root, profiles_root=profiles)

    assert foreign.read_bytes() == b"user-owned"
    assert apparent_partial.read_bytes() == b"validated-partial"
    assert not profile_path.exists()


def test_nonempty_central_data_blocks_recovery_without_deletion(
    tmp_path: Path,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, "foreign-central-data")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    data_dir = profile_path.parent / "central-data"
    data_dir.mkdir(mode=0o700)
    database = data_dir / "board.sqlite3"
    write_private(database, b"preserve-database")

    with pytest.raises(ProfileSecurityError, match="contains Central data"):
        ensure_personal_profile(project_root, profiles_root=profiles)

    assert database.read_bytes() == b"preserve-database"
    assert not profile_path.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_recoverable_looking_foreign_links_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    profiles.mkdir(mode=0o700)
    project_root = project(parent, f"foreign-{kind}")
    profile_path = profile_path_for_project(project_root, profiles)
    profile_path.parent.mkdir(mode=0o700)
    external = parent / f"external-{kind}.pem"
    write_private(external, b"external-secret")
    apparent_partial = (
        profile_path.parent / ("credential-1-" + "c" * 24 + ".key.pem")
    )
    if kind == "symlink":
        apparent_partial.symlink_to(external)
    else:
        os.link(external, apparent_partial)

    with pytest.raises(ProfileSecurityError):
        ensure_personal_profile(project_root, profiles_root=profiles)

    assert external.read_bytes() == b"external-secret"
    assert apparent_partial.exists() or apparent_partial.is_symlink()
    assert not profile_path.exists()


def test_rotation_cleanup_failure_returns_committed_profile_and_retries_later(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    project_root = project(parent, "rotation-cleanup")
    original = ensure_personal_profile(project_root, profiles_root=profiles)
    old_token = read_capability(original)
    old_paths = (
        original.private_key_path,
        original.jwks_path,
        original.token_path,
    )
    old_contents = {path.name: path.read_bytes() for path in old_paths}
    original_unlink = profile_module._unlink_private

    def fail_retired_cleanup(directory: Path, name: str) -> None:
        if name in old_contents:
            raise OSError("synthetic retired-credential cleanup failure")
        original_unlink(directory, name)

    monkeypatch.setattr(profile_module, "_unlink_private", fail_retired_cleanup)
    rotated = profile_module.rotate_personal_capability(original.profile_path)
    new_token = read_capability(rotated)

    assert rotated.principal_id == original.principal_id
    assert rotated.kid != original.kid
    assert new_token != old_token
    assert load_personal_profile(rotated.profile_path) == rotated
    assert all(path.exists() for path in old_paths)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in old_paths)
    assert all(path.read_bytes() == old_contents[path.name] for path in old_paths)
    assert all(
        active.exists()
        for active in (
            rotated.private_key_path,
            rotated.jwks_path,
            rotated.token_path,
        )
    )
    document = json.loads(rotated.profile_path.read_text(encoding="utf-8"))
    pending = document["pending_cleanup"]
    assert {item["name"] for item in pending} == set(old_contents)
    assert {
        item["name"]: item["sha256"] for item in pending
    } == {
        name: hashlib.sha256(content).hexdigest()
        for name, content in old_contents.items()
    }
    rendered = rotated.profile_path.read_bytes()
    assert old_token.encode() not in rendered
    assert new_token.encode() not in rendered
    assert b"BEGIN PRIVATE KEY" not in rendered

    monkeypatch.setattr(profile_module, "_unlink_private", original_unlink)
    recovered = ensure_personal_profile(project_root, profiles_root=profiles)

    assert recovered.profile_path == rotated.profile_path
    assert recovered.kid == rotated.kid
    assert read_capability(recovered) == new_token
    assert all(not path.exists() for path in old_paths)
    cleaned_document = json.loads(
        recovered.profile_path.read_text(encoding="utf-8")
    )
    assert cleaned_document["pending_cleanup"] == []
    assert all(
        active.exists()
        for active in (
            recovered.private_key_path,
            recovered.jwks_path,
            recovered.token_path,
        )
    )


@pytest.mark.parametrize("failure_point", ["directory-fsync", "profile-stat"])
def test_rotation_returns_committed_profile_after_post_replace_io_error(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    project_root = project(parent, "rotation-post-commit-fsync")
    original = ensure_personal_profile(project_root, profiles_root=profiles)
    old_token = read_capability(original)
    old_paths = (
        original.private_key_path,
        original.jwks_path,
        original.token_path,
    )
    injected = False

    if failure_point == "directory-fsync":
        real_fsync = profile_module.os.fsync

        def fail_once_after_profile_replace(descriptor: int) -> None:
            nonlocal injected
            if not injected and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                document = json.loads(
                    original.profile_path.read_text(encoding="utf-8")
                )
                if document.get("credential_generation") == 2:
                    injected = True
                    raise OSError("synthetic post-replace directory fsync failure")
            real_fsync(descriptor)

        monkeypatch.setattr(
            profile_module.os,
            "fsync",
            fail_once_after_profile_replace,
        )
    else:
        real_stat = profile_module.os.stat

        def fail_once_after_profile_replace(path, *args, **kwargs):
            nonlocal injected
            if (
                not injected
                and path == "profile.json"
                and kwargs.get("dir_fd") is not None
                and kwargs.get("follow_symlinks") is False
            ):
                document = json.loads(
                    original.profile_path.read_text(encoding="utf-8")
                )
                if document.get("credential_generation") == 2:
                    injected = True
                    raise OSError("synthetic post-replace profile stat failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(
            profile_module.os,
            "stat",
            fail_once_after_profile_replace,
        )
    rotated = profile_module.rotate_personal_capability(original.profile_path)

    assert injected
    assert rotated.principal_id == original.principal_id
    assert rotated.kid != original.kid
    assert read_capability(rotated) != old_token
    assert load_personal_profile(rotated.profile_path) == rotated
    with pytest.raises(ProfileSecurityError):
        profile_module._token_claims(rotated, old_token)
    assert all(not path.exists() for path in old_paths)
    document = json.loads(rotated.profile_path.read_text(encoding="utf-8"))
    assert document["credential_generation"] == 2
    assert document["pending_cleanup"] == []
    assert old_token.encode() not in rotated.profile_path.read_bytes()
    assert b"BEGIN PRIVATE KEY" not in rotated.profile_path.read_bytes()


def test_deferred_cleanup_never_deletes_hash_mismatched_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent = private_parent(tmp_path)
    profiles = parent / "profiles"
    project_root = project(parent, "rotation-foreign-replacement")
    original = ensure_personal_profile(project_root, profiles_root=profiles)
    old_paths = (
        original.private_key_path,
        original.jwks_path,
        original.token_path,
    )
    original_unlink = profile_module._unlink_private

    def fail_retired_cleanup(_directory: Path, _name: str) -> None:
        raise OSError("synthetic retired-credential cleanup failure")

    monkeypatch.setattr(profile_module, "_unlink_private", fail_retired_cleanup)
    rotated = profile_module.rotate_personal_capability(original.profile_path)
    monkeypatch.setattr(profile_module, "_unlink_private", original_unlink)
    replacement = old_paths[-1]
    write_private(replacement, b"foreign-replacement-must-survive")

    active = ensure_personal_profile(project_root, profiles_root=profiles)

    assert active.kid == rotated.kid
    assert replacement.read_bytes() == b"foreign-replacement-must-survive"
    assert all(not path.exists() for path in old_paths[:-1])
    document = json.loads(active.profile_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in document["pending_cleanup"]] == [
        replacement.name
    ]
