#!/usr/bin/env python3
"""Local Personal Preview profile and generated capability primitives.

This module is deliberately local-only.  It never calls Central, edits a host
configuration, or treats an agent display name as an authorization identity.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


PROFILE_SCHEMA_VERSION = 1
PROFILE_MODE = "personal"
PERSONAL_REVIEW_POLICY = "workflow"
PROFILE_ENV = "ONBOARD_PERSONAL_PROFILE"
DEFAULT_PORT = 8766
DEFAULT_CAPABILITY_LIFETIME_S = 365 * 24 * 60 * 60
MIN_CAPABILITY_LIFETIME_S = 60 * 60
MAX_CAPABILITY_LIFETIME_S = 2 * 365 * 24 * 60 * 60
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SAFE_LEAF_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
LEGACY_OVERRIDE_ENV = (
    "ONBOARD_CENTRAL_TOKEN",
    "ONBOARD_CENTRAL_URL",
    "ONBOARD_BOARD_ID",
    "ONBOARD_AGENT_NAME",
)
SCOPES = ("board:read", "board:write", "board:review")
_PROCESS_LOCK = threading.RLock()
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CREDENTIAL_FILE_RE = re.compile(
    r"^credential-(?:[1-9][0-9]*)-[0-9a-f]{24}\."
    r"(?:key\.pem|jwks\.json|jwt)$"
)
_INITIAL_CREDENTIAL_FILE_RE = re.compile(
    r"^(?P<prefix>credential-1-[0-9a-f]{24})\.(?:key\.pem|jwks\.json|jwt)$"
)
_INITIAL_CREATION_TEMP_RE = re.compile(
    r"^\.(?P<leaf>(?:credential-1-[0-9a-f]{24}\."
    r"(?:key\.pem|jwks\.json|jwt)|profile\.json))\.[0-9a-f]{24}\.tmp$"
)


class PersonalProfileError(RuntimeError):
    """Base error for invalid or unavailable personal profiles."""


class ProfileSecurityError(PersonalProfileError):
    """Raised when a local path or credential fails a security invariant."""


class _ProfileReplaceCommittedError(ProfileSecurityError):
    """Raised when a profile pointer was replaced but post-commit I/O failed."""


@dataclass(frozen=True)
class PersonalProfile:
    profile_path: Path
    project_root: Path
    profile_id: str
    board_id: str
    review_policy: str
    central_port: int
    central_url: str
    central_data_dir: Path
    issuer: str
    audience: str
    subject: str
    client_id: str
    principal_id: str
    kid: str
    scopes: tuple[str, ...]
    private_key_path: Path
    jwks_path: Path
    token_path: Path


@dataclass(frozen=True)
class PersonalContext:
    profile_path: Path
    project_root: Path
    central_url: str
    central_data_dir: Path
    board_id: str
    authenticated_principal_id: str
    agent_name: str
    agent_platform: str
    capability_token: str = field(repr=False)

    def safe_summary(self) -> dict[str, str]:
        return {
            "profile_path": str(self.profile_path),
            "project_root": str(self.project_root),
            "central_url": self.central_url,
            "central_data_dir": str(self.central_data_dir),
            "board_id": self.board_id,
            "authenticated_principal_id": self.authenticated_principal_id,
            "agent_name": self.agent_name,
            "agent_platform": self.agent_platform,
        }


class ReviewPolicyClient(Protocol):
    async def board_review_policy_set(self, review_policy: str) -> dict[str, Any]: ...


def default_profiles_root() -> Path:
    """Return the macOS-first local profile root without creating it."""
    return Path.home() / "Library" / "Application Support" / "On Board Personal"


def _require_nofollow() -> None:
    if not _O_NOFOLLOW:
        raise ProfileSecurityError("this platform does not provide O_NOFOLLOW")


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _validate_owned(value: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if getuid is not None and value.st_uid != getuid():
        raise ProfileSecurityError(f"{label} is not owned by the current user")


def _validate_directory_stat(value: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise ProfileSecurityError(f"{label} must be a directory")
    _validate_owned(value, label)
    if _mode(value) != 0o700:
        raise ProfileSecurityError(f"{label} must have mode 0700")


def _validate_file_stat(value: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise ProfileSecurityError(f"{label} must be a regular file")
    _validate_owned(value, label)
    if _mode(value) != 0o600:
        raise ProfileSecurityError(f"{label} must have mode 0600")
    if value.st_nlink != 1:
        raise ProfileSecurityError(f"{label} must have exactly one hard link")


def _open_directory(path: Path) -> int:
    _require_nofollow()
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        raise ProfileSecurityError(f"cannot securely open directory: {path}") from exc
    try:
        _validate_directory_stat(os.fstat(descriptor), str(path))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_private_root(path: Path) -> Path:
    """Create one private leaf beneath an existing non-symlink parent."""
    _require_nofollow()
    root = path.expanduser().absolute()
    if not root.parent.is_dir():
        raise ProfileSecurityError(f"profile root parent does not exist: {root.parent}")
    parent_fd = None
    try:
        parent_fd = os.open(root.parent, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        value = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_directory_stat(value, str(root))
    except OSError as exc:
        raise ProfileSecurityError(f"cannot securely prepare profile root: {root}") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return root


def _safe_leaf(name: str) -> str:
    if not isinstance(name, str) or not SAFE_LEAF_RE.fullmatch(name):
        raise ProfileSecurityError("profile file name is not a safe leaf")
    return name


def _read_private_file(directory: Path, name: str) -> bytes:
    leaf = _safe_leaf(name)
    directory_fd = _open_directory(directory)
    descriptor = None
    try:
        descriptor = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW, dir_fd=directory_fd)
        _validate_file_stat(os.fstat(descriptor), str(directory / leaf))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ProfileSecurityError(f"cannot securely read profile file: {directory / leaf}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _private_file_digest_if_present(directory: Path, name: str) -> str | None:
    leaf = _safe_leaf(name)
    directory_fd = _open_directory(directory)
    descriptor = None
    try:
        try:
            descriptor = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        _validate_file_stat(os.fstat(descriptor), str(directory / leaf))
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ProfileSecurityError(
            f"cannot securely inspect retired credential: {directory / leaf}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _atomic_write_private(
    directory: Path,
    name: str,
    content: bytes,
    *,
    replace: bool = False,
) -> None:
    leaf = _safe_leaf(name)
    directory_fd = _open_directory(directory)
    temporary = f".{leaf}.{secrets.token_hex(12)}.tmp"
    descriptor = None
    replaced = False
    try:
        try:
            existing = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _validate_file_stat(existing, str(directory / leaf))
            if not replace:
                raise ProfileSecurityError(f"refusing to overwrite profile file: {directory / leaf}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            leaf,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        os.fsync(directory_fd)
        written = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        _validate_file_stat(written, str(directory / leaf))
    except OSError as exc:
        if replace and replaced:
            raise _ProfileReplaceCommittedError(
                f"profile pointer was replaced but post-commit verification failed: "
                f"{directory / leaf}"
            ) from exc
        raise ProfileSecurityError(f"cannot atomically write profile file: {directory / leaf}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _unlink_private(directory: Path, name: str) -> None:
    leaf = _safe_leaf(name)
    directory_fd = _open_directory(directory)
    try:
        value = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        _validate_file_stat(value, str(directory / leaf))
        os.unlink(leaf, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise ProfileSecurityError(f"cannot securely remove old credential: {directory / leaf}") from exc
    finally:
        os.close(directory_fd)


def _has_profile_document(directory: Path) -> bool:
    directory_fd = _open_directory(directory)
    try:
        try:
            os.stat("profile.json", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    except OSError as exc:
        raise ProfileSecurityError(
            f"cannot securely inspect profile directory: {directory}"
        ) from exc
    finally:
        os.close(directory_fd)


def _recover_incomplete_profile_directory(directory: Path) -> None:
    """Remove only validated first-creation debris; preserve anything unknown."""
    directory_fd = _open_directory(directory)
    cleanup: list[str] = []
    credential_prefixes: set[str] = set()
    try:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ProfileSecurityError(
                f"cannot securely list incomplete profile directory: {directory}"
            ) from exc
        for name in names:
            try:
                value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ProfileSecurityError(
                    f"cannot securely inspect incomplete profile entry: {directory / name}"
                ) from exc
            if name == "central-data":
                _validate_directory_stat(value, str(directory / name))
                child_fd = None
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    _validate_directory_stat(os.fstat(child_fd), str(directory / name))
                    if os.listdir(child_fd):
                        raise ProfileSecurityError(
                            "incomplete profile contains Central data; refusing recovery"
                        )
                except OSError as exc:
                    raise ProfileSecurityError(
                        f"cannot securely inspect Central data directory: {directory / name}"
                    ) from exc
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
                continue

            if name == "integration.lock":
                _validate_file_stat(value, str(directory / name))
                continue

            credential = _INITIAL_CREDENTIAL_FILE_RE.fullmatch(name)
            temporary = _INITIAL_CREATION_TEMP_RE.fullmatch(name)
            if credential is None and temporary is None:
                raise ProfileSecurityError(
                    "incomplete profile contains unrecognized entries; refusing recovery"
                )
            _validate_file_stat(value, str(directory / name))
            cleanup.append(name)
            leaf = name if credential is not None else temporary.group("leaf")
            leaf_credential = _INITIAL_CREDENTIAL_FILE_RE.fullmatch(leaf)
            if leaf_credential is not None:
                credential_prefixes.add(leaf_credential.group("prefix"))
        if len(credential_prefixes) > 1:
            raise ProfileSecurityError(
                "incomplete profile contains multiple credential sets; refusing recovery"
            )
    finally:
        os.close(directory_fd)

    for name in cleanup:
        _unlink_private(directory, name)

    directory_fd = _open_directory(directory)
    try:
        remaining = set(os.listdir(directory_fd))
    except OSError as exc:
        raise ProfileSecurityError(
            f"cannot verify recovered profile directory: {directory}"
        ) from exc
    finally:
        os.close(directory_fd)
    if not remaining.issubset({"central-data", "integration.lock"}):
        raise ProfileSecurityError(
            "incomplete profile changed during recovery; refusing initialization"
        )


def _ensure_empty_central_data_directory(directory: Path) -> None:
    directory_fd = _open_directory(directory)
    child_fd = None
    try:
        try:
            os.mkdir("central-data", 0o700, dir_fd=directory_fd)
        except FileExistsError:
            pass
        value = os.stat("central-data", dir_fd=directory_fd, follow_symlinks=False)
        _validate_directory_stat(value, str(directory / "central-data"))
        child_fd = os.open(
            "central-data",
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        _validate_directory_stat(os.fstat(child_fd), str(directory / "central-data"))
        if os.listdir(child_fd):
            raise ProfileSecurityError(
                "new Personal profile Central data directory must be empty"
            )
    except ProfileSecurityError:
        raise
    except OSError as exc:
        raise ProfileSecurityError(
            "cannot securely prepare private Central data directory"
        ) from exc
    finally:
        if child_fd is not None:
            os.close(child_fd)
        os.close(directory_fd)


class _ProfileLock:
    def __init__(self, profiles_root: Path):
        self.profiles_root = profiles_root
        self._directory_fd: int | None = None
        self._lock_fd: int | None = None

    def __enter__(self) -> None:
        _PROCESS_LOCK.acquire()
        try:
            self._directory_fd = _open_directory(self.profiles_root)
            self._lock_fd = os.open(
                ".profiles.lock",
                os.O_RDWR | os.O_CREAT | _O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            _validate_file_stat(os.fstat(self._lock_fd), "profile lock")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None
        _PROCESS_LOCK.release()


def _canonical_project_root(project_root: Path) -> Path:
    candidate = project_root.expanduser().absolute()
    if candidate.is_symlink():
        raise ProfileSecurityError("project root must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PersonalProfileError("project root does not exist") from exc
    if not resolved.is_dir():
        raise PersonalProfileError("project root must be a directory")
    return resolved


def _project_key(project_root: Path) -> str:
    canonical = _canonical_project_root(project_root)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:24]
    return f"project-{digest}"


def profile_path_for_project(project_root: Path, profiles_root: Path) -> Path:
    return profiles_root.expanduser().absolute() / _project_key(project_root) / "profile.json"


def _slug(value: str, *, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"
    return slug[:limit].rstrip("-") or "project"


def _principal_id(client_id: str, issuer: str, subject: str) -> str:
    canonical = json.dumps([client_id, issuer, subject], separators=(",", ":"))
    return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def _credential_files(generation: int) -> dict[str, str]:
    if generation < 1:
        raise ValueError("credential generation must be positive")
    credential_id = secrets.token_hex(12)
    prefix = f"credential-{generation}-{credential_id}"
    return {
        "private_key": f"{prefix}.key.pem",
        "jwks": f"{prefix}.jwks.json",
        "token": f"{prefix}.jwt",
    }


def _issue_token(
    private_key: rsa.RSAPrivateKey,
    profile_document: Mapping[str, Any],
    *,
    now: int | None = None,
    lifetime_s: int = DEFAULT_CAPABILITY_LIFETIME_S,
) -> str:
    if not MIN_CAPABILITY_LIFETIME_S <= lifetime_s <= MAX_CAPABILITY_LIFETIME_S:
        raise ValueError(
            f"capability lifetime must be between {MIN_CAPABILITY_LIFETIME_S} "
            f"and {MAX_CAPABILITY_LIFETIME_S} seconds"
        )
    issued = int(time.time()) if now is None else int(now)
    claims = {
        "iss": profile_document["issuer"],
        "aud": profile_document["audience"],
        "resource": profile_document["audience"],
        "sub": profile_document["subject"],
        "client_id": profile_document["client_id"],
        "scope": " ".join(profile_document["scopes"]),
        "iat": issued,
        "nbf": issued - 5,
        "exp": issued + lifetime_s,
        "jti": secrets.token_urlsafe(24),
    }
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": profile_document["kid"]},
    )


def _profile_document(project_root: Path, port: int) -> tuple[dict[str, Any], rsa.RSAPrivateKey]:
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    profile_id = secrets.token_hex(16)
    project_slug = _slug(project_root.name, limit=40)
    board_id = f"{project_slug}-{secrets.token_hex(12)}"
    central_url = f"http://127.0.0.1:{port}/mcp"
    issuer = f"http://127.0.0.1:{port}/personal-issuer/{profile_id}"
    subject = f"owner-{secrets.token_urlsafe(24)}"
    client_id = "pursers-personal"
    kid = f"personal-{secrets.token_hex(16)}"
    credential_generation = 1
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    document = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "mode": PROFILE_MODE,
        "profile_id": profile_id,
        "project_root": str(project_root),
        "board_id": board_id,
        "review_policy": PERSONAL_REVIEW_POLICY,
        "central": {
            "host": "127.0.0.1",
            "port": port,
            "url": central_url,
            "store_backend": "sqlite",
            "auth_mode": "jwt",
            "admission": "invite",
            "data_dir": "central-data",
        },
        "issuer": issuer,
        "audience": central_url,
        "subject": subject,
        "client_id": client_id,
        "principal_id": _principal_id(client_id, issuer, subject),
        "scopes": list(SCOPES),
        "kid": kid,
        "credential_generation": credential_generation,
        "files": _credential_files(credential_generation),
        "pending_cleanup": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return document, private_key


def ensure_personal_profile(
    project_root: Path,
    *,
    profiles_root: Path | None = None,
    port: int = DEFAULT_PORT,
    capability_lifetime_s: int = DEFAULT_CAPABILITY_LIFETIME_S,
) -> PersonalProfile:
    """Create once, then return the stable profile for one canonical project."""
    project = _canonical_project_root(project_root)
    root = _ensure_private_root(profiles_root or default_profiles_root())
    profile_path = profile_path_for_project(project, root)
    with _ProfileLock(root):
        if profile_path.parent.exists() or profile_path.parent.is_symlink():
            if _has_profile_document(profile_path.parent):
                profile = load_personal_profile(profile_path)
                if profile.project_root != project:
                    raise ProfileSecurityError("profile key collision for a different project")
                if profile.central_port != port:
                    raise PersonalProfileError(
                        f"existing profile uses port {profile.central_port}, not requested port {port}"
                    )
                _retry_pending_cleanup(profile.profile_path)
                return profile
            _recover_incomplete_profile_directory(profile_path.parent)
        else:
            root_fd = _open_directory(root)
            try:
                os.mkdir(profile_path.parent.name, 0o700, dir_fd=root_fd)
            except OSError as exc:
                raise ProfileSecurityError(
                    "cannot create private project profile directory"
                ) from exc
            finally:
                os.close(root_fd)

        document, private_key = _profile_document(project, port)
        _ensure_empty_central_data_directory(profile_path.parent)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwks = {"keys": [_public_jwk(private_key, document["kid"])]}
        token = _issue_token(
            private_key,
            document,
            lifetime_s=capability_lifetime_s,
        )
        files = document["files"]
        _atomic_write_private(profile_path.parent, files["private_key"], private_pem)
        _atomic_write_private(
            profile_path.parent,
            files["jwks"],
            (json.dumps(jwks, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        _atomic_write_private(
            profile_path.parent, files["token"], (token + "\n").encode()
        )
        _atomic_write_private(
            profile_path.parent,
            "profile.json",
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
        )
        return load_personal_profile(profile_path)


def _parse_profile_document(profile_path: Path) -> dict[str, Any]:
    try:
        raw = _read_private_file(profile_path.parent, profile_path.name)
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonalProfileError("profile document is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise PersonalProfileError("profile document must be an object")
    return document


def _required_text(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise PersonalProfileError(f"profile {key} must be a non-empty string")
    return value


def _pending_cleanup_records(
    document: Mapping[str, Any],
    active_names: set[str],
) -> tuple[tuple[str, str], ...]:
    raw = document.get("pending_cleanup", [])
    if not isinstance(raw, list):
        raise PersonalProfileError("profile pending_cleanup must be a list")
    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "sha256"}:
            raise PersonalProfileError("profile pending_cleanup record is invalid")
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or not _CREDENTIAL_FILE_RE.fullmatch(name):
            raise ProfileSecurityError("retired credential name is invalid")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ProfileSecurityError("retired credential digest is invalid")
        if name in active_names or name in seen:
            raise ProfileSecurityError("retired credential set overlaps or repeats")
        seen.add(name)
        records.append((name, digest))
    return tuple(records)


def _retry_pending_cleanup(profile_path: Path) -> None:
    """Best-effort cleanup after a committed profile pointer switch.

    This helper is called only while the profile-root lock is held. Failures
    retain a hash-bound cleanup record and never make the committed rotation
    appear to have failed.
    """
    try:
        document = _parse_profile_document(profile_path)
        files = document.get("files")
        if not isinstance(files, dict):
            return
        active_names = {
            _safe_leaf(_required_text(files, key))
            for key in ("private_key", "jwks", "token")
        }
        records = _pending_cleanup_records(document, active_names)
    except Exception:
        return
    if not records:
        return

    remaining: list[tuple[str, str]] = []
    for name, expected_digest in records:
        try:
            actual_digest = _private_file_digest_if_present(profile_path.parent, name)
            if actual_digest is None:
                continue
            if actual_digest != expected_digest:
                remaining.append((name, expected_digest))
                continue
            _unlink_private(profile_path.parent, name)
        except Exception:
            remaining.append((name, expected_digest))

    if remaining == list(records):
        return
    document["pending_cleanup"] = [
        {"name": name, "sha256": digest} for name, digest in remaining
    ]
    try:
        _atomic_write_private(
            profile_path.parent,
            profile_path.name,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
            replace=True,
        )
    except Exception:
        pass


def _token_claims(
    profile: PersonalProfile,
    token: str,
    *,
    verify_expiration: bool = True,
) -> dict[str, Any]:
    try:
        jwks = json.loads(_read_private_file(profile.jwks_path.parent, profile.jwks_path.name))
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
        matches = [
            item
            for item in keys or []
            if isinstance(item, dict) and item.get("kid") == profile.kid
        ]
        if len(matches) != 1:
            raise PersonalProfileError("profile JWKS does not contain exactly one matching key")
        key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(matches[0]))
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=profile.issuer,
            audience=profile.audience,
            options={
                "require": ["exp", "nbf", "iss", "sub", "aud", "resource", "scope"],
                "strict_aud": True,
                "verify_exp": verify_expiration,
            },
        )
    except PersonalProfileError:
        raise
    except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ProfileSecurityError("personal capability verification failed") from exc
    expected = {
        "resource": profile.audience,
        "sub": profile.subject,
        "client_id": profile.client_id,
        "scope": " ".join(profile.scopes),
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise ProfileSecurityError("personal capability claims do not match the selected profile")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or header.get("kid") != profile.kid:
        raise ProfileSecurityError("personal capability header does not match the selected profile")
    return dict(claims)


def _load_personal_profile(
    profile_path: Path,
    *,
    allow_expired_capability: bool,
) -> PersonalProfile:
    """Load a private profile; expiry may be relaxed only for key rotation."""
    path = profile_path.expanduser().absolute()
    if path.is_dir():
        path = path / "profile.json"
    if path.name != "profile.json":
        raise PersonalProfileError("personal profile path must name profile.json or its directory")
    document = _parse_profile_document(path)
    if document.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise PersonalProfileError("unsupported personal profile schema")
    if document.get("mode") != PROFILE_MODE:
        raise PersonalProfileError("profile is not personal mode")

    project = _canonical_project_root(Path(_required_text(document, "project_root")))
    board_id = _required_text(document, "board_id")
    if not ID_RE.fullmatch(board_id):
        raise PersonalProfileError("profile board_id is invalid")
    central = document.get("central")
    if not isinstance(central, dict):
        raise PersonalProfileError("profile central settings are missing")
    if central.get("host") != "127.0.0.1":
        raise ProfileSecurityError("personal Central host must be exactly 127.0.0.1")
    port = central.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
        raise PersonalProfileError("profile Central port is invalid")
    central_url = f"http://127.0.0.1:{port}/mcp"
    expected_central = {
        "url": central_url,
        "store_backend": "sqlite",
        "auth_mode": "jwt",
        "admission": "invite",
        "data_dir": "central-data",
    }
    if any(central.get(key) != value for key, value in expected_central.items()):
        raise ProfileSecurityError("personal Central settings are not the strict local profile")

    issuer = _required_text(document, "issuer")
    audience = _required_text(document, "audience")
    if audience != central_url or issuer != (
        f"http://127.0.0.1:{port}/personal-issuer/{_required_text(document, 'profile_id')}"
    ):
        raise ProfileSecurityError("personal issuer/audience do not match the loopback profile")
    subject = _required_text(document, "subject")
    client_id = _required_text(document, "client_id")
    principal_id = _required_text(document, "principal_id")
    if principal_id != _principal_id(client_id, issuer, subject):
        raise ProfileSecurityError("profile principal_id does not match signed identity claims")
    scopes = document.get("scopes")
    if scopes != list(SCOPES):
        raise ProfileSecurityError("personal capability scopes are not the approved set")
    review_policy = _required_text(document, "review_policy")
    if review_policy != PERSONAL_REVIEW_POLICY:
        raise ProfileSecurityError("personal review policy must be workflow")
    kid = _required_text(document, "kid")
    files = document.get("files")
    if not isinstance(files, dict):
        raise PersonalProfileError("profile file map is missing")
    private_key_path = path.parent / _safe_leaf(_required_text(files, "private_key"))
    jwks_path = path.parent / _safe_leaf(_required_text(files, "jwks"))
    token_path = path.parent / _safe_leaf(_required_text(files, "token"))
    _pending_cleanup_records(
        document,
        {private_key_path.name, jwks_path.name, token_path.name},
    )
    central_data_dir = path.parent / _safe_leaf(_required_text(central, "data_dir"))
    central_data_fd = _open_directory(central_data_dir)
    os.close(central_data_fd)
    generation = document.get("credential_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise PersonalProfileError("profile credential_generation is invalid")

    try:
        private_key = serialization.load_pem_private_key(
            _read_private_file(private_key_path.parent, private_key_path.name),
            password=None,
        )
    except (ValueError, TypeError) as exc:
        raise ProfileSecurityError("personal signing key is invalid") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2_048:
        raise ProfileSecurityError("personal signing key must be RSA with at least 2048 bits")
    try:
        jwks = json.loads(_read_private_file(jwks_path.parent, jwks_path.name))
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
    except json.JSONDecodeError as exc:
        raise ProfileSecurityError("personal JWKS is invalid") from exc
    expected_jwk = _public_jwk(private_key, kid)
    if keys != [expected_jwk]:
        raise ProfileSecurityError("personal JWKS does not match the private signing key")

    profile = PersonalProfile(
        profile_path=path,
        project_root=project,
        profile_id=_required_text(document, "profile_id"),
        board_id=board_id,
        review_policy=review_policy,
        central_port=port,
        central_url=central_url,
        central_data_dir=central_data_dir,
        issuer=issuer,
        audience=audience,
        subject=subject,
        client_id=client_id,
        principal_id=principal_id,
        kid=kid,
        scopes=SCOPES,
        private_key_path=private_key_path,
        jwks_path=jwks_path,
        token_path=token_path,
    )
    token = read_capability(profile)
    _token_claims(
        profile,
        token,
        verify_expiration=not allow_expired_capability,
    )
    return profile


def load_personal_profile(profile_path: Path) -> PersonalProfile:
    """Load and fully verify one private profile without following file symlinks."""
    return _load_personal_profile(
        profile_path,
        allow_expired_capability=False,
    )


def read_capability(profile: PersonalProfile) -> str:
    try:
        token = _read_private_file(profile.token_path.parent, profile.token_path.name).decode(
            "ascii"
        ).strip()
    except UnicodeDecodeError as exc:
        raise ProfileSecurityError("personal capability is not ASCII") from exc
    if not token or any(character.isspace() for character in token):
        raise ProfileSecurityError("personal capability file is malformed")
    return token


def rotate_personal_capability(
    profile_path: Path,
    *,
    lifetime_s: int = DEFAULT_CAPABILITY_LIFETIME_S,
) -> PersonalProfile:
    """Rotate key, JWKS, kid, and token while preserving the signed principal."""
    profile = _load_personal_profile(
        profile_path,
        allow_expired_capability=True,
    )
    root = profile.profile_path.parent.parent
    with _ProfileLock(root):
        profile = _load_personal_profile(
            profile.profile_path,
            allow_expired_capability=True,
        )
        document = _parse_profile_document(profile.profile_path)
        old_files = dict(document["files"])
        pending_cleanup = list(
            _pending_cleanup_records(document, set(old_files.values()))
        )
        for old_name in old_files.values():
            pending_cleanup.append(
                (
                    old_name,
                    hashlib.sha256(
                        _read_private_file(profile.profile_path.parent, old_name)
                    ).hexdigest(),
                )
            )
        generation = int(document["credential_generation"]) + 1
        new_files = _credential_files(generation)
        private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
        document["credential_generation"] = generation
        document["kid"] = f"personal-{secrets.token_hex(16)}"
        document["files"] = new_files
        document["pending_cleanup"] = [
            {"name": name, "sha256": digest}
            for name, digest in pending_cleanup
        ]
        token = _issue_token(private_key, document, lifetime_s=lifetime_s)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwks = {"keys": [_public_jwk(private_key, document["kid"])]}
        _atomic_write_private(
            profile.profile_path.parent, new_files["private_key"], private_pem
        )
        _atomic_write_private(
            profile.profile_path.parent,
            new_files["jwks"],
            (json.dumps(jwks, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        _atomic_write_private(
            profile.profile_path.parent,
            new_files["token"],
            (token + "\n").encode("ascii"),
        )
        try:
            _atomic_write_private(
                profile.profile_path.parent,
                profile.profile_path.name,
                (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
                replace=True,
            )
        except _ProfileReplaceCommittedError:
            # The new pointer is authoritative. Reload it below instead of
            # reporting a failed rotation and leaving the caller on old state.
            pass
        rotated = load_personal_profile(profile.profile_path)
        _retry_pending_cleanup(profile.profile_path)
    if rotated.principal_id != profile.principal_id:
        raise ProfileSecurityError("capability rotation changed the authenticated principal")
    return rotated


def _agent_component(value: str, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    clean = value.strip()
    if len(clean) > limit or any(ord(character) < 0x20 for character in clean):
        raise ValueError(f"{field_name} is too long or contains control characters")
    return clean


def resolve_personal_context(
    profile: PersonalProfile,
    *,
    host: str,
    session: str,
) -> PersonalContext:
    """Derive one stable agent label from explicit host and session identities."""
    host_value = _agent_component(host, "host", 128)
    session_value = _agent_component(session, "session", 512)
    platform = _slug(host_value, limit=32)
    identity_material = json.dumps(
        [profile.profile_id, host_value, session_value], separators=(",", ":")
    )
    suffix = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:20]
    agent_name = f"{platform}-{suffix}"
    if not ID_RE.fullmatch(agent_name):
        raise AssertionError("derived agent name violates Central's identifier contract")
    return PersonalContext(
        profile_path=profile.profile_path,
        project_root=profile.project_root,
        central_url=profile.central_url,
        central_data_dir=profile.central_data_dir,
        board_id=profile.board_id,
        authenticated_principal_id=profile.principal_id,
        agent_name=agent_name,
        agent_platform=platform,
        capability_token=read_capability(profile),
    )


def select_personal_profile(
    *,
    explicit_profile: Path | None = None,
    project_root: Path | None = None,
    profiles_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[PersonalProfile, str]:
    """Resolve profile selection; selected profile data always beats legacy env."""
    environment = os.environ if env is None else env
    if explicit_profile is not None:
        return load_personal_profile(explicit_profile), "explicit"
    configured = environment.get(PROFILE_ENV, "").strip()
    if configured:
        return load_personal_profile(Path(configured)), PROFILE_ENV
    if project_root is None:
        raise PersonalProfileError(
            f"select a profile explicitly, set {PROFILE_ENV}, or provide project_root"
        )
    root = (profiles_root or default_profiles_root()).expanduser().absolute()
    return load_personal_profile(profile_path_for_project(project_root, root)), "project-root"


def central_environment(profile: PersonalProfile) -> dict[str, str]:
    """Return strict Central settings; the bearer secret is intentionally absent."""
    return {
        "ONBOARD_CENTRAL_HOST": "127.0.0.1",
        "ONBOARD_CENTRAL_PORT": str(profile.central_port),
        "ONBOARD_CENTRAL_DATA_DIR": str(profile.central_data_dir),
        "ONBOARD_CENTRAL_STORE_BACKEND": "sqlite",
        "ONBOARD_CENTRAL_AUTH_MODE": "jwt",
        "ONBOARD_CENTRAL_ADMISSION": "invite",
        "CENTRAL_JWT_ISSUER": profile.issuer,
        "CENTRAL_JWT_AUDIENCE": profile.audience,
        "CENTRAL_JWKS_PATH": str(profile.jwks_path),
        "CENTRAL_JWT_CLOCK_SKEW": "30",
    }


async def bootstrap_personal_review_policy(
    client: ReviewPolicyClient,
) -> dict[str, Any]:
    """Select the personal workflow policy after the client's admin join.

    Repeating this is safe because Central's policy setter is idempotent.  The
    helper carries no capability value and returns only the setter result.
    """
    return await client.board_review_policy_set(PERSONAL_REVIEW_POLICY)


def doctor_identity_summary(
    *,
    host: str,
    session: str,
    explicit_profile: Path | None = None,
    project_root: Path | None = None,
    profiles_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a pure, secret-free account/board/agent identity explanation."""
    environment = os.environ if env is None else env
    profile, source = select_personal_profile(
        explicit_profile=explicit_profile,
        project_root=project_root,
        profiles_root=profiles_root,
        env=environment,
    )
    context = resolve_personal_context(profile, host=host, session=session)
    token = read_capability(profile)
    claims = _token_claims(profile, token)
    ignored = sorted(key for key in LEGACY_OVERRIDE_ENV if environment.get(key))
    expires_at = datetime.fromtimestamp(int(claims["exp"]), timezone.utc).isoformat()
    result: dict[str, Any] = {
        "healthy": True,
        "profile_source": source,
        "mode": PROFILE_MODE,
        "review_policy": profile.review_policy,
        **context.safe_summary(),
        "capability": {
            "algorithm": "RS256",
            "kid": profile.kid,
            "expires_at": expires_at,
            "scopes": list(profile.scopes),
        },
        "ignored_legacy_environment": ignored,
        "identity_statement": (
            f"Authenticated principal {profile.principal_id} uses agent "
            f"{context.agent_name} on board {profile.board_id}."
        ),
    }
    return result


