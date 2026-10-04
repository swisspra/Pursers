"""Explicit, bounded installation for immutable ACP runner selections."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from runner_catalog import CatalogError, canonical_json

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024


class InstallError(RuntimeError):
    """A selected runner could not be materialized without weakening policy."""


def installation_preview(
    resolved: Mapping[str, Any], cache_root: Path
) -> dict[str, Any]:
    value = _validated_resolution(resolved)
    distribution = value["distribution"]
    kind = distribution["kind"]
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    install_root = _destination(cache_root, value, digest)
    if kind != "binary":
        executable = value["launch"]["argv"][0]
        return {
            "schema": "pursers_acp_install_preview_v1",
            "action": "use_pinned_package_launcher",
            "distribution_kind": kind,
            "install_root": None,
            "command": list(value["launch"]["argv"]),
            "launcher": executable,
            "launcher_available": shutil.which(executable) is not None,
            "network_at_launch": True,
            "ready": shutil.which(executable) is not None,
            "limitation": "package download bounds are delegated to the installed launcher",
        }
    integrity = distribution["integrity"]
    command = _safe_member(value["launch"]["argv"][0])
    ready = _installed(install_root, digest, command)
    invalid_cache = not ready and _path_present(install_root)
    return {
        "schema": "pursers_acp_install_preview_v1",
        "action": (
            "reuse_verified_cache"
            if ready
            else "repair_invalid_cache"
            if invalid_cache
            else "download_verify_extract"
        ),
        "distribution_kind": kind,
        "source": dict(distribution["source"]),
        "integrity": integrity,
        "install_root": str(install_root),
        "max_download_bytes": MAX_ARCHIVE_BYTES,
        "max_extracted_bytes": MAX_EXTRACTED_BYTES,
        "ready": ready,
        "blocked_reason": (
            "cached_install_invalid"
            if invalid_cache
            else None
            if integrity is not None
            else "binary_integrity_required"
        ),
    }


def install_binary(
    resolved: Mapping[str, Any],
    cache_root: Path,
    *,
    fetch: Callable[[str, int], bytes] | None = None,
) -> dict[str, Any]:
    """Install one reviewed binary archive into an immutable content cache."""
    value = _validated_resolution(resolved)
    distribution = value["distribution"]
    if distribution["kind"] != "binary":
        raise InstallError("package_runner_uses_external_launcher")
    integrity = distribution["integrity"]
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise InstallError("binary_integrity_required")
    selection_digest = hashlib.sha256(canonical_json(value)).hexdigest()
    destination = _destination(cache_root, value, selection_digest)
    command = _safe_member(value["launch"]["argv"][0])
    if _installed(destination, selection_digest, command):
        return _receipt(value, destination, selection_digest, cached=True)
    if _path_present(destination):
        raise InstallError("cached_install_invalid")
    payload = (fetch or _fetch)(distribution["source"]["archive"], MAX_ARCHIVE_BYTES)
    if not isinstance(payload, bytes) or len(payload) > MAX_ARCHIVE_BYTES:
        raise InstallError("archive_oversized")
    if hashlib.sha256(payload).hexdigest() != integrity["digest"]:
        raise InstallError("archive_integrity_mismatch")
    cache_root = cache_root.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".acp-install-", dir=cache_root))
    try:
        _extract_archive(payload, stage)
        executable = (stage / command).resolve()
        if not executable.is_relative_to(stage.resolve()):
            raise InstallError("runner_command_escape")
        try:
            info = executable.lstat()
        except OSError as exc:
            raise InstallError("runner_command_missing") from exc
        if not stat.S_ISREG(info.st_mode) or executable.is_symlink():
            raise InstallError("runner_command_invalid")
        executable.chmod(info.st_mode | stat.S_IXUSR)
        tree_digest = _tree_digest(stage)
        manifest = {
            "schema": "pursers_acp_install_receipt_v1",
            "selection_sha256": selection_digest,
            "archive_sha256": integrity["digest"],
            "install_tree_sha256": tree_digest,
            "resolved": value,
        }
        _write_private(stage / "manifest.json", canonical_json(manifest) + b"\n")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.rename(stage, destination)
        except FileExistsError:
            if not _installed(destination, selection_digest, command):
                raise InstallError("install_destination_conflict")
        if not _installed(destination, selection_digest, command):
            raise InstallError("install_verification_failed")
        return _receipt(value, destination, selection_digest, cached=False)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def resolved_command(
    resolved: Mapping[str, Any], install_root: Path | None
) -> list[str]:
    value = _validated_resolution(resolved)
    argv = list(value["launch"]["argv"])
    if value["distribution"]["kind"] != "binary":
        return argv
    if install_root is None:
        raise InstallError("binary_install_root_required")
    executable = (install_root / _safe_member(argv[0])).resolve()
    if not executable.is_relative_to(install_root.resolve()):
        raise InstallError("runner_command_escape")
    return [str(executable), *argv[1:]]


def _validated_resolution(resolved: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from runner_catalog import _validate_resolution

        return _validate_resolution(dict(resolved))
    except (CatalogError, TypeError, ValueError) as exc:
        raise InstallError("resolution_invalid") from exc


def _destination(root: Path, value: Mapping[str, Any], digest: str) -> Path:
    parts = (value["agent_id"], value["agent_version"], value["platform"], digest)
    if any(
        not isinstance(part, str) or not part or "/" in part or "\\" in part
        for part in parts
    ):
        raise InstallError("cache_key_invalid")
    return root.expanduser().resolve().joinpath(*parts)


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except OSError:
        return False


def _installed(path: Path, digest: str, command: Path) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            return False
        manifest_path = path / "manifest.json"
        manifest_info = manifest_path.lstat()
        if not stat.S_ISREG(manifest_info.st_mode) or manifest_path.is_symlink():
            return False
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        executable = path / command
        executable_info = executable.lstat()
        if (
            not stat.S_ISREG(executable_info.st_mode)
            or executable.is_symlink()
            or not executable_info.st_mode & 0o111
        ):
            return False
        expected_tree = document.get("install_tree_sha256")
        return (
            document.get("selection_sha256") == digest
            and isinstance(expected_tree, str)
            and len(expected_tree) == 64
            and _tree_digest(path, exclude={"manifest.json"}) == expected_tree
        )
    except (InstallError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def _tree_digest(root: Path, *, exclude: set[str] | None = None) -> str:
    """Hash the complete extracted tree without following links."""
    entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        nonlocal file_count, total_bytes
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise InstallError("install_tree_unreadable") from exc
        for child in children:
            relative = prefix / child.name
            relative_text = relative.as_posix()
            if exclude and relative_text in exclude:
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise InstallError("install_tree_unreadable") from exc
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative_text, "type": "directory", "mode": mode})
                visit(Path(child.path), relative)
                continue
            if not stat.S_ISREG(info.st_mode) or child.is_symlink():
                raise InstallError("install_tree_invalid")
            file_count += 1
            if file_count > MAX_ARCHIVE_FILES:
                raise InstallError("install_tree_file_count_exceeded")
            digest = hashlib.sha256()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(child.path, flags)
                with os.fdopen(descriptor, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    ):
                        raise InstallError("install_tree_invalid")
                    total_bytes += opened.st_size
                    if total_bytes > MAX_EXTRACTED_BYTES:
                        raise InstallError("install_tree_oversized")
                    bytes_read = 0
                    while chunk := source.read(1024 * 1024):
                        bytes_read += len(chunk)
                        if bytes_read > opened.st_size:
                            raise InstallError("install_tree_changed")
                        digest.update(chunk)
                    if bytes_read != opened.st_size:
                        raise InstallError("install_tree_changed")
            except OSError as exc:
                raise InstallError("install_tree_unreadable") from exc
            entries.append(
                {
                    "path": relative_text,
                    "type": "file",
                    "mode": stat.S_IMODE(opened.st_mode),
                    "size": opened.st_size,
                    "sha256": digest.hexdigest(),
                }
            )

    visit(root, PurePosixPath())
    document = {"schema": "pursers_acp_install_tree_v1", "entries": entries}
    return hashlib.sha256(canonical_json(document)).hexdigest()


def _receipt(
    value: Mapping[str, Any], root: Path, digest: str, *, cached: bool
) -> dict[str, Any]:
    return {
        "schema": "pursers_acp_install_result_v1",
        "agent_id": value["agent_id"],
        "agent_version": value["agent_version"],
        "selection_sha256": digest,
        "install_root": str(root),
        "command": resolved_command(value, root),
        "cached": cached,
    }


def _fetch(url: str, maximum: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Pursers-ACP-Installer/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(maximum + 1)
    except (OSError, TimeoutError) as exc:
        raise InstallError("archive_download_failed") from exc
    if len(payload) > maximum:
        raise InstallError("archive_oversized")
    return payload


def _safe_member(name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise InstallError("archive_member_unsafe")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InstallError("archive_member_unsafe")
    return Path(*path.parts)


def _extract_archive(payload: bytes, destination: Path) -> None:
    archive = destination / ".archive"
    archive.write_bytes(payload)
    try:
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, destination)
        elif tarfile.is_tarfile(archive):
            _extract_tar(archive, destination)
        else:
            raise InstallError("archive_format_unsupported")
    finally:
        archive.unlink(missing_ok=True)


def _extract_zip(archive: Path, destination: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise InstallError("archive_file_count_exceeded")
        for member in members:
            relative = _safe_member(member.filename)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise InstallError("archive_link_forbidden")
            total += member.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise InstallError("archive_expanded_oversized")
            target = destination / relative
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.open(member) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _extract_tar(archive: Path, destination: Path) -> None:
    total = 0
    with tarfile.open(archive, mode="r:*") as source:
        members = source.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise InstallError("archive_file_count_exceeded")
        for member in members:
            relative = _safe_member(member.name)
            if (
                member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise InstallError("archive_link_or_special_forbidden")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise InstallError("archive_expanded_oversized")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            reader = source.extractfile(member)
            if reader is None:
                raise InstallError("archive_member_unreadable")
            with reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
