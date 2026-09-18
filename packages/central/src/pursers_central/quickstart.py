"""Provision a private, local Pursers Central quickstart instance."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


PROFILE_NAME = "profile.env"
KEY_NAME = "signing-key.pem"
JWKS_NAME = "jwks.json"
ADMIN_TOKEN_NAME = "admin.jwt"
WORKER_TOKEN_NAME = "worker.jwt"
DATA_NAME = "data"
MANAGED_FILES = (
    PROFILE_NAME,
    KEY_NAME,
    JWKS_NAME,
    ADMIN_TOKEN_NAME,
    WORKER_TOKEN_NAME,
)
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
KID_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}$")
PROFILE_KEYS = frozenset(
    {
        "ONBOARD_CENTRAL_HOST",
        "ONBOARD_CENTRAL_PORT",
        "ONBOARD_CENTRAL_DATA_DIR",
        "CENTRAL_JWT_ISSUER",
        "CENTRAL_JWT_AUDIENCE",
        "CENTRAL_JWKS_PATH",
        "PURSERS_BOARD_ID",
        "PURSERS_ADMIN_TOKEN_FILE",
        "PURSERS_WORKER_TOKEN_FILE",
    }
)
RUNTIME_PROFILE_KEYS = frozenset(
    {
        "ONBOARD_CENTRAL_HOST",
        "ONBOARD_CENTRAL_PORT",
        "ONBOARD_CENTRAL_DATA_DIR",
        "CENTRAL_JWT_ISSUER",
        "CENTRAL_JWT_AUDIENCE",
        "CENTRAL_JWKS_PATH",
    }
)


class QuickstartError(RuntimeError):
    """A secret-safe quickstart provisioning or profile error."""


def _regular_file(path: Path, *, label: str) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise QuickstartError(f"{label} must be a regular file: {path}")
        return path.read_bytes()
    except QuickstartError:
        raise
    except OSError as exc:
        raise QuickstartError(f"cannot read {label} {path}") from exc


def _atomic_private_batch(
    writes: Mapping[Path, bytes], *, deletes: Sequence[Path] = ()
) -> None:
    """Commit private-file replacements/deletions together, restoring on failure."""
    destinations = tuple(writes)
    targets = (*destinations, *deletes)
    if len(set(targets)) != len(targets):
        raise QuickstartError("private file transaction contains duplicate paths")

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    committed = False
    try:
        for path, content in writes.items():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            staged[path] = temporary
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

        for path in targets:
            if os.path.lexists(path):
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise QuickstartError(
                        f"private file target must be a regular file: {path}"
                    )
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".backup", dir=path.parent
                )
                os.close(descriptor)
                backup = Path(backup_name)
                backup.unlink()
                os.link(path, backup)
                backups[path] = backup
            else:
                backups[path] = None

        for path, temporary in staged.items():
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        for path in deletes:
            path.unlink()
        for parent in {path.parent for path in targets}:
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        committed = True
    except QuickstartError:
        raise
    except OSError as exc:
        for path, backup in backups.items():
            try:
                if backup is None:
                    path.unlink(missing_ok=True)
                elif backup.exists():
                    os.rename(backup, path)
            except OSError:
                pass
        raise QuickstartError("cannot commit private file transaction") from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        if not committed:
            for path, backup in backups.items():
                if backup is not None and backup.exists():
                    try:
                        os.rename(backup, path)
                    except OSError:
                        pass


def _private_root(value: Path) -> Path:
    root = value.expanduser().absolute()
    if root == Path(root.anchor):
        raise QuickstartError("instance directory must not be a filesystem root")
    for candidate in (root, *root.parents):
        if os.path.lexists(candidate) and candidate.is_symlink():
            raise QuickstartError("instance directory and its parents must not be symlinks")
    if root.exists() and not root.is_dir():
        raise QuickstartError("instance path exists and is not a directory")
    return root


def _atomic_private_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise QuickstartError(f"cannot write private file {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, object]:
    public = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return public


def _load_private_key(path: Path) -> tuple[rsa.RSAPrivateKey, bytes]:
    content = _regular_file(path, label="signing key")
    try:
        key = serialization.load_pem_private_key(content, password=None)
    except (TypeError, ValueError) as exc:
        raise QuickstartError(
            f"signing key is not a valid unencrypted RSA key: {path}"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2_048:
        raise QuickstartError(f"signing key must be RSA with at least 2048 bits: {path}")
    return key, content


def _load_jwks(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    content = _regular_file(path, label="JWKS")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuickstartError(f"JWKS is not valid JSON: {path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise QuickstartError(f"JWKS keys must be a list: {path}")
    keys = document["keys"]
    if not all(isinstance(item, dict) for item in keys):
        raise QuickstartError(f"every JWKS key must be an object: {path}")
    kids = [item.get("kid") for item in keys]
    if any(not isinstance(kid, str) or not kid for kid in kids) or len(
        set(kids)
    ) != len(kids):
        raise QuickstartError(f"JWKS kids must be non-empty and unique: {path}")
    return document, keys


def _matching_kid(
    private_key: rsa.RSAPrivateKey, keys: Sequence[dict[str, object]], *, path: Path
) -> str:
    public = _public_jwk(private_key, "unused")
    matches = [
        item["kid"]
        for item in keys
        if item.get("kty") == "RSA"
        and item.get("n") == public.get("n")
        and item.get("e") == public.get("e")
        and "pursers_door" not in item
    ]
    if len(matches) != 1:
        raise QuickstartError(
            f"signing key does not match exactly one issuer key in {path}"
        )
    return str(matches[0])


def _retired_key_path(key_path: Path, kid: str) -> Path:
    if not KID_FILENAME_RE.fullmatch(kid):
        raise QuickstartError("issuer kid is not safe for a retired-key filename")
    return key_path.with_name(f"{key_path.stem}.{kid}.retired{key_path.suffix}")


def _read_token_file(path: Path) -> tuple[str, bool, bool]:
    content = _regular_file(path, label="token file")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QuickstartError(f"token file is not UTF-8: {path}") from exc
    trailing_newline = text.endswith("\n")
    body = text[:-1] if trailing_newline else text
    if "\n" in body or "\r" in body:
        raise QuickstartError(f"token file must contain exactly one token: {path}")
    prefix = "Authorization: Bearer "
    header_format = body.startswith(prefix)
    token = body[len(prefix) :] if header_format else body
    if not token:
        raise QuickstartError(f"token file is empty: {path}")
    return token, header_format, trailing_newline


def _token_kid(token: str, *, path: Path) -> str:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise QuickstartError(f"token file does not contain a valid JWT: {path}") from exc
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise QuickstartError(f"token file JWT has no kid: {path}")
    return kid


def rotate_key(
    key_path: Path, jwks_path: Path, token_paths: Sequence[Path]
) -> dict[str, object]:
    """Add a new issuer key and re-sign token files without a service restart."""
    if not token_paths:
        raise QuickstartError("at least one token file is required")
    if len(set(token_paths)) != len(token_paths):
        raise QuickstartError("token file paths must be unique")
    if key_path == jwks_path or key_path in token_paths or jwks_path in token_paths:
        raise QuickstartError("signing key, JWKS, and token paths must be distinct")
    old_key, old_pem = _load_private_key(key_path)
    document, keys = _load_jwks(jwks_path)
    old_kid = _matching_kid(old_key, keys, path=jwks_path)
    retired_path = _retired_key_path(key_path, old_kid)
    if os.path.lexists(retired_path):
        raise QuickstartError(f"retired signing key already exists: {retired_path}")

    new_kid = f"pursers-issuer-{secrets.token_hex(16)}"
    while any(item.get("kid") == new_kid for item in keys):
        new_kid = f"pursers-issuer-{secrets.token_hex(16)}"
    new_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    new_pem = new_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    old_index = next(index for index, item in enumerate(keys) if item["kid"] == old_kid)
    new_keys = list(keys)
    new_keys.insert(old_index + 1, _public_jwk(new_key, new_kid))
    new_document = dict(document)
    new_document["keys"] = new_keys

    now = int(time.time())
    rewritten: dict[Path, bytes] = {}
    for token_path in token_paths:
        token, header_format, trailing_newline = _read_token_file(token_path)
        token_kid = _token_kid(token, path=token_path)
        if token_kid not in {old_kid, new_kid}:
            raise QuickstartError(
                f"token file kid is not the current or new issuer key: {token_path}"
            )
        verification_key = (
            old_key.public_key() if token_kid == old_kid else new_key.public_key()
        )
        try:
            claims = jwt.decode(
                token,
                key=verification_key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise QuickstartError(f"token file signature is invalid: {token_path}") from exc
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= issued_at
        ):
            raise QuickstartError(
                f"token file must have integer exp later than integer iat: {token_path}"
            )
        lifetime = expires_at - issued_at
        claims["iat"] = now
        if "nbf" in claims:
            claims["nbf"] = now - 5
        claims["exp"] = now + lifetime
        rotated = jwt.encode(
            claims, new_key, algorithm="RS256", headers={"kid": new_kid}
        )
        rendered = ("Authorization: Bearer " if header_format else "") + rotated
        if trailing_newline:
            rendered += "\n"
        rewritten[token_path] = rendered.encode("utf-8")

    writes = {
        retired_path: old_pem,
        key_path: new_pem,
        jwks_path: (json.dumps(new_document, sort_keys=True) + "\n").encode("utf-8"),
        **rewritten,
    }
    _atomic_private_batch(writes)
    return {
        "key": key_path,
        "retired_key": retired_path,
        "jwks": jwks_path,
        "old_kid": old_kid,
        "new_kid": new_kid,
        "token_count": len(token_paths),
    }


def retire_key(
    key_path: Path,
    jwks_path: Path,
    kid: str,
    *,
    check_token_paths: Sequence[Path] = (),
) -> dict[str, object]:
    """Remove one inactive issuer key after clients have moved off it."""
    current_key, _ = _load_private_key(key_path)
    document, keys = _load_jwks(jwks_path)
    current_kid = _matching_kid(current_key, keys, path=jwks_path)
    matches = [item for item in keys if item.get("kid") == kid]
    if len(matches) != 1:
        raise QuickstartError(f"issuer kid was not found in JWKS: {kid}")
    target = matches[0]
    if "pursers_door" in target:
        raise QuickstartError(f"refusing to retire door key: {kid}")
    issuer_keys = [item for item in keys if "pursers_door" not in item]
    if len(issuer_keys) <= 1:
        raise QuickstartError("refusing to retire the last issuer key")
    if kid == current_kid:
        raise QuickstartError(f"refusing to retire the current signing key: {kid}")
    for token_path in check_token_paths:
        token, _, _ = _read_token_file(token_path)
        if _token_kid(token, path=token_path) == kid:
            raise QuickstartError(
                f"checked token file still uses the retiring kid: {token_path}"
            )

    retired_path = _retired_key_path(key_path, kid)
    _regular_file(retired_path, label="retired signing key")
    new_document = dict(document)
    new_document["keys"] = [item for item in keys if item.get("kid") != kid]
    _atomic_private_batch(
        {jwks_path: (json.dumps(new_document, sort_keys=True) + "\n").encode("utf-8")},
        deletes=(retired_path,),
    )
    return {
        "key": key_path,
        "retired_key": retired_path,
        "jwks": jwks_path,
        "retired_kid": kid,
        "issuer_key_count": len(issuer_keys) - 1,
    }


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str,
    issuer: str,
    audience: str,
    scopes: tuple[str, ...],
    board_id: str | None,
) -> str:
    issued_at = int(time.time())
    claims: dict[str, object] = {
        "iss": issuer,
        "sub": "local-board-owner",
        "aud": audience,
        "resource": audience,
        "scope": " ".join(scopes),
        "client_id": "pursers-central-quickstart",
        "iat": issued_at,
        "nbf": issued_at - 5,
        "exp": issued_at + 365 * 24 * 60 * 60,
        "jti": secrets.token_urlsafe(24),
    }
    if board_id is not None:
        claims["pursers_board"] = board_id
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _profile(values: Mapping[str, str]) -> bytes:
    lines = [
        "# Generated by pursers-central init. Read directly by pursers-central run.",
        *(f"{key}={values[key]}" for key in sorted(values)),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def init_instance(
    root_value: Path,
    *,
    port: int = 8766,
    board_id: str = "pursers-local",
    force: bool = False,
) -> dict[str, Path]:
    """Create or hard-replace one local quickstart instance."""
    if not 1 <= port <= 65_535:
        raise QuickstartError("port must be between 1 and 65535")
    if not BOARD_ID_RE.fullmatch(board_id):
        raise QuickstartError(f"board must match {BOARD_ID_RE.pattern}")
    root = _private_root(root_value)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    data_dir = root / DATA_NAME
    if os.path.lexists(data_dir) and data_dir.is_symlink():
        raise QuickstartError("data directory must not be a symlink")
    data_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(data_dir, 0o700)

    paths = {name: root / name for name in MANAGED_FILES}
    existing = [path for path in paths.values() if os.path.lexists(path)]
    for path in existing:
        if path.is_symlink() or not path.is_file():
            raise QuickstartError(f"managed path is not a regular file: {path}")
    if existing and not force:
        raise QuickstartError(
            "refusing to overwrite existing quickstart credentials; pass --force"
        )

    host = "127.0.0.1"
    audience = f"http://{host}:{port}/mcp"
    issuer = f"http://{host}:{port}/quickstart"
    kid = f"quickstart-{secrets.token_hex(16)}"
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    admin_token = _token(
        private_key,
        kid=kid,
        issuer=issuer,
        audience=audience,
        scopes=("board:read", "board:write", "board:review"),
        board_id=None,
    )
    worker_token = _token(
        private_key,
        kid=kid,
        issuer=issuer,
        audience=audience,
        scopes=("board:read", "board:write"),
        board_id=board_id,
    )
    profile_values = {
        "ONBOARD_CENTRAL_HOST": host,
        "ONBOARD_CENTRAL_PORT": str(port),
        "ONBOARD_CENTRAL_DATA_DIR": str(data_dir),
        "CENTRAL_JWT_ISSUER": issuer,
        "CENTRAL_JWT_AUDIENCE": audience,
        "CENTRAL_JWKS_PATH": str(paths[JWKS_NAME]),
        "PURSERS_BOARD_ID": board_id,
        "PURSERS_ADMIN_TOKEN_FILE": str(paths[ADMIN_TOKEN_NAME]),
        "PURSERS_WORKER_TOKEN_FILE": str(paths[WORKER_TOKEN_NAME]),
    }
    _atomic_private_write(paths[KEY_NAME], private_pem)
    _atomic_private_write(
        paths[JWKS_NAME],
        (json.dumps({"keys": [_public_jwk(private_key, kid)]}, sort_keys=True) + "\n").encode(),
    )
    _atomic_private_write(paths[ADMIN_TOKEN_NAME], (admin_token + "\n").encode())
    _atomic_private_write(paths[WORKER_TOKEN_NAME], (worker_token + "\n").encode())
    _atomic_private_write(paths[PROFILE_NAME], _profile(profile_values))
    return {"root": root, "data": data_dir, **paths}


def load_profile(root_value: Path) -> dict[str, str]:
    """Load and validate the non-shell quickstart runtime profile."""
    root = _private_root(root_value)
    profile_path = root / PROFILE_NAME
    try:
        info = profile_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise QuickstartError("quickstart profile must be a regular 0600 file")
        rows = profile_path.read_text(encoding="utf-8").splitlines()
    except QuickstartError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise QuickstartError(f"cannot read quickstart profile {profile_path}") from exc
    values: dict[str, str] = {}
    for number, row in enumerate(rows, 1):
        if not row or row.startswith("#"):
            continue
        key, separator, value = row.partition("=")
        if not separator or key not in PROFILE_KEYS or not value or key in values:
            raise QuickstartError(f"invalid quickstart profile line {number}")
        values[key] = value
    missing = sorted(PROFILE_KEYS - values.keys())
    if missing:
        raise QuickstartError("quickstart profile is missing: " + ", ".join(missing))
    try:
        port = int(values["ONBOARD_CENTRAL_PORT"])
    except ValueError as exc:
        raise QuickstartError("quickstart profile port is invalid") from exc
    if not 1 <= port <= 65_535:
        raise QuickstartError("quickstart profile port is invalid")
    if values["ONBOARD_CENTRAL_HOST"] != "127.0.0.1":
        raise QuickstartError("quickstart profile host must be 127.0.0.1")
    expected_paths = {
        "ONBOARD_CENTRAL_DATA_DIR": root / DATA_NAME,
        "CENTRAL_JWKS_PATH": root / JWKS_NAME,
        "PURSERS_ADMIN_TOKEN_FILE": root / ADMIN_TOKEN_NAME,
        "PURSERS_WORKER_TOKEN_FILE": root / WORKER_TOKEN_NAME,
    }
    for key, expected in expected_paths.items():
        if Path(values[key]) != expected:
            raise QuickstartError(f"quickstart profile {key} does not match its instance")
    if not BOARD_ID_RE.fullmatch(values["PURSERS_BOARD_ID"]):
        raise QuickstartError("quickstart profile board is invalid")
    return values


def apply_runtime_profile(root: Path) -> dict[str, str]:
    """Apply only runtime settings; credential values never enter the environment."""
    profile = load_profile(root)
    for key in RUNTIME_PROFILE_KEYS:
        os.environ[key] = profile[key]
    return profile