def _safe_profile_summary(profile: PersonalProfile) -> dict[str, Any]:
    return {
        "profile_path": str(profile.profile_path),
        "project_root": str(profile.project_root),
        "board_id": profile.board_id,
        "review_policy": profile.review_policy,
        "central_url": profile.central_url,
        "central_data_dir": str(profile.central_data_dir),
        "authenticated_principal_id": profile.principal_id,
        "mode": PROFILE_MODE,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, default=default_profiles_root())
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init")
    initialize.add_argument("--project", type=Path, required=True)
    initialize.add_argument("--port", type=int, default=DEFAULT_PORT)

    show = subparsers.add_parser("show")
    show.add_argument("--profile", type=Path)
    show.add_argument("--project", type=Path)
    show.add_argument("--host", required=True)
    show.add_argument("--session", required=True)

    rotate = subparsers.add_parser("rotate")
    rotate.add_argument("--profile", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "init":
        result = _safe_profile_summary(
            ensure_personal_profile(
                args.project, profiles_root=args.profiles_root, port=args.port
            )
        )
    elif args.command == "show":
        result = doctor_identity_summary(
            host=args.host,
            session=args.session,
            explicit_profile=args.profile,
            project_root=args.project,
            profiles_root=args.profiles_root,
        )
    else:
        result = _safe_profile_summary(rotate_personal_capability(args.profile))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
