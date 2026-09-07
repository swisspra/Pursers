#!/usr/bin/env python3
"""Issue and rotate Pursers door credentials without exposing bare tokens."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


DOOR_PREFIX = "prs1."
VALID_ROLES = frozenset({"worker", "reviewer"})
ROLE_SCOPES = {
    "worker": "board:read board:write",
    "reviewer": "board:read board:review",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
METADATA_KEY = "pursers_door"


class DoorAdminError(RuntimeError):
    """An expected, secret-safe command failure."""


@dataclass(frozen=True)
class IssuedCredential:
    door_string: str
    token: str = field(repr=False)
    kid: str
    claims: dict[str, Any]


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise DoorAdminError(f"{label} must match {IDENTIFIER_RE.pattern}")
    return value


def _central_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DoorAdminError("central URL must be a non-empty, trimmed URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DoorAdminError("central URL must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise DoorAdminError("central URL must not contain credentials or a fragment")
    return value


def default_issuer(central_url: str) -> str:
    parsed = urlsplit(_central_url(central_url))
    return f"{parsed.scheme}://{parsed.netloc}"


def _load_jwks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"keys": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DoorAdminError(f"cannot read valid JWKS from {path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise DoorAdminError("JWKS must be an object containing a keys list")
    if not all(isinstance(item, dict) for item in document["keys"]):
        raise DoorAdminError("every JWKS key must be an object")
    return document


def _atomic_write(
    path: Path,
    content: bytes,
    mode: int,
    *,
    before_replace: Callable[[Path, Path], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace(temporary, path)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise DoorAdminError(f"cannot atomically write {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_jwks(
    path: Path,
    document: dict[str, Any],
    *,
    before_replace: Callable[[Path, Path], None] | None = None,
) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(path, payload, mode, before_replace=before_replace)


def _key_path(keys_dir: Path, kid: str) -> Path:
    return keys_dir / f"{kid}.pem"


def _write_private_key(path: Path, private_key: rsa.RSAPrivateKey) -> None:
    payload = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _atomic_write(path, payload, 0o600)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DoorAdminError(f"private key permissions are not 0600: {path}")


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise DoorAdminError(f"private key must be a regular 0600 file: {path}")
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except DoorAdminError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise DoorAdminError(f"cannot load private key for kid {path.stem}") from exc
    if not isinstance(loaded, rsa.RSAPrivateKey) or loaded.key_size != 2048:
        raise DoorAdminError("door private key must be RSA-2048")
    return loaded


def _identity(
    board: str,
    role: str | None,
    *,
    named: bool,
    subject: str | None,
    scope: str | None,
) -> tuple[str, str, str, str]:
    board = _identifier(board, "board")
    if named:
        if role is not None:
            raise DoorAdminError("--named cannot be combined with --role")
        if subject is None or not subject.strip() or subject != subject.strip():
            raise DoorAdminError("--named requires a non-empty, trimmed --sub")
        if scope is None or not scope.split():
            raise DoorAdminError("--named requires a non-empty --scope")
        normalized_scope = " ".join(dict.fromkeys(scope.split()))
        return "named", subject, subject, normalized_scope
    if role not in VALID_ROLES:
        raise DoorAdminError("--role must be worker or reviewer")
    if subject is not None or scope is not None:
        raise DoorAdminError("--sub and --scope require --named")
    name = f"door-{board}-{role}"
    return role, f"door:{board}:{role}", name, ROLE_SCOPES[role]


def _kid_base(board: str, identity_role: str) -> str:
    return f"door-{board}-{identity_role}"


def _version(kid: str, base: str) -> int | None:
    match = re.fullmatch(re.escape(base) + r"-v([1-9][0-9]*)", kid)
    return int(match.group(1)) if match else None


def _matching_keys(
    document: dict[str, Any], base: str
) -> list[tuple[int, dict[str, Any]]]:
    matches: list[tuple[int, dict[str, Any]]] = []
    for item in document["keys"]:
        kid = item.get("kid")
        if isinstance(kid, str) and (version := _version(kid, base)) is not None:
            matches.append((version, item))
    return sorted(matches, key=lambda entry: entry[0])


def _highest_disk_version(keys_dir: Path, base: str) -> int:
    highest = 0
    if keys_dir.exists():
        for path in keys_dir.glob(f"{base}-v*.pem"):
            version = _version(path.stem, base)
            if version is not None:
                highest = max(highest, version)
    return highest


def _public_jwk(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str,
    board: str,
    role: str,
    subject: str,
    client_id: str,
    scope: str,
    expires_at: int,
) -> dict[str, Any]:
    public = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public.update(
        {
            "kid": kid,
            "alg": "RS256",
            "use": "sig",
            METADATA_KEY: {
                "board": board,
                "role": role,
                "sub": subject,
                "client_id": client_id,
                "scope": scope,
                "exp": expires_at,
            },
        }
    )
    return public


def _encode_door(central_url: str, board: str, role: str, token: str) -> str:
    payload = json.dumps(
        {"u": central_url, "b": board, "r": role, "t": token},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return DOOR_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_door(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(DOOR_PREFIX):
        raise DoorAdminError(f"door string must start with {DOOR_PREFIX}")
    encoded = value[len(DOOR_PREFIX) :]
    if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise DoorAdminError("door string payload is not valid base64url")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DoorAdminError("door string payload is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"u", "b", "r", "t"}:
        raise DoorAdminError("door string JSON must contain exactly u, b, r, and t")
    if not all(isinstance(payload[key], str) and payload[key] for key in payload):
        raise DoorAdminError("door string fields must be non-empty strings")
    try:
        header = jwt.get_unverified_header(payload["t"])
        claims = jwt.decode(
            payload["t"],
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["RS256"],
        )
    except jwt.PyJWTError as exc:
        raise DoorAdminError("door string contains a malformed token") from exc
    kid = header.get("kid")
    expires_at = claims.get("exp")
    if not isinstance(kid, str) or not kid or type(expires_at) is not int:
        raise DoorAdminError("door token must contain kid and integer exp")
    return {
        "u": payload["u"],
        "b": payload["b"],
        "r": payload["r"],
        "kid": kid,
        "exp": expires_at,
    }


def issue_credential(
    *,
    board: str,
    role: str | None,
    central_url: str,
    jwks_path: Path,
    keys_dir: Path,
    exp_days: int = 180,
    issuer: str | None = None,
    named: bool = False,
    subject: str | None = None,
    scope: str | None = None,
    rotate: bool = False,
    now: int | None = None,
    before_jwks_replace: Callable[[Path, Path], None] | None = None,
) -> IssuedCredential:
    if exp_days < 1:
        raise DoorAdminError("exp-days must be at least 1")
    board = _identifier(board, "board")
    central_url = _central_url(central_url)
    identity_role, actual_sub, client_id, actual_scope = _identity(
        board, role, named=named, subject=subject, scope=scope
    )
    actual_issuer = issuer if issuer is not None else default_issuer(central_url)
    if not isinstance(actual_issuer, str) or not actual_issuer.strip():
        raise DoorAdminError("issuer must be a non-empty string")
    document = _load_jwks(jwks_path)
    base = _kid_base(board, identity_role)
    active = _matching_keys(document, base)
    if len(active) > 1:
        raise DoorAdminError(f"JWKS has multiple active keys for {base}")

    if rotate:
        if not active:
            raise DoorAdminError(f"cannot rotate {base}: no active kid")
        next_version = max(active[-1][0], _highest_disk_version(keys_dir, base)) + 1
        kid = f"{base}-v{next_version}"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _write_private_key(_key_path(keys_dir, kid), private_key)
    elif active:
        kid = str(active[0][1]["kid"])
        private_key = _load_private_key(_key_path(keys_dir, kid))
    else:
        next_version = _highest_disk_version(keys_dir, base) + 1
        kid = f"{base}-v{next_version}"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _write_private_key(_key_path(keys_dir, kid), private_key)

    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + exp_days * 86_400
    claims = {
        "iss": actual_issuer,
        "sub": actual_sub,
        "client_id": client_id,
        "aud": central_url,
        "resource": central_url,
        "scope": actual_scope,
        "iat": issued_at,
        "nbf": issued_at - 60,
        "exp": expires_at,
    }
    token = jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": kid, "typ": "JWT"}
    )
    public = _public_jwk(
        private_key,
        kid=kid,
        board=board,
        role=identity_role,
        subject=actual_sub,
        client_id=client_id,
        scope=actual_scope,
        expires_at=expires_at,
    )
    document["keys"] = [
        item
        for item in document["keys"]
        if _version(str(item.get("kid", "")), base) is None
    ] + [public]
    _write_jwks(jwks_path, document, before_replace=before_jwks_replace)
    return IssuedCredential(
        door_string=_encode_door(central_url, board, identity_role, token),
        token=token,
        kid=kid,
        claims=claims,
    )


def list_doors(jwks_path: Path) -> list[dict[str, Any]]:
    doors: list[dict[str, Any]] = []
    for item in _load_jwks(jwks_path)["keys"]:
        metadata = item.get(METADATA_KEY)
        kid = item.get("kid")
        if not isinstance(metadata, dict) or not isinstance(kid, str):
            continue
        if not {"board", "role", "exp"} <= set(metadata):
            continue
        doors.append(
            {
                "board": metadata["board"],
                "role": metadata["role"],
                "kid": kid,
                "exp": metadata["exp"],
            }
        )
    return sorted(doors, key=lambda item: (str(item["board"]), str(item["role"])))


def revoke_kid(jwks_path: Path, kid: str) -> None:
    kid = _identifier(kid, "kid")
    document = _load_jwks(jwks_path)
    retained = [item for item in document["keys"] if item.get("kid") != kid]
    if len(retained) == len(document["keys"]):
        raise DoorAdminError(f"unknown kid: {kid}")
    document["keys"] = retained
    _write_jwks(jwks_path, document)


def _add_issue_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--board", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--role", choices=sorted(VALID_ROLES))
    identity.add_argument("--named", action="store_true")
    parser.add_argument("--sub")
    parser.add_argument("--scope")
    parser.add_argument("--central-url", required=True)
    parser.add_argument("--issuer")
    parser.add_argument("--jwks", required=True, type=Path)
    parser.add_argument("--keys-dir", required=True, type=Path)
    parser.add_argument("--exp-days", type=int, default=180)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Pursers door credentials.")
    commands = parser.add_subparsers(dest="command", required=True)
    _add_issue_arguments(commands.add_parser("issue", help="issue or refresh a door"))
    _add_issue_arguments(commands.add_parser("rotate", help="rotate and revoke a door"))
    listing = commands.add_parser("list", help="list public door metadata")
    listing.add_argument("--jwks", required=True, type=Path)
    revoke = commands.add_parser("revoke-kid", help="remove one public signing key")
    revoke.add_argument("kid")
    revoke.add_argument("--jwks", required=True, type=Path)
    decode = commands.add_parser("decode", help="show non-secret door metadata")
    decode.add_argument("door_string")
    return parser


def execute(args: argparse.Namespace) -> None:
    if args.command in {"issue", "rotate"}:
        credential = issue_credential(
            board=args.board,
            role=args.role,
            central_url=args.central_url,
            jwks_path=args.jwks,
            keys_dir=args.keys_dir,
            exp_days=args.exp_days,
            issuer=args.issuer,
            named=args.named,
            subject=args.sub,
            scope=args.scope,
            rotate=args.command == "rotate",
        )
        print(credential.door_string)
    elif args.command == "list":
        print(json.dumps(list_doors(args.jwks), indent=2, sort_keys=True))
    elif args.command == "revoke-kid":
        revoke_kid(args.jwks, args.kid)
        print(f"revoked {args.kid}")
    elif args.command == "decode":
        print(json.dumps(decode_door(args.door_string), indent=2, sort_keys=True))
    else:  # pragma: no cover - argparse prevents this
        raise DoorAdminError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        execute(build_parser().parse_args(argv))
    except DoorAdminError as exc:
        print(f"pursers-door: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
