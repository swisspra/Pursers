#!/usr/bin/env python3
"""O1 staged HTTP-loopback cutover toolkit (preparation, gates, operator activation).

This module turns the approved O1 readiness runbook (ticket TK-3776ba2309a8,
commit 47bd5d39f2b75fa166a12948200f71bc665099ed) into an executable, fail-closed
plan for the single-machine cutover from the current HTTPS a25 listener to the
released plain-HTTP loopback resource:

    prepare -> preflight -> dry-run -> activate -> rollback

Safety contract
---------------
* ``dry-run`` and ``preflight`` are strictly non-mutating: they read live paths,
  prove every activation target, evaluate every gate, and write evidence only
  inside the operator-selected staging root.
* ``activate`` mutates live files only, never service lifecycle. It requires an
  explicit operator confirmation, a matching fresh dry-run evidence record, and
  every gate to pass. Each replacement is journalled and atomic
  (temporary file plus ``os.replace``) so an interrupted activation is
  detectable and reversible.
* ``rollback`` restores from the single backup unit in reverse journal order and
  refuses a mixed token/JWKS restore.
* Service and AionUI Team lifecycle steps are emitted as an operator command
  sheet and are never executed here; the toolkit reports the honest host
  capability gap instead of inventing a hot-reload API.
* No private source, configuration, token content, or door material is ever
  written into the repository or into a board-facing report. Reports are
  sanitized to ``/PATH/TO`` placeholders and credential material is reduced to
  non-secret claim metadata.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
TOOLKIT_IDENTIFIER = "o1-cutover-toolkit/1"
RUNBOOK_REFERENCE = "TK-3776ba2309a8@47bd5d39f2b75fa166a12948200f71bc665099ed"

# Released O1 target resource. Scheme, host, port, and path are identity bearing.
TARGET_URL = "http://127.0.0.1:8766/mcp"
TARGET_ISSUER = "http://127.0.0.1:8766"
TARGET_AUDIENCE = TARGET_URL
EXPECTED_CENTRAL_VERSION = "0.1.0a29"
DASHBOARD_UI_URL = "http://127.0.0.1:8899"
LOOPBACK_HOST = "127.0.0.1"
EXPECTED_PORT = 8766
EXPECTED_PATH = "/mcp"

DEFAULT_ACTIVE_BOARDS: tuple[str, ...] = ("pursers", "fullplatts", "mi-mcp-prd")
HELD_TICKET_STATES: tuple[str, ...] = (
    "claimed",
    "submitted",
    "reviewing",
    "in_review",
    "rejected",
    "needs_human",
)
EXPECTED_SEAT_COUNT = 8
SEAT_ROLES: tuple[str, ...] = ("worker", "reviewer")
MAX_SEAT_TIER = 2
DOOR_ROLES: tuple[str, ...] = ("worker", "reviewer")
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

# A CA override anywhere in the staged HTTP cutover contradicts the target.
FORBIDDEN_CA_ENV: tuple[str, ...] = (
    "PURSERS_CA_FILE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
FORBIDDEN_LIFECYCLE_COMMANDS: tuple[str, ...] = ("start-all",)
FORBIDDEN_WAIT_FLAGS: tuple[str, ...] = ("--poll",)

# Public a25 release digests recorded by the readiness audit.
A25_WHEEL_DIGESTS: Mapping[str, str] = {
    "pursers_central-0.1.0a29-py3-none-any.whl": (
        "8defa64200fa53425079408744243e2392ee5ddbffaea14d249dc3766bbfa118"
    ),
    "pursers_client-0.1.0a22-py3-none-any.whl": (
        "fe5d9a28e16ab5da5b5141798e814fbe68a95489f20ba2835ed0d44f54134cb4"
    ),
    "pursers_wait_bridge-0.1.0a15-py3-none-any.whl": (
        "faa60203ee6a1857636322def8e5f3c92510c87983cbcd07d2ebd68b9e7d3611"
    ),
}

REQUIRED_NAMED_CREDENTIALS: Mapping[str, frozenset[str]] = {
    "coordinator-main": frozenset({"board:read", "board:write", "board:coordinate"}),
    "coordinator-intake": frozenset({"board:read", "board:coordinate", "board:intake"}),
    "dashboard-admin": frozenset({"board:read", "board:write", "board:review"}),
}
FORBIDDEN_CREDENTIAL_SCOPES: Mapping[str, frozenset[str]] = {
    "coordinator-intake": frozenset({"board:write"}),
}

STAGING_SUBDIRS: tuple[str, ...] = (
    "backup",
    "target-jwt",
    "target-jwt/door-keys",
    "staged-configs",
    "staged-seats",
    "cutover-evidence",
    "journal",
)
JOURNAL_NAME = "activation-journal.json"
EVIDENCE_PREFIX = "dry-run"
DEFAULT_EVIDENCE_MAX_AGE_S = 900
DEFAULT_SNAPSHOT_MAX_AGE_S = 900
REDACTED = "<redacted>"
REDACT_KEY_PARTS: tuple[str, ...] = (
    "token",
    "door",
    "secret",
    "private_key",
    "key_pem",
    "password",
    "authorization",
    "jwk_private",
)


class CutoverError(RuntimeError):
    """Base class for toolkit refusals."""


class ConfigError(CutoverError):
    """The operator configuration is unusable; nothing was attempted."""


class GateFailure(CutoverError):
    """One or more fail-closed gates refused the requested phase."""

    def __init__(self, gate: str, reasons: Sequence[str]) -> None:
        self.gate = gate
        self.reasons = tuple(reasons)
        super().__init__(f"gate {gate} refused: " + "; ".join(reasons))


@dataclass(frozen=True)
class Finding:
    """A single gate result. ``detail`` is always sanitized before reporting."""

    gate: str
    ok: bool
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "ok": self.ok,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class Step:
    """One ordered plan step."""

    step_id: str
    phase: str
    summary: str
    mutating: bool
    execution: str  # "toolkit" | "operator"
    targets: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase,
            "summary": self.summary,
            "mutating": self.mutating,
            "execution": self.execution,
            "targets": list(self.targets),
            "requires": list(self.requires),
        }


@dataclass(frozen=True)
class Artifact:
    """A hashed file inside the backup unit or the staged artifact set."""

    identifier: str
    rel_path: str
    sha256: str
    size: int
    mode: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class FileSwap:
    """A journalled, atomic live-file replacement performed during activation."""

    step_id: str
    identifier: str
    live_path: Path
    staged_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "identifier": self.identifier,
            "live_path": str(self.live_path),
            "staged_path": str(self.staged_path),
        }


# ---------------------------------------------------------------------------
# Ordered plan. Operator-executed lifecycle steps are interleaved so the sheet
# is complete, but the toolkit never runs them.
# ---------------------------------------------------------------------------

PLAN: tuple[Step, ...] = (
    Step("declare-freeze", "prepare", "Disable dispatch and pause both AionUI Teams",
         False, "operator"),
    Step("stage-skeleton", "prepare", "Create the private staging root skeleton (0700)",
         True, "toolkit", targets=("staging_root",)),
    Step("drain-work", "prepare", "Require zero held tickets on every active board",
         False, "operator", requires=("gate_drain",)),
    Step("record-baseline", "prepare", "Record health, registry, listener and credential metadata",
         False, "operator"),
    Step("backup-rollback-unit", "prepare", "Copy and hash the entire rollback unit",
         True, "toolkit", targets=("backup_root",), requires=("gate_backup",)),
    Step("verify-a25-artifacts", "preflight", "Verify release digests and build the target venv",
         False, "toolkit", requires=("gate_artifacts",)),
    Step("stage-target-credentials", "preflight",
         "Stage named credentials, doors and the target JWKS in the private root",
         False, "operator", requires=("gate_credentials",)),
    Step("commit-target-memberships", "preflight",
         "Add target principals to every active board while HTTPS is live",
         False, "operator", requires=("gate_membership",)),
    Step("prepare-seat-folders", "preflight",
         "Regenerate all eight seat folders preserving name, role and tier",
         False, "operator", requires=("gate_seats",)),
    Step("stage-dependent-configs", "preflight",
         "Stage coordinator, dashboard, bridge and registry configuration",
         False, "operator", requires=("gate_configs",)),
    Step("sandbox-acceptance", "preflight",
         "Run sandbox acceptance on a copied data root and a different loopback port",
         False, "operator", requires=("gate_url",)),
    Step("dry-run", "dry-run", "Prove every target and every gate without mutating live state",
         False, "toolkit",
         requires=("gate_staging", "gate_url", "gate_artifacts", "gate_backup",
                   "gate_drain", "gate_credentials", "gate_membership",
                   "gate_seats", "gate_configs", "gate_legacy", "gate_host_capability")),
    Step("stop-consumers", "activate",
         "Stop coordinator, dashboard and both Teams; stop the old Central last",
         True, "operator"),
    Step("swap-launcher", "activate", "Atomically replace the Central launcher and profile",
         True, "toolkit", targets=("launcher",), requires=("gate_backup", "gate_artifacts")),
    Step("swap-jwks", "activate", "Atomically replace the JWKS with the staged target key set",
         True, "toolkit", targets=("jwks",), requires=("gate_credentials",)),
    Step("swap-credentials", "activate", "Atomically replace named credential and door files",
         True, "toolkit", targets=("credentials",), requires=("gate_credentials",)),
    Step("swap-configs", "activate", "Atomically replace coordinator, dashboard and bridge configs",
         True, "toolkit", targets=("configs",), requires=("gate_configs",)),
    Step("swap-seats", "activate", "Atomically replace managed seat files in all eight folders",
         True, "toolkit", targets=("seats",), requires=("gate_seats",)),
    Step("start-central", "activate", "Start the HTTP Central only and probe health",
         True, "operator"),
    Step("verify-memberships-live", "activate",
         "Verify registry, members and project registry with the new admin credential",
         False, "operator", requires=("gate_membership",)),
    Step("start-consumers", "activate", "Start coordinator, dashboard and the canary seats",
         True, "operator"),
    Step("doctor-and-resume", "activate", "Run registry/seat doctor, resume Teams, dispatch",
         False, "operator"),
    Step("rollback-restore", "rollback",
         "Restore the backup unit in reverse journal order",
         True, "toolkit", targets=("backup_root",), requires=("gate_backup",)),
    Step("rollback-restart", "rollback",
         "Restart the old HTTPS Central, coordinator, dashboard and canaries",
         True, "operator"),
)

TOOLKIT_ACTIVATION_STEPS: tuple[str, ...] = tuple(
    step.step_id for step in PLAN
    if step.phase == "activate" and step.execution == "toolkit"
)

# Plan step -> toolkit subcommand. Activation swaps share one gated command.
TOOLKIT_COMMANDS: Mapping[str, str] = {
    "stage-skeleton": "prepare",
    "backup-rollback-unit": "backup",
    "verify-a25-artifacts": "verify-artifacts",
    "dry-run": "dry-run",
    "rollback-restore": "rollback",
    **{step_id: "activate" for step_id in TOOLKIT_ACTIVATION_STEPS},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{label} unreadable: {exc}") from exc


def write_bytes_atomic(path: Path, payload: bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Write ``payload`` atomically: temp file in the same directory plus replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{path}.tmp-{os.getpid()}")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json_atomic(path: Path, value: Any, mode: int = PRIVATE_FILE_MODE) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    write_bytes_atomic(path, payload, mode)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, PRIVATE_DIR_MODE)


def snapshot_age_seconds(recorded_at: str | None, now: datetime | None = None) -> float | None:
    if not recorded_at:
        return None
    try:
        stamp = datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return ((now or utc_now()) - stamp).total_seconds()


# ---------------------------------------------------------------------------
# Sanitization: board-facing output uses placeholders, never private paths or
# credential material.
# ---------------------------------------------------------------------------


class Sanitizer:
    """Replace private filesystem roots with placeholders and redact secrets."""

    def __init__(self, mappings: Mapping[str | Path, str] | None = None) -> None:
        self._pairs: list[tuple[str, str]] = []
        for real, placeholder in (mappings or {}).items():
            self.add(real, placeholder)

    def add(self, real: str | Path, placeholder: str) -> None:
        text = str(real)
        if not text:
            return
        self._pairs.append((text, placeholder))
        self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def text(self, value: str) -> str:
        out = value
        for real, placeholder in self._pairs:
            if real in out:
                out = out.replace(real, placeholder)
        return out

    def structure(self, value: Any, key: str | None = None) -> Any:
        if key and any(part in key.lower() for part in REDACT_KEY_PARTS):
            return REDACTED
        if isinstance(value, Mapping):
            return {str(k): self.structure(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.structure(item, key) for item in value]
        if isinstance(value, (str,)):
            return self.text(value)
        if isinstance(value, Path):
            return self.text(str(value))
        return value


def default_sanitizer(config: "CutoverConfig") -> Sanitizer:
    sanitizer = Sanitizer()
    sanitizer.add(config.repo_root, "/PATH/TO/pursers-source")
    sanitizer.add(config.staging_root, "/PATH/TO/staging")
    sanitizer.add(config.backup_root, "/PATH/TO/backup")
    sanitizer.add(config.live_root, "/PATH/TO/live")
    if config.operator_toolkit:
        sanitizer.add(config.operator_toolkit, "/PATH/TO/operator-private")
    for index, seat in enumerate(config.seats):
        sanitizer.add(seat.root, f"/PATH/TO/seat-{index:02d}")
    return sanitizer


# ---------------------------------------------------------------------------
# URL binding: the released target is one exact loopback resource.
# ---------------------------------------------------------------------------

URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<authority>[^/?#]*)(?P<path>/[^?#]*)?$"
)


@dataclass(frozen=True)
class LoopbackUrl:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def normalized(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"

    @property
    def issuer(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_loopback_url(url: str, label: str = "url") -> LoopbackUrl:
    """Strictly parse a loopback Central resource URL.

    Rejects userinfo, query strings, fragments, non-loopback hosts, missing or
    non-numeric ports, and empty paths. Scheme, host, port, and path are all
    identity bearing for URL-bound credentials.
    """
    if not isinstance(url, str) or not url.strip():
        raise GateFailure("url_binding", [f"{label}: empty url"])
    text = url.strip()
    match = URL_RE.match(text)
    if not match:
        raise GateFailure("url_binding", [f"{label}: not an absolute http(s) resource url"])
    if "?" in text or "#" in text:
        raise GateFailure("url_binding", [f"{label}: query/fragment not permitted"])
    authority = match.group("authority")
    if "@" in authority or not authority:
        raise GateFailure("url_binding", [f"{label}: userinfo or empty authority not permitted"])
    if ":" not in authority:
        raise GateFailure("url_binding", [f"{label}: explicit port is required"])
    host, _, port_text = authority.partition(":")
    if not port_text.isdigit():
        raise GateFailure("url_binding", [f"{label}: non-numeric port"])
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise GateFailure("url_binding", [f"{label}: port out of range"])
    if host != LOOPBACK_HOST:
        raise GateFailure("url_binding", [f"{label}: host must be loopback {LOOPBACK_HOST}"])
    path = match.group("path") or ""
    if not path:
        raise GateFailure("url_binding", [f"{label}: path is required"])
    return LoopbackUrl(match.group("scheme"), host, port, path)


def require_target_url(url: str, label: str = "target_url") -> LoopbackUrl:
    parsed = parse_loopback_url(url, label)
    reasons: list[str] = []
    if parsed.scheme != "http":
        reasons.append(f"{label}: scheme must be http for the released target")
    if parsed.port != EXPECTED_PORT:
        reasons.append(f"{label}: port must be {EXPECTED_PORT}")
    if parsed.path != EXPECTED_PATH:
        reasons.append(f"{label}: path must be {EXPECTED_PATH}")
    if reasons:
        raise GateFailure("url_binding", reasons)
    return parsed


def evidence_url_acceptable(evidence_url: str, gate_url: str) -> bool:
    """True only when evidence was captured against the exact gated resource.

    Sandbox evidence from another loopback port never satisfies a production
    URL-bound gate.
    """
    try:
        left = parse_loopback_url(evidence_url, "evidence_url").normalized
        right = parse_loopback_url(gate_url, "gate_url").normalized
    except GateFailure:
        return False
    return left == right


# ---------------------------------------------------------------------------
# Credential claim metadata. Token bodies are never returned or printed.
# ---------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise GateFailure("credentials", [f"token segment is not base64url: {exc}"]) from exc


def decode_token_claims(token_text: str, identifier: str) -> dict[str, Any]:
    """Decode only the claim set of a compact token; never echo the token."""
    if not isinstance(token_text, str) or not token_text.strip():
        raise GateFailure("credentials", [f"{identifier}: empty token file"])
    parts = token_text.strip().split(".")
    if len(parts) != 3:
        raise GateFailure("credentials", [f"{identifier}: not a three-segment compact token"])
    payload = _b64url_decode(parts[1])
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure("credentials", [f"{identifier}: claim set is not JSON"]) from exc
    if not isinstance(claims, dict):
        raise GateFailure("credentials", [f"{identifier}: claim set is not an object"])
    return claims


def decode_token_header(token_text: str, identifier: str) -> dict[str, Any]:
    """Decode the JOSE header (segment 0) of a compact token."""
    if not isinstance(token_text, str) or not token_text.strip():
        raise GateFailure("credentials", [f"{identifier}: empty token file"])
    parts = token_text.strip().split(".")
    if len(parts) != 3:
        raise GateFailure("credentials", [f"{identifier}: not a three-segment compact token"])
    header = _b64url_decode(parts[0])
    try:
        value = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure("credentials", [f"{identifier}: JOSE header is not JSON"]) from exc
    if not isinstance(value, dict):
        raise GateFailure("credentials", [f"{identifier}: JOSE header is not an object"])
    return value


def claim_scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    for key in ("scope", "scopes", "board_scopes"):
        value = claims.get(key)
        if isinstance(value, str):
            return frozenset(part for part in value.split() if part)
        if isinstance(value, (list, tuple)):
            return frozenset(str(item) for item in value)
    return frozenset()


def claim_audiences(claims: Mapping[str, Any]) -> tuple[str, ...]:
    value = claims.get("aud")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def credential_binding_findings(
    identifier: str,
    token_text: str,
    url: str,
    required_scopes: frozenset[str] = frozenset(),
    forbidden_scopes: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Strict single-value aud/resource/issuer coherence for one URL binding."""
    findings: list[Finding] = []
    parsed = parse_loopback_url(url, f"{identifier}.url")
    try:
        header = decode_token_header(token_text, identifier)
        claims = decode_token_claims(token_text, identifier)
    except GateFailure as exc:
        return [Finding("credentials", False, exc.reasons[0], {"identifier": identifier})]

    header_kid = header.get("kid")
    if not isinstance(header_kid, str) or not header_kid.strip():
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: JOSE header kid is missing or empty",
            {"identifier": identifier}))

    audiences = claim_audiences(claims)
    if len(audiences) != 1:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: audience must be exactly one value, found {len(audiences)}",
            {"identifier": identifier}))
    elif audiences[0] != parsed.normalized:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: aud does not equal the bound resource",
            {"identifier": identifier, "expected_aud": parsed.normalized}))

    resource = claims.get("resource")
    if not isinstance(resource, str) or not resource:
        findings.append(Finding(
            "credentials", False, f"{identifier}: resource claim is missing",
            {"identifier": identifier}))
    elif resource != parsed.normalized:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: resource must equal the configured audience exactly",
            {"identifier": identifier}))
    elif audiences and resource != audiences[0]:
        findings.append(Finding(
            "credentials", False, f"{identifier}: resource does not equal aud",
            {"identifier": identifier}))

    issuer = claims.get("iss")
    if not isinstance(issuer, str) or issuer != parsed.issuer:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: issuer does not match the bound resource issuer",
            {"identifier": identifier, "expected_iss": parsed.issuer}))

    scopes = claim_scopes(claims)
    missing = sorted(required_scopes - scopes)
    if missing:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: missing required scopes {', '.join(missing)}",
            {"identifier": identifier}))
    surplus_forbidden = sorted(forbidden_scopes & scopes)
    if surplus_forbidden:
        findings.append(Finding(
            "credentials", False,
            f"{identifier}: holds forbidden scopes {', '.join(surplus_forbidden)}",
            {"identifier": identifier}))

    metadata = {
        "identifier": identifier,
        "kid": header_kid,
        "client_id": claims.get("client_id"),
        "sub": claims.get("sub"),
        "exp": claims.get("exp"),
        "scopes": sorted(scopes),
        "bound_url": parsed.normalized,
    }
    if not any(not finding.ok for finding in findings):
        findings.append(Finding(
            "credentials", True,
            f"{identifier}: aud/resource/issuer coherent with the bound resource", metadata))
    return findings


def token_kid(token_text: str, identifier: str) -> str | None:
    try:
        header = decode_token_header(token_text, identifier)
    except GateFailure:
        return None
    kid = header.get("kid")
    return kid.strip() if isinstance(kid, str) and kid.strip() else None


def jwks_kids(jwks_obj: Any) -> set[str]:
    if not isinstance(jwks_obj, Mapping):
        raise GateFailure("credentials", ["jwks: document is not an object"])
    keys = jwks_obj.get("keys")
    if not isinstance(keys, list):
        raise GateFailure("credentials", ["jwks: 'keys' must be a list"])
    kids: set[str] = set()
    for entry in keys:
        if isinstance(entry, Mapping) and entry.get("kid"):
            kids.add(str(entry["kid"]))
    return kids


def kid_coherence_finding(
    gate: str,
    label: str,
    required_kids: Iterable[str | None],
    available_kids: Iterable[str],
) -> Finding:
    declared = list(required_kids)
    absent = sum(1 for kid in declared if not isinstance(kid, str) or not kid.strip())
    required = {kid.strip() for kid in declared if isinstance(kid, str) and kid.strip()}
    available = set(available_kids)
    if not declared or absent:
        return Finding(
            gate, False,
            f"{label}: every compact credential requires a non-empty JOSE header kid",
            {"label": label, "credential_count": len(declared), "missing_kid_count": absent})
    missing = sorted(required - available)
    if missing:
        return Finding(
            gate, False,
            f"{label}: signing keys absent from the verifying JWKS ({len(missing)} kid(s))",
            {"label": label, "missing_kid_count": len(missing)})
    return Finding(gate, True, f"{label}: every signing key is present in the JWKS",
                   {"label": label, "kid_count": len(required)})


# ---------------------------------------------------------------------------
# Backup unit, manifest and integrity verification.
# ---------------------------------------------------------------------------


def backup_member_path(backup_root: Path, identifier: str) -> Path:
    """Resolve a backup member without allowing traversal or symlink hops."""
    try:
        safe = _relative_identifier(identifier, "rollback unit")
    except ConfigError as exc:
        raise GateFailure("backup", [str(exc)]) from exc
    root = backup_root.resolve(strict=False)
    candidate = root
    for part in PurePosixPath(safe).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise GateFailure(
                "backup", [f"rollback identifier traverses a symlink: {identifier!r}"])
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GateFailure(
            "backup", [f"rollback identifier escapes backup_root: {identifier!r}"]) from exc
    return candidate


def hash_tree(root: Path, base: Path) -> list[tuple[str, Path]]:
    """Return (relative posix path, absolute path) for every regular file."""
    out: list[tuple[str, Path]] = []
    if base.is_file():
        try:
            return [(base.relative_to(root).as_posix(), base)]
        except ValueError:
            return [(base.name, base)]
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out.append((path.relative_to(root).as_posix(), path))
    return out


def build_backup_manifest(
    backup_root: Path,
    entries: Sequence[tuple[str, Path]],
) -> list[Artifact]:
    """Hash the copied rollback unit already present under ``backup_root``."""
    artifacts: list[Artifact] = []
    for identifier, relative in entries:
        base = backup_member_path(backup_root, relative.as_posix())
        if not base.exists():
            raise GateFailure(
                "backup", [f"rollback unit member is missing from the backup: {identifier}"])
        for rel, path in hash_tree(backup_root, base):
            artifacts.append(Artifact(identifier, rel, sha256_file(path),
                                      path.stat().st_size, file_mode(path)))
    return artifacts


def copy_rollback_unit(
    config: "CutoverConfig",
    entries: Sequence[tuple[str, Path]],
) -> list[Artifact]:
    """Copy each live rollback-unit member into the backup root preserving modes."""
    planned = [
        (identifier, live, backup_member_path(config.backup_root, identifier))
        for identifier, live in entries
    ]
    config.backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(config.backup_root, PRIVATE_DIR_MODE)
    copied: list[tuple[str, Path]] = []
    for identifier, live, destination in planned:
        relative = Path(identifier)
        if live.is_dir():
            for rel, path in hash_tree(live, live):
                target = destination / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                payload = path.read_bytes()
                write_bytes_atomic(target, payload, file_mode(path))
        elif is_regular_file(live):
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(destination, live.read_bytes(), file_mode(live))
        else:
            raise GateFailure(
                "backup", [f"rollback unit member is not a regular file or directory: {identifier}"])
        copied.append((identifier, relative))
    artifacts = build_backup_manifest(config.backup_root, copied)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "runbook": RUNBOOK_REFERENCE,
        "created_at": iso_now(),
        "target_url": config.target_url,
        "previous_url": config.previous_url,
        "artifacts": [artifact.as_dict() for artifact in artifacts],
    }
    write_json_atomic(config.backup_manifest_path, manifest)
    return artifacts


def verify_backup_integrity(config: "CutoverConfig") -> list[Finding]:
    """Fail closed on a missing manifest, missing member, or hash/mode drift."""
    findings: list[Finding] = []
    if not config.backup_manifest_path.is_file():
        return [Finding("backup", False, "backup manifest is absent",
                        {"manifest": str(config.backup_manifest_path)})]
    manifest = read_json(config.backup_manifest_path, "backup manifest")
    artifacts = manifest.get("artifacts") if isinstance(manifest, Mapping) else None
    if not isinstance(artifacts, list) or not artifacts:
        return [Finding("backup", False, "backup manifest declares no artifacts", {})]

    seen: set[str] = set()
    drift = 0
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            drift += 1
            continue
        rel = str(raw.get("rel_path") or "")
        seen.add(str(raw.get("identifier") or ""))
        try:
            path = backup_member_path(config.backup_root, rel)
        except GateFailure:
            drift += 1
            continue
        if not is_regular_file(path):
            drift += 1
            continue
        if sha256_file(path) != raw.get("sha256"):
            drift += 1
            continue
        if file_mode(path) != int(raw.get("mode") or 0):
            drift += 1
    if drift:
        findings.append(Finding("backup", False,
                                f"backup unit integrity check found {drift} drifted member(s)",
                                {"drifted": drift, "members": len(artifacts)}))
    else:
        findings.append(Finding("backup", True,
                                f"backup unit verified: {len(artifacts)} member file(s)",
                                {"members": len(artifacts), "identifiers": sorted(seen)}))

    declared = {identifier for identifier, _ in config.rollback_unit()}
    missing = sorted(declared - seen)
    if missing:
        findings.append(Finding("backup", False,
                                f"rollback unit is incomplete: {len(missing)} identifier(s) not backed up",
                                {"missing_count": len(missing), "missing": missing}))
    return findings


def backup_is_current(config: "CutoverConfig") -> list[Finding]:
    """Every swap target must still hash to its backed-up value (no drift)."""
    findings: list[Finding] = []
    if not config.backup_manifest_path.is_file():
        return [Finding("backup", False, "cannot compare live state: backup manifest absent", {})]
    manifest = read_json(config.backup_manifest_path, "backup manifest")
    recorded: dict[str, str] = {}
    for raw in (manifest.get("artifacts") or []):
        if isinstance(raw, Mapping):
            recorded[str(raw.get("rel_path"))] = str(raw.get("sha256"))
    stale = 0
    for swap in config.swaps():
        backup_path = backup_member_path(config.backup_root, swap.identifier)
        if not is_regular_file(backup_path):
            stale += 1
            continue
        expected = recorded.get(swap.identifier) or sha256_file(backup_path)
        if not is_regular_file(swap.live_path):
            stale += 1
            continue
        if sha256_file(swap.live_path) != expected:
            stale += 1
    if stale:
        findings.append(Finding(
            "backup", False,
            f"{stale} activation target(s) drifted since the backup was taken",
            {"stale": stale}))
    else:
        findings.append(Finding("backup", True,
                                "every activation target matches its backed-up value", {}))
    return findings


# ---------------------------------------------------------------------------
# Operator configuration. Every live path is operator supplied: the toolkit
# hardcodes no private location and never writes into the repository.
# ---------------------------------------------------------------------------

MANAGED_SEAT_FILES: tuple[str, ...] = (
    "bin/board.sh",
    "bin/board.py",
    "AGENTS.md",
    ".goosehints",
)
SEAT_WRAPPER = "bin/board.sh"
REQUIRED_SEAT_ENV: tuple[str, ...] = ("PURSERS_BOARDS=registry",)


@dataclass(frozen=True)
class SeatSpec:
    name: str
    host: str
    role: str
    tier: int
    root: Path
    managed: tuple[str, ...] = MANAGED_SEAT_FILES


@dataclass(frozen=True)
class SwapPair:
    identifier: str
    live: Path
    staged: Path


@dataclass(frozen=True)
class CredentialSpec:
    identifier: str
    live: Path
    staged: Path
    required_scopes: frozenset[str] = frozenset()
    forbidden_scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DoorSpec:
    role: str
    live: Path
    staged: Path


@dataclass(frozen=True)
class LauncherSpec:
    live_profile: Path
    staged_profile: Path
    live_script: Path
    staged_script: Path
    venv_python: Path | None = None
    expected_digests: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigFileSpec:
    identifier: str
    live: Path
    staged: Path
    required_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MembershipExpectation:
    board: str
    identifier: str
    principal_id: str
    role: str


@dataclass(frozen=True)
class CutoverConfig:
    """Validated operator configuration for one cutover attempt."""

    path: Path
    repo_root: Path
    staging_root: Path
    live_root: Path
    backup_root: Path
    target_url: str
    previous_url: str
    active_boards: tuple[str, ...]
    launcher: LauncherSpec
    jwks_live: Path
    jwks_staged: Path
    credentials: tuple[CredentialSpec, ...]
    doors: tuple[DoorSpec, ...]
    seats: tuple[SeatSpec, ...]
    config_files: tuple[ConfigFileSpec, ...]
    board_snapshot: Path
    membership_snapshot: Path | None
    memberships: tuple[MembershipExpectation, ...]
    seats_baseline: Path | None
    legacy_markers: tuple[Path, ...]
    extra_backup_paths: tuple[tuple[str, Path], ...]
    wheels_dir: Path | None
    wheel_digests: Mapping[str, str]
    operator_toolkit: Path | None
    team_adapter: Path | None
    helper_bin: str | None
    snapshot_max_age_s: int
    evidence_max_age_s: int

    # -- derived locations -------------------------------------------------
    @property
    def evidence_dir(self) -> Path:
        return self.staging_root / "cutover-evidence"

    @property
    def journal_dir(self) -> Path:
        return self.staging_root / "journal"

    @property
    def journal_path(self) -> Path:
        return self.journal_dir / JOURNAL_NAME

    @property
    def backup_manifest_path(self) -> Path:
        return self.backup_root / "manifest.json"

    @property
    def staged_seat_root(self) -> Path:
        return self.staging_root / "staged-seats"

    def rollback_unit(self) -> tuple[tuple[str, Path], ...]:
        """Identifier -> live path for the single rollback unit."""
        unit: list[tuple[str, Path]] = [
            ("launcher/profile.env", self.launcher.live_profile),
            ("launcher/launch-central.sh", self.launcher.live_script),
            ("jwks/jwks.json", self.jwks_live),
        ]
        for credential in self.credentials:
            unit.append((f"credentials/{credential.identifier}.jwt", credential.live))
        for door in self.doors:
            unit.append((f"doors/{door.role}.door", door.live))
        for spec in self.config_files:
            unit.append((f"configs/{spec.identifier}", spec.live))
        for index, seat in enumerate(self.seats):
            for managed in seat.managed:
                unit.append((f"seats/{index:02d}/{managed}", seat.root / managed))
        unit.extend(self.extra_backup_paths)
        return tuple(unit)

    def swaps(self) -> tuple[FileSwap, ...]:
        """Ordered atomic replacements performed by the toolkit during activation."""
        swaps: list[FileSwap] = []
        swaps.append(FileSwap("swap-launcher", "launcher/profile.env",
                              self.launcher.live_profile, self.launcher.staged_profile))
        swaps.append(FileSwap("swap-launcher", "launcher/launch-central.sh",
                              self.launcher.live_script, self.launcher.staged_script))
        swaps.append(FileSwap("swap-jwks", "jwks/jwks.json",
                              self.jwks_live, self.jwks_staged))
        for credential in self.credentials:
            swaps.append(FileSwap(
                "swap-credentials", f"credentials/{credential.identifier}.jwt",
                credential.live, credential.staged))
        for door in self.doors:
            swaps.append(FileSwap("swap-credentials", f"doors/{door.role}.door",
                                  door.live, door.staged))
        for spec in self.config_files:
            swaps.append(FileSwap("swap-configs", f"configs/{spec.identifier}",
                                  spec.live, spec.staged))
        for index, seat in enumerate(self.seats):
            for managed in seat.managed:
                swaps.append(FileSwap(
                    "swap-seats", f"seats/{index:02d}/{managed}",
                    seat.root / managed,
                    self.staged_seat_root / f"{index:02d}" / managed))
        return tuple(swaps)

    def swaps_for_step(self, step_id: str) -> tuple[FileSwap, ...]:
        return tuple(swap for swap in self.swaps() if swap.step_id == step_id)

    def sanitizer(self) -> Sanitizer:
        return default_sanitizer(self)


def _require(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in mapping or mapping[key] in (None, "", [], {}):
        raise ConfigError(f"{label}: missing required key '{key}'")
    return mapping[key]


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}: path must be a non-empty string")
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        raise ConfigError(f"{label}: path must be absolute (no relative traversal)")
    return candidate.resolve(strict=False)


def _relative_identifier(value: Any, label: str) -> str:
    """Return one normalized safe backup identifier, or refuse before writes."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}: identifier must be a non-empty relative path")
    text = value.strip()
    if "\\" in text:
        raise ConfigError(f"{label}: identifier must use safe POSIX path segments")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ConfigError(f"{label}: identifier must be relative without traversal")
    normalized = path.as_posix()
    if normalized.startswith("/"):
        raise ConfigError(f"{label}: identifier must not be absolute")
    return normalized


def _optional_path(mapping: Mapping[str, Any], key: str, label: str) -> Path | None:
    value = mapping.get(key)
    if value in (None, ""):
        return None
    return _path(value, f"{label}.{key}")


def _swap_pair(mapping: Mapping[str, Any], identifier: str, label: str) -> SwapPair:
    return SwapPair(identifier,
                    _path(_require(mapping, "live", f"{label}.live"), f"{label}.live"),
                    _path(_require(mapping, "staged", f"{label}.staged"), f"{label}.staged"))


def config_template() -> dict[str, Any]:
    """A placeholder template for the operator-private configuration file."""
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_root": "/PATH/TO/pursers-source",
        "staging_root": "/PATH/TO/staging",
        "live_root": "/PATH/TO/live",
        "backup_root": "/PATH/TO/staging/backup",
        "target_url": TARGET_URL,
        "previous_url": "https://127.0.0.1:8766/mcp",
        "active_boards": list(DEFAULT_ACTIVE_BOARDS),
        "launcher": {
            "live_profile": "/PATH/TO/live/profile.env",
            "staged_profile": "/PATH/TO/staging/staged-configs/profile.env",
            "live_script": "/PATH/TO/live/launch-central.sh",
            "staged_script": "/PATH/TO/staging/staged-configs/launch-central.sh",
            "venv_python": "/PATH/TO/instance/.venv/bin/python",
            "expected_digests": {},
        },
        "jwks": {
            "live": "/PATH/TO/live/jwks.json",
            "staged": "/PATH/TO/staging/target-jwt/jwks.json",
        },
        "credentials": [
            {"identifier": name,
             "live": f"/PATH/TO/live/{name}.jwt",
             "staged": f"/PATH/TO/staging/target-jwt/{name}.jwt"}
            for name in REQUIRED_NAMED_CREDENTIALS
        ],
        "doors": [
            {"role": role,
             "live": f"/PATH/TO/live/{role}.door",
             "staged": f"/PATH/TO/staging/target-jwt/{role}.door"}
            for role in DOOR_ROLES
        ],
        "seats": [
            {"name": "<SEAT_NAME>", "host": "<HOST>", "role": "worker", "tier": 1,
             "root": "/PATH/TO/seats/<SEAT_NAME>"}
            for _ in range(EXPECTED_SEAT_COUNT)
        ],
        "seats_baseline": "/PATH/TO/staging/cutover-evidence/seats-baseline.json",
        "configs": [
            {"identifier": "coordinator.env",
             "live": "/PATH/TO/live/coordinator.env",
             "staged": "/PATH/TO/staging/staged-configs/coordinator.env",
             "required_substrings": [TARGET_URL],
             "forbidden_substrings": list(FORBIDDEN_CA_ENV)},
        ],
        "board_snapshot": "/PATH/TO/staging/cutover-evidence/board-snapshot.json",
        "membership_snapshot": "/PATH/TO/staging/cutover-evidence/membership-snapshot.json",
        "memberships": [],
        "legacy_markers": [],
        "extra_backup_paths": [],
        "wheels_dir": "/PATH/TO/release-a25",
        "wheel_digests": dict(A25_WHEEL_DIGESTS),
        "operator_toolkit": "/PATH/TO/operator-private",
        "team_adapter": "/PATH/TO/pursers-source/tools/aionui-extension/team/adapter.cjs",
        "helper_bin": None,
        "snapshot_max_age_s": DEFAULT_SNAPSHOT_MAX_AGE_S,
        "evidence_max_age_s": DEFAULT_EVIDENCE_MAX_AGE_S,
    }


def load_config(path: str | Path, repo_root: str | Path | None = None) -> CutoverConfig:
    """Load and strictly validate the operator configuration."""
    config_path = Path(path).expanduser().resolve(strict=False)
    raw = read_json(config_path, "cutover config")
    if not isinstance(raw, Mapping):
        raise ConfigError("cutover config: top level must be an object")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"cutover config: unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")

    resolved_repo = Path(repo_root).resolve() if repo_root else Path(
        str(raw.get("repo_root") or Path(__file__).resolve().parents[1])).resolve()
    staging_root = _path(_require(raw, "staging_root", "config"), "config.staging_root")
    live_root = _path(_require(raw, "live_root", "config"), "config.live_root")
    backup_root = _optional_path(raw, "backup_root", "config") or (staging_root / "backup")

    target_url = str(raw.get("target_url") or TARGET_URL)
    require_target_url(target_url, "config.target_url")
    previous_url = str(_require(raw, "previous_url", "config"))
    parse_loopback_url(previous_url, "config.previous_url")
    if previous_url.strip() == target_url:
        raise ConfigError("config.previous_url must differ from the target url")

    boards = raw.get("active_boards") or list(DEFAULT_ACTIVE_BOARDS)
    if not isinstance(boards, (list, tuple)) or not boards:
        raise ConfigError("config.active_boards must be a non-empty list")

    launcher_raw = _require(raw, "launcher", "config")
    if not isinstance(launcher_raw, Mapping):
        raise ConfigError("config.launcher must be an object")
    launcher = LauncherSpec(
        live_profile=_path(_require(launcher_raw, "live_profile", "launcher"),
                           "launcher.live_profile"),
        staged_profile=_path(_require(launcher_raw, "staged_profile", "launcher"),
                             "launcher.staged_profile"),
        live_script=_path(_require(launcher_raw, "live_script", "launcher"),
                          "launcher.live_script"),
        staged_script=_path(_require(launcher_raw, "staged_script", "launcher"),
                            "launcher.staged_script"),
        venv_python=_optional_path(launcher_raw, "venv_python", "launcher"),
        expected_digests=dict(launcher_raw.get("expected_digests") or {}),
    )

    jwks_raw = _require(raw, "jwks", "config")
    if not isinstance(jwks_raw, Mapping):
        raise ConfigError("config.jwks must be an object")
    jwks_live = _path(_require(jwks_raw, "live", "jwks"), "jwks.live")
    jwks_staged = _path(_require(jwks_raw, "staged", "jwks"), "jwks.staged")

    credentials: list[CredentialSpec] = []
    for entry in _require(raw, "credentials", "config"):
        if not isinstance(entry, Mapping):
            raise ConfigError("config.credentials entries must be objects")
        identifier = _relative_identifier(
            _require(entry, "identifier", "credential"), "credential.identifier")
        pair = _swap_pair(entry, identifier, f"credential {identifier}")
        credentials.append(CredentialSpec(
            identifier, pair.live, pair.staged,
            REQUIRED_NAMED_CREDENTIALS.get(identifier, frozenset()),
            FORBIDDEN_CREDENTIAL_SCOPES.get(identifier, frozenset())))
    declared = {item.identifier for item in credentials}
    missing_named = sorted(set(REQUIRED_NAMED_CREDENTIALS) - declared)
    if missing_named:
        raise ConfigError(
            "config.credentials must declare every named control-plane credential: "
            + ", ".join(missing_named))

    doors: list[DoorSpec] = []
    for entry in _require(raw, "doors", "config"):
        if not isinstance(entry, Mapping):
            raise ConfigError("config.doors entries must be objects")
        role = str(_require(entry, "role", "door"))
        if role not in DOOR_ROLES:
            raise ConfigError(f"door role must be one of {DOOR_ROLES}, got {role!r}")
        pair = _swap_pair(entry, role, f"door {role}")
        doors.append(DoorSpec(role, pair.live, pair.staged))
    if {door.role for door in doors} != set(DOOR_ROLES):
        raise ConfigError("config.doors must declare exactly the worker and reviewer doors")

    seats: list[SeatSpec] = []
    for entry in _require(raw, "seats", "config"):
        if not isinstance(entry, Mapping):
            raise ConfigError("config.seats entries must be objects")
        name = str(_require(entry, "name", "seat"))
        role = str(_require(entry, "role", f"seat {name}"))
        if role not in SEAT_ROLES:
            raise ConfigError(f"seat {name}: role must be one of {SEAT_ROLES}")
        tier = entry.get("tier")
        if not isinstance(tier, int) or not 1 <= tier <= MAX_SEAT_TIER:
            raise ConfigError(f"seat {name}: tier must be an integer 1..{MAX_SEAT_TIER}")
        managed = tuple(
            _relative_identifier(item, f"seat {name}.managed")
            for item in (entry.get("managed") or MANAGED_SEAT_FILES))
        seats.append(SeatSpec(name, str(entry.get("host") or ""), role, tier,
                              _path(_require(entry, "root", f"seat {name}"),
                                    f"seat {name}.root"), managed))

    config_files: list[ConfigFileSpec] = []
    for entry in (raw.get("configs") or []):
        if not isinstance(entry, Mapping):
            raise ConfigError("config.configs entries must be objects")
        identifier = _relative_identifier(
            _require(entry, "identifier", "config file"), "config file.identifier")
        pair = _swap_pair(entry, identifier, f"config {identifier}")
        config_files.append(ConfigFileSpec(
            identifier, pair.live, pair.staged,
            tuple(str(item) for item in (entry.get("required_substrings") or ())),
            tuple(str(item) for item in (entry.get("forbidden_substrings") or ()))))

    memberships: list[MembershipExpectation] = []
    for entry in (raw.get("memberships") or []):
        if not isinstance(entry, Mapping):
            raise ConfigError("config.memberships entries must be objects")
        memberships.append(MembershipExpectation(
            str(_require(entry, "board", "membership")),
            str(_require(entry, "identifier", "membership")),
            str(_require(entry, "principal_id", "membership")),
            str(_require(entry, "role", "membership"))))

    config = CutoverConfig(
        path=config_path,
        repo_root=resolved_repo,
        staging_root=staging_root,
        live_root=live_root,
        backup_root=backup_root,
        target_url=target_url,
        previous_url=previous_url,
        active_boards=tuple(str(item) for item in boards),
        launcher=launcher,
        jwks_live=jwks_live,
        jwks_staged=jwks_staged,
        credentials=tuple(credentials),
        doors=tuple(doors),
        seats=tuple(seats),
        config_files=tuple(config_files),
        board_snapshot=_path(_require(raw, "board_snapshot", "config"), "config.board_snapshot"),
        membership_snapshot=_optional_path(raw, "membership_snapshot", "config"),
        memberships=tuple(memberships),
        seats_baseline=_optional_path(raw, "seats_baseline", "config"),
        legacy_markers=tuple(_path(item, "legacy_markers") for item in (raw.get("legacy_markers") or ())),
        extra_backup_paths=tuple(
            (_relative_identifier(item.get("identifier"), "extra_backup_paths.identifier"),
             _path(item.get("path"), "extra_backup_paths"))
            for item in (raw.get("extra_backup_paths") or [])
            if isinstance(item, Mapping) and item.get("identifier") and item.get("path")),
        wheels_dir=_optional_path(raw, "wheels_dir", "config"),
        wheel_digests=dict(raw.get("wheel_digests") or A25_WHEEL_DIGESTS),
        operator_toolkit=_optional_path(raw, "operator_toolkit", "config"),
        team_adapter=_optional_path(raw, "team_adapter", "config"),
        helper_bin=(str(raw["helper_bin"]) if raw.get("helper_bin") else None),
        snapshot_max_age_s=int(raw.get("snapshot_max_age_s") or DEFAULT_SNAPSHOT_MAX_AGE_S),
        evidence_max_age_s=int(raw.get("evidence_max_age_s") or DEFAULT_EVIDENCE_MAX_AGE_S),
    )
    validate_layout(config)
    return config


def validate_layout(config: CutoverConfig) -> None:
    """Fail closed unless every read/write path is contained by its declared root."""
    problems: list[str] = []
    staging = config.staging_root.resolve(strict=False)
    live_root = config.live_root.resolve(strict=False)
    repository = config.repo_root.resolve(strict=False)
    backup = config.backup_root.resolve(strict=False)

    def require_child(label: str, path: Path, root: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            problems.append(f"{label} must be beneath {root.name or root}")
            return
        if resolved == root:
            problems.append(f"{label} must be a descendant of {root.name or root}")

    if not staging.is_absolute():
        problems.append("staging_root must be absolute")
    for label, root in (("repository", repository), ("live", live_root)):
        try:
            staging.relative_to(root)
            problems.append(f"staging_root must not live under the {label} root")
        except ValueError:
            pass
        try:
            root.relative_to(staging)
            problems.append(f"{label} root must not live under staging_root")
        except ValueError:
            pass
    if staging == backup:
        problems.append("backup_root must be a distinct directory inside staging_root")
    else:
        try:
            backup.relative_to(staging)
        except ValueError:
            problems.append("backup_root must be a descendant of staging_root")
    try:
        repository.relative_to(staging)
        problems.append("repository root must not live under staging_root")
    except ValueError:
        pass

    require_child("operator config", config.path, staging)
    require_child("board snapshot", config.board_snapshot, staging)
    if config.membership_snapshot is not None:
        require_child("membership snapshot", config.membership_snapshot, staging)
    if config.seats_baseline is not None:
        require_child("seat baseline", config.seats_baseline, staging)

    canonical_seat_roots: dict[Path, list[str]] = {}
    for seat in config.seats:
        canonical_seat_roots.setdefault(
            seat.root.resolve(strict=False), []).append(seat.name)
    for names in canonical_seat_roots.values():
        if len(names) > 1:
            problems.append(
                "seat roots must be distinct canonical directories: "
                + ", ".join(sorted(names)))

    canonical_live_targets: dict[Path, list[str]] = {}
    for swap in config.swaps():
        require_child(f"staged swap source {swap.identifier}", swap.staged_path, staging)
        require_child(f"live mutation target {swap.identifier}", swap.live_path, live_root)
        resolved_live = swap.live_path.resolve(strict=False)
        canonical_live_targets.setdefault(resolved_live, []).append(swap.identifier)
        try:
            resolved_live.relative_to(repository)
            problems.append(f"live mutation target {swap.identifier} is inside the repository")
        except ValueError:
            pass
        try:
            resolved_live.relative_to(staging)
            problems.append(f"live mutation target {swap.identifier} is inside staging_root")
        except ValueError:
            pass
    for identifiers in canonical_live_targets.values():
        if len(identifiers) > 1:
            problems.append(
                "live mutation targets must be unique after canonicalization: "
                + ", ".join(sorted(identifiers)))
    rollback = config.rollback_unit()
    identifiers: list[str] = []
    for identifier, live in rollback:
        try:
            identifiers.append(_relative_identifier(identifier, "rollback unit"))
        except ConfigError as exc:
            problems.append(str(exc))
        try:
            Path(live).relative_to(config.repo_root)
            problems.append(f"rollback unit member {identifier} is inside the repository")
        except ValueError:
            pass
    for index, identifier in enumerate(sorted(identifiers)):
        for other in sorted(identifiers)[index + 1:]:
            if identifier == other or other.startswith(identifier + "/"):
                problems.append(
                    f"rollback identifiers collide: {identifier!r} and {other!r}")
    if backup.exists():
        for identifier in identifiers:
            cursor = backup
            for part in PurePosixPath(identifier).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    problems.append(f"rollback identifier traverses a symlink: {identifier!r}")
                    break
    if problems:
        raise ConfigError("staging layout refused: " + "; ".join(problems))


def staging_output_path(config: CutoverConfig, path: Path, label: str) -> Path:
    """Canonicalize one generated output and keep it inside the private staging root."""
    resolved = path.expanduser().resolve(strict=False)
    staging = config.staging_root.resolve(strict=False)
    try:
        resolved.relative_to(staging)
    except ValueError as exc:
        raise ConfigError(f"{label} must be beneath staging_root") from exc
    if resolved == staging:
        raise ConfigError(f"{label} must be a descendant of staging_root")
    return resolved


# ---------------------------------------------------------------------------
# Activation journal: the only record of what was replaced, and how far.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    step_id: str
    state: str  # started | done | rolled_back
    at: str
    targets: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "state": self.state, "at": self.at,
                "targets": [dict(target) for target in self.targets]}


def journal_load(path: Path) -> list[JournalEntry]:
    if not path.is_file():
        return []
    raw = read_json(path, "activation journal")
    if not isinstance(raw, Mapping):
        raise ConfigError("activation journal: top level must be an object")
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ConfigError(
            "activation journal: unsupported schema_version "
            f"{version!r} (expected {SCHEMA_VERSION})"
        )
    toolkit = raw.get("toolkit")
    if toolkit != TOOLKIT_IDENTIFIER:
        raise ConfigError(
            "activation journal: unsupported toolkit "
            f"{toolkit!r} (expected {TOOLKIT_IDENTIFIER!r})"
        )
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigError("activation journal: entries must be a list")
    entries: list[JournalEntry] = []
    for index, item in enumerate(raw_entries):
        if not isinstance(item, Mapping):
            raise ConfigError(
                f"activation journal: entry {index} must be an object"
            )
        fields: dict[str, str] = {}
        for field in ("step_id", "state", "at"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    f"activation journal: entry {index}.{field} "
                    "must be a non-empty string"
                )
            fields[field] = value
        raw_targets = item.get("targets")
        if not isinstance(raw_targets, list):
            raise ConfigError(
                f"activation journal: entry {index}.targets must be a list"
            )
        targets: list[Mapping[str, Any]] = []
        for target_index, target in enumerate(raw_targets):
            if not isinstance(target, Mapping):
                raise ConfigError(
                    "activation journal: entry "
                    f"{index}.targets[{target_index}] must be an object"
                )
            targets.append(dict(target))
        entries.append(JournalEntry(
            fields["step_id"], fields["state"], fields["at"], tuple(targets)))
    return entries


def journal_write(path: Path, entries: Sequence[JournalEntry], meta: Mapping[str, Any] | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "updated_at": iso_now(),
        "meta": dict(meta or {}),
        "entries": [entry.as_dict() for entry in entries],
    }
    ensure_private_dir(path.parent)
    write_json_atomic(path, payload)


def journal_append(path: Path, entry: JournalEntry) -> list[JournalEntry]:
    entries = journal_load(path)
    entries.append(entry)
    journal_write(path, entries)
    return entries


def interrupted_steps(entries: Sequence[JournalEntry]) -> list[str]:
    """Steps marked started without a terminal state: an interrupted activation."""
    terminal: dict[str, str] = {}
    for entry in entries:
        terminal[entry.step_id] = entry.state
    return sorted(step for step, state in terminal.items() if state == "started")


def steps_needing_rollback(entries: Sequence[JournalEntry]) -> list[str]:
    """Reverse-order list of steps whose replacements must be undone."""
    order: list[str] = []
    terminal: dict[str, str] = {}
    for entry in entries:
        if entry.step_id not in order:
            order.append(entry.step_id)
        terminal[entry.step_id] = entry.state
    return [
        step_id for step_id in reversed(order)
        if terminal.get(step_id) in ("started", "done")
    ]


def validate_rollback_journal(
    config: "CutoverConfig",
    entries: Sequence[JournalEntry],
    steps: Sequence[str],
) -> dict[str, list[Mapping[str, Any]]]:
    """Require complete, canonical rollback metadata before the first write."""
    problems: list[str] = []
    configured_order = list(TOOLKIT_ACTIVATION_STEPS)
    expected_steps = set(TOOLKIT_ACTIVATION_STEPS)
    allowed_states = {"started", "done", "rolled_back"}
    present_order: list[str] = []
    states_by_step: dict[str, list[str]] = {}
    entries_by_step: dict[str, list[JournalEntry]] = {}

    for entry in entries:
        step_id = entry.step_id
        if step_id not in expected_steps:
            problems.append(f"journal step {step_id!r} is not a configured activation step")
            continue
        if entry.state not in allowed_states:
            problems.append(
                f"journal step {step_id!r} has invalid state {entry.state!r}"
            )
            continue
        if step_id not in states_by_step:
            present_order.append(step_id)
            states_by_step[step_id] = []
            entries_by_step[step_id] = []
        states_by_step[step_id].append(entry.state)
        entries_by_step[step_id].append(entry)

    expected_prefix = configured_order[:len(present_order)]
    if present_order != expected_prefix:
        problems.append(
            "journal activation steps are missing or reordered: "
            f"expected prefix {expected_prefix}, observed {present_order}"
        )

    valid_progressions = {
        ("started",),
        ("started", "done"),
        ("started", "rolled_back"),
        ("started", "done", "rolled_back"),
    }
    for step_id in present_order:
        progression = tuple(states_by_step[step_id])
        if progression not in valid_progressions:
            problems.append(
                f"journal step {step_id!r} has invalid state progression {list(progression)}"
            )
        baseline_targets = entries_by_step[step_id][0].targets
        if any(entry.targets != baseline_targets for entry in entries_by_step[step_id][1:]):
            problems.append(
                f"journal step {step_id!r} changes target metadata across states"
            )

    active_sequence = [
        (entry.step_id, entry.state)
        for entry in entries
        if entry.step_id in expected_steps and entry.state in ("started", "done")
    ]
    expected_sequence = [
        (step_id, state)
        for step_id in present_order
        for state in states_by_step[step_id]
        if state in ("started", "done")
    ]
    if active_sequence != expected_sequence:
        problems.append("journal activation entries are interleaved or reordered")
    for step_id in present_order[:-1]:
        activation_states = [
            state for state in states_by_step[step_id]
            if state in ("started", "done")
        ]
        if not activation_states or activation_states[-1] != "done":
            problems.append(
                f"journal advances past incomplete activation step {step_id!r}"
            )

    active_steps = [
        step_id for step_id in present_order
        if states_by_step[step_id][-1] in ("started", "done")
    ]
    expected_rollback_steps = list(reversed(active_steps))
    if list(steps) != expected_rollback_steps:
        problems.append(
            "rollback step selection does not match journal progression: "
            f"expected {expected_rollback_steps}, observed {list(steps)}"
        )

    for step_id in configured_order:
        terminal = states_by_step.get(step_id, [None])[-1]
        if terminal not in (None, "rolled_back"):
            continue
        state_label = "absent" if terminal is None else "rolled-back"
        for swap in config.swaps_for_step(step_id):
            backup = backup_member_path(config.backup_root, swap.identifier)
            if (
                not is_regular_file(backup)
                or not is_regular_file(swap.live_path)
                or sha256_file(swap.live_path) != sha256_file(backup)
            ):
                problems.append(
                    f"{state_label} activation step {step_id!r} live target "
                    f"{swap.identifier!r} differs from the verified backup"
                )

    for step_id in steps:
        if step_id not in expected_steps:
            problems.append(f"journal step {step_id!r} is not a configured activation step")

    for entry in entries:
        if entry.state not in allowed_states or entry.step_id not in expected_steps:
            continue
        step_id = entry.step_id
        expected = {swap.identifier: swap for swap in config.swaps_for_step(step_id)}
        targets = list(entry.targets)
        identifiers = [
            target.get("identifier")
            if isinstance(target.get("identifier"), str)
            else None
            for target in targets
        ]
        valid_identifiers = [identifier for identifier in identifiers if identifier]
        duplicates = sorted({
            identifier for identifier in valid_identifiers
            if valid_identifiers.count(identifier) > 1
        })
        missing = sorted(set(expected) - set(valid_identifiers))
        extra = sorted(set(valid_identifiers) - set(expected))
        malformed_count = len(identifiers) - len(valid_identifiers)
        if duplicates:
            problems.append(
                f"journal {step_id}/{entry.state} has duplicate target identifiers: "
                + ", ".join(duplicates)
            )
        if missing:
            problems.append(
                f"journal {step_id}/{entry.state} is missing configured targets: "
                + ", ".join(missing)
            )
        if extra:
            problems.append(
                f"journal {step_id}/{entry.state} has unconfigured targets: "
                + ", ".join(extra)
            )
        if malformed_count:
            problems.append(
                f"journal {step_id}/{entry.state} has {malformed_count} malformed target(s)"
            )
        if len(targets) != len(expected):
            problems.append(
                f"journal {step_id}/{entry.state} target count differs from configuration"
            )

        for target in targets:
            identifier = target.get("identifier")
            swap = expected.get(identifier) if isinstance(identifier, str) else None
            if swap is None:
                continue
            live = target.get("live")
            staged = target.get("staged")
            backup_ref = target.get("backup_ref")
            pre_sha = target.get("pre_sha256")
            staged_sha = target.get("staged_sha256")
            mode = target.get("mode")
            if not isinstance(live, str) or not live:
                problems.append(f"journal target {step_id}/{identifier} has no live path")
            else:
                try:
                    live_matches = (
                        Path(live).resolve(strict=False)
                        == swap.live_path.resolve(strict=False)
                    )
                except (OSError, RuntimeError, ValueError):
                    live_matches = False
                if not live_matches:
                    problems.append(
                        f"journal live path for {step_id}/{identifier} differs from "
                        "the configured target"
                    )
            if not isinstance(staged, str) or not staged:
                problems.append(f"journal target {step_id}/{identifier} has no staged path")
            else:
                try:
                    staged_matches = (
                        Path(staged).resolve(strict=False)
                        == swap.staged_path.resolve(strict=False)
                    )
                except (OSError, RuntimeError, ValueError):
                    staged_matches = False
                if not staged_matches:
                    problems.append(
                        f"journal staged path for {step_id}/{identifier} differs "
                        "from configuration"
                    )
            if backup_ref != identifier:
                problems.append(
                    f"journal backup reference for {step_id}/{identifier} is missing or mismatched"
                )
            if not isinstance(pre_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", pre_sha):
                problems.append(
                    f"journal pre-activation hash for {step_id}/{identifier} is missing or malformed"
                )
            else:
                backup = backup_member_path(config.backup_root, identifier)
                if not is_regular_file(backup) or sha256_file(backup) != pre_sha:
                    problems.append(
                        f"journal pre-activation hash for {step_id}/{identifier} "
                        "does not match the backup"
                    )
            if not isinstance(staged_sha, str) or not re.fullmatch(
                r"[0-9a-f]{64}", staged_sha
            ):
                problems.append(
                    f"journal staged hash for {step_id}/{identifier} is missing or malformed"
                )
            if isinstance(mode, bool) or not isinstance(mode, int) or not 0 < mode <= 0o777:
                problems.append(
                    f"journal mode for {step_id}/{identifier} is missing or malformed"
                )
    recorded = {
        step_id: list(entries_by_step[step_id][0].targets)
        for step_id in steps
        if step_id in entries_by_step
    }

    missing_entries = sorted(set(steps) - set(recorded))
    if missing_entries:
        problems.append(
            "journal has no usable target entry for step(s): " + ", ".join(missing_entries)
        )
    if problems:
        raise GateFailure("rollback", problems)
    return recorded


# ---------------------------------------------------------------------------
# Gates. Every gate is fail closed: absent evidence is a refusal, never a pass.
# ---------------------------------------------------------------------------


def _private_file_findings(gate: str, label: str, path: Path,
                           require_mode: int = PRIVATE_FILE_MODE) -> list[Finding]:
    findings: list[Finding] = []
    if not path.exists():
        return [Finding(gate, False, f"{label}: staged file is absent", {"path": str(path)})]
    if not is_regular_file(path):
        return [Finding(gate, False, f"{label}: staged path is not a regular file",
                        {"path": str(path)})]
    mode = file_mode(path)
    if require_mode and mode != require_mode:
        findings.append(Finding(
            gate, False,
            f"{label}: mode must be {oct(require_mode)}, found {oct(mode)}",
            {"path": str(path)}))
    parent_mode = file_mode(path.parent)
    if parent_mode & 0o077:
        findings.append(Finding(
            gate, False,
            f"{label}: containing directory is group/world accessible ({oct(parent_mode)})",
            {"path": str(path.parent)}))
    return findings


def gate_staging(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    root = config.staging_root
    if not root.is_dir():
        return [Finding("staging", False, "staging root does not exist; run prepare first",
                        {"path": str(root)})]
    mode = file_mode(root)
    if mode != PRIVATE_DIR_MODE:
        findings.append(Finding("staging", False,
                                f"staging root mode must be {oct(PRIVATE_DIR_MODE)}, found {oct(mode)}",
                                {"path": str(root)}))
    for relative in STAGING_SUBDIRS:
        sub = root / relative
        if not sub.is_dir():
            findings.append(Finding("staging", False,
                                    f"staging subdirectory is missing: {relative}", {}))
            continue
        sub_mode = file_mode(sub)
        if sub_mode != PRIVATE_DIR_MODE:
            findings.append(Finding("staging", False,
                                    f"staging subdirectory {relative} mode must be "
                                    f"{oct(PRIVATE_DIR_MODE)}, found {oct(sub_mode)}", {}))
    if config.path.is_file():
        config_mode = file_mode(config.path)
        if config_mode != PRIVATE_FILE_MODE:
            findings.append(Finding("staging", False,
                                    f"cutover config mode must be {oct(PRIVATE_FILE_MODE)}, "
                                    f"found {oct(config_mode)}", {"path": str(config.path)}))
    inside_repo = False
    try:
        root.relative_to(config.repo_root)
        inside_repo = True
    except ValueError:
        pass
    if inside_repo:
        findings.append(Finding("staging", False,
                                "staging root must be a new directory outside the repository",
                                {"path": str(root)}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("staging", True,
                                "staging root is private, complete and outside the repository",
                                {"subdirectories": len(STAGING_SUBDIRS)}))
    return findings


def gate_url(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    try:
        parsed = require_target_url(config.target_url, "config.target_url")
        findings.append(Finding("url_binding", True,
                                "target resource is the exact released loopback URL",
                                {"target_url": parsed.normalized, "issuer": parsed.issuer}))
    except GateFailure as exc:
        findings.extend(Finding("url_binding", False, reason, {}) for reason in exc.reasons)
    try:
        previous = parse_loopback_url(config.previous_url, "config.previous_url")
        if previous.normalized == config.target_url:
            findings.append(Finding("url_binding", False,
                                    "previous and target resources must differ", {}))
        else:
            findings.append(Finding("url_binding", True,
                                    "previous resource recorded for rollback coherence",
                                    {"previous_url": previous.normalized}))
    except GateFailure as exc:
        findings.extend(Finding("url_binding", False, reason, {}) for reason in exc.reasons)
    if evidence_url_acceptable(config.target_url, TARGET_URL):
        findings.append(Finding("url_binding", True,
                                "configured target equals the audited O1 resource", {}))
    else:
        findings.append(Finding("url_binding", False,
                                "configured target differs from the audited O1 resource", {}))
    return findings


def gate_artifacts(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    wheels_dir = config.wheels_dir
    if wheels_dir is None:
        findings.append(Finding("artifacts", False,
                                "wheel directory is not declared; a25 digests cannot be verified", {}))
    elif not wheels_dir.is_dir():
        findings.append(Finding("artifacts", False,
                                "declared wheel directory is absent", {"path": str(wheels_dir)}))
    else:
        mismatched = 0
        verified = 0
        for filename, expected in sorted(config.wheel_digests.items()):
            wheel = wheels_dir / filename
            if not is_regular_file(wheel):
                mismatched += 1
                continue
            if sha256_file(wheel) != expected:
                mismatched += 1
                continue
            verified += 1
        if mismatched:
            findings.append(Finding("artifacts", False,
                                    f"{mismatched} pinned a25 wheel(s) missing or digest mismatched",
                                    {"mismatched": mismatched, "verified": verified}))
        else:
            findings.append(Finding("artifacts", True,
                                    f"all {verified} pinned a25 wheel digests verified",
                                    {"verified": verified}))

    for label, staged, live in (
            ("central profile", config.launcher.staged_profile, config.launcher.live_profile),
            ("central launcher", config.launcher.staged_script, config.launcher.live_script)):
        findings.extend(_private_file_findings("artifacts", label, staged, require_mode=0))
        if not live.exists():
            findings.append(Finding("artifacts", False,
                                    f"{label}: live target is absent", {"path": str(live)}))
    if is_regular_file(config.launcher.staged_script):
        text = config.launcher.staged_script.read_text(encoding="utf-8", errors="replace")
        if "pursers_central.pursers_central_runtime" not in text:
            findings.append(Finding("artifacts", False,
                                    "staged launcher does not invoke the released runtime module", {}))
        if "serve_tls" in text:
            findings.append(Finding("artifacts", False,
                                    "staged launcher still references the predecessor TLS shim", {}))
        if f"--port {EXPECTED_PORT}" not in text or f"--host {LOOPBACK_HOST}" not in text:
            findings.append(Finding("artifacts", False,
                                    "staged launcher does not bind the exact loopback host and port", {}))
        for command in FORBIDDEN_LIFECYCLE_COMMANDS:
            if command in text:
                findings.append(Finding("artifacts", False,
                                        f"staged launcher references the disallowed '{command}' command", {}))
    if is_regular_file(config.launcher.staged_profile):
        profile = config.launcher.staged_profile.read_text(encoding="utf-8", errors="replace")
        for variable in ("CENTRAL_JWT_ISSUER", "CENTRAL_JWT_AUDIENCE", "CENTRAL_JWKS_PATH"):
            if variable not in profile:
                findings.append(Finding("artifacts", False,
                                        f"staged profile does not set {variable}", {}))
        for variable in FORBIDDEN_CA_ENV:
            if variable in profile:
                findings.append(Finding("artifacts", False,
                                        f"staged profile sets a CA override ({variable})", {}))
        if f"CENTRAL_JWT_AUDIENCE={TARGET_AUDIENCE}" in profile:
            findings.append(Finding("artifacts", True,
                                    "staged profile audience equals the target resource", {}))
    venv_python = config.launcher.venv_python
    if venv_python is not None:
        if not is_regular_file(venv_python):
            findings.append(Finding("artifacts", False,
                                    "declared target venv interpreter is absent",
                                    {"path": str(venv_python)}))
        elif not os.access(venv_python, os.X_OK):
            findings.append(Finding("artifacts", False,
                                    "declared target venv interpreter is not executable",
                                    {"path": str(venv_python)}))
    for identifier, expected in sorted(config.launcher.expected_digests.items()):
        staged = {
            "profile": config.launcher.staged_profile,
            "script": config.launcher.staged_script,
            "venv_python": config.launcher.venv_python,
        }.get(identifier)
        if staged is None:
            findings.append(Finding("artifacts", False,
                                    f"launcher hash preflight declares an unknown target: {identifier}", {}))
            continue
        if not is_regular_file(staged):
            findings.append(Finding("artifacts", False,
                                    f"launcher hash preflight target is absent: {identifier}", {}))
            continue
        actual = sha256_file(staged)
        if actual != expected:
            findings.append(Finding("artifacts", False,
                                    f"launcher hash preflight mismatch for {identifier}",
                                    {"identifier": identifier}))
        else:
            findings.append(Finding("artifacts", True,
                                    f"launcher hash preflight verified {identifier}",
                                    {"identifier": identifier, "sha256": actual}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("artifacts", True, "release and launcher preflight complete", {}))
    return findings


def gate_backup(config: CutoverConfig) -> list[Finding]:
    findings = verify_backup_integrity(config)
    findings.extend(backup_is_current(config))
    return findings


def gate_drain(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    if not config.board_snapshot.is_file():
        return [Finding("drain", False,
                        "board snapshot is absent; activation cannot prove a drained fleet",
                        {"path": str(config.board_snapshot)})]
    snapshot = read_json(config.board_snapshot, "board snapshot")
    if not isinstance(snapshot, Mapping):
        return [Finding("drain", False, "board snapshot must be an object", {})]
    age = snapshot_age_seconds(snapshot.get("recorded_at"))
    if age is None:
        findings.append(Finding("drain", False,
                                "board snapshot has no parsable recorded_at timestamp", {}))
    elif age > config.snapshot_max_age_s:
        findings.append(Finding("drain", False,
                                f"board snapshot is stale ({int(age)}s older than the "
                                f"{config.snapshot_max_age_s}s limit)", {"age_s": int(age)}))
    if snapshot.get("dispatch_paused") is not True:
        findings.append(Finding("drain", False, "dispatch is not recorded as paused", {}))
    if snapshot.get("teams_paused") is not True:
        findings.append(Finding("drain", False, "AionUI Teams are not recorded as paused", {}))
    boards = snapshot.get("boards")
    if not isinstance(boards, Mapping):
        findings.append(Finding("drain", False, "board snapshot declares no boards mapping", {}))
        boards = {}
    for board in config.active_boards:
        counts = boards.get(board)
        if not isinstance(counts, Mapping):
            findings.append(Finding("drain", False,
                                    f"active board is missing from the snapshot: {board}",
                                    {"board": board}))
            continue
        held = 0
        for state in HELD_TICKET_STATES:
            value = counts.get(state, 0)
            if not isinstance(value, int) or value < 0:
                findings.append(Finding("drain", False,
                                        f"board {board}: state '{state}' is not a non-negative integer",
                                        {"board": board, "state": state}))
                continue
            held += value
        if held:
            findings.append(Finding("drain", False,
                                    f"board {board} still holds {held} ticket(s) in a claim/review state",
                                    {"board": board, "held": held}))
        else:
            findings.append(Finding("drain", True,
                                    f"board {board} reports zero held tickets", {"board": board}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("drain", True,
                                "fleet is drained, dispatch paused and Teams paused",
                                {"boards": len(config.active_boards)}))
    return findings


def gate_credentials(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    kids: list[str | None] = []
    for credential in config.credentials:
        findings.extend(_private_file_findings(
            "credentials", f"named credential {credential.identifier}", credential.staged))
        if not is_regular_file(credential.staged):
            continue
        text = credential.staged.read_text(encoding="utf-8", errors="replace")
        findings.extend(credential_binding_findings(
            credential.identifier, text, config.target_url,
            credential.required_scopes, credential.forbidden_scopes))
        kids.append(token_kid(text, credential.identifier))
        if not is_regular_file(credential.live):
            findings.append(Finding("credentials", False,
                                    f"named credential {credential.identifier}: live target absent",
                                    {"path": str(credential.live)}))
    for door in config.doors:
        findings.extend(_private_file_findings("credentials", f"{door.role} door", door.staged))
        if not is_regular_file(door.staged):
            continue
        text = door.staged.read_text(encoding="utf-8", errors="replace").strip()
        if len(text.split(".")) == 3:
            findings.extend(credential_binding_findings(
                f"{door.role}-door", text, config.target_url))
            kids.append(token_kid(text, f"{door.role}-door"))
        else:
            findings.append(Finding("credentials", True,
                                    f"{door.role} door is a non-token bundle; mode and presence checked only",
                                    {"role": door.role}))
        if not is_regular_file(door.live):
            findings.append(Finding("credentials", False,
                                    f"{door.role} door: live target absent",
                                    {"path": str(door.live)}))

    findings.extend(_private_file_findings("credentials", "target JWKS", config.jwks_staged))
    staged_kids: set[str] = set()
    if is_regular_file(config.jwks_staged):
        try:
            staged_kids = jwks_kids(read_json(config.jwks_staged, "target JWKS"))
        except (GateFailure, ConfigError) as exc:
            findings.append(Finding("credentials", False, f"target JWKS is unusable: {exc}", {}))
        if not staged_kids:
            findings.append(Finding("credentials", False, "target JWKS declares no key ids", {}))
    findings.append(kid_coherence_finding(
        "credentials", "target credential set", kids, staged_kids))

    if config.backup_manifest_path.is_file():
        manifest = read_json(config.backup_manifest_path, "backup manifest")
        recorded = {str(item.get("rel_path")): str(item.get("sha256"))
                    for item in (manifest.get("artifacts") or []) if isinstance(item, Mapping)}
        expected = recorded.get("jwks/jwks.json")
        if expected and is_regular_file(config.jwks_live):
            if sha256_file(config.jwks_live) != expected:
                findings.append(Finding("credentials", False,
                                        "the active JWKS changed during preparation; the active key "
                                        "set must never be rotated before activation", {}))
            else:
                findings.append(Finding("credentials", True,
                                        "active JWKS untouched by preparation", {}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("credentials", True,
                                "named credentials, doors and JWKS are coherent with the target resource",
                                {"credentials": len(config.credentials), "doors": len(config.doors)}))
    return findings


def gate_membership(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    if not config.memberships:
        return [Finding("membership", False,
                        "no target membership expectations declared; coherence cannot be proven", {})]
    snapshot_path = config.membership_snapshot
    if snapshot_path is None or not snapshot_path.is_file():
        return [Finding("membership", False,
                        "membership read-back snapshot is absent", {})]
    snapshot = read_json(snapshot_path, "membership snapshot")
    if not isinstance(snapshot, Mapping):
        return [Finding("membership", False, "membership snapshot must be an object", {})]
    snapshot_url = str(snapshot.get("url") or "")
    if not evidence_url_acceptable(snapshot_url, config.target_url):
        findings.append(Finding("membership", False,
                                "membership snapshot was not captured against the exact target "
                                "resource; sandbox or other-port evidence is refused",
                                {"expected_url": config.target_url}))
    age = snapshot_age_seconds(snapshot.get("recorded_at"))
    if age is None or age > config.snapshot_max_age_s:
        findings.append(Finding("membership", False,
                                "membership snapshot is missing a fresh recorded_at timestamp", {}))
    boards = snapshot.get("boards") if isinstance(snapshot.get("boards"), Mapping) else {}
    for expectation in config.memberships:
        rows = boards.get(expectation.board)
        if not isinstance(rows, list):
            findings.append(Finding("membership", False,
                                    f"board {expectation.board} has no membership read-back",
                                    {"board": expectation.board}))
            continue
        match = next(
            (row for row in rows
             if isinstance(row, Mapping)
             and str(row.get("principal_id")) == expectation.principal_id), None)
        if match is None:
            findings.append(Finding("membership", False,
                                    f"target principal for {expectation.identifier} is not a member "
                                    f"of board {expectation.board}",
                                    {"board": expectation.board, "identifier": expectation.identifier}))
        elif str(match.get("role")) != expectation.role:
            findings.append(Finding("membership", False,
                                    f"{expectation.identifier} holds role "
                                    f"{match.get('role')!r} on {expectation.board}, expected "
                                    f"{expectation.role!r}",
                                    {"board": expectation.board, "identifier": expectation.identifier}))
        else:
            findings.append(Finding("membership", True,
                                    f"{expectation.identifier} verified as {expectation.role} "
                                    f"on {expectation.board}",
                                    {"board": expectation.board, "identifier": expectation.identifier}))
    return findings


def _seat_inventory(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ConfigError("seat inventory must be a list")
    inventory: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConfigError("seat inventory rows must be objects")
        name = str(row.get("name") or "")
        if not name:
            raise ConfigError("seat inventory row is missing a name")
        inventory[name] = {"role": str(row.get("role") or ""), "tier": row.get("tier")}
    return inventory


def gate_seats(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    seats = config.seats
    names = [seat.name for seat in seats]
    if len(seats) != EXPECTED_SEAT_COUNT:
        findings.append(Finding("seats", False,
                                f"expected exactly {EXPECTED_SEAT_COUNT} authoritative Team seat "
                                f"folders, config declares {len(seats)}", {"declared": len(seats)}))
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        findings.append(Finding("seats", False,
                                f"seat names must be unique ({len(duplicates)} duplicate(s))",
                                {"duplicates": duplicates}))
    roles = {seat.role for seat in seats}
    if not roles.issubset(set(SEAT_ROLES)):
        findings.append(Finding("seats", False, "seat role outside the worker/reviewer set", {}))
    for role in SEAT_ROLES:
        if role not in roles:
            findings.append(Finding("seats", False, f"no seat declares role '{role}'", {}))
    for seat in seats:
        if not 1 <= seat.tier <= MAX_SEAT_TIER:
            findings.append(Finding("seats", False,
                                    f"seat tier outside 1..{MAX_SEAT_TIER}", {"tier": seat.tier}))
    if config.seats_baseline is not None:
        if not config.seats_baseline.is_file():
            findings.append(Finding("seats", False,
                                    "declared seat baseline inventory is absent", {}))
        else:
            baseline = _seat_inventory(read_json(config.seats_baseline, "seat baseline"))
            current = {seat.name: {"role": seat.role, "tier": seat.tier} for seat in seats}
            if set(baseline) != set(current):
                findings.append(Finding("seats", False,
                                        "seat name set differs from the pre-cutover baseline; "
                                        "identities must be preserved", {}))
            else:
                drift = sorted(name for name in baseline if baseline[name] != current[name])
                if drift:
                    findings.append(Finding("seats", False,
                                            f"{len(drift)} seat(s) changed role or tier against baseline",
                                            {"drift_count": len(drift)}))
                else:
                    findings.append(Finding("seats", True,
                                            "seat names, roles and tiers preserved against baseline",
                                            {"seats": len(current)}))
    for index, seat in enumerate(seats):
        if not seat.root.is_dir():
            findings.append(Finding("seats", False,
                                    f"seat folder {index:02d} is absent", {"index": index}))
            continue
        for managed in seat.managed:
            live = seat.root / managed
            staged = config.staged_seat_root / f"{index:02d}" / managed
            if not is_regular_file(live):
                findings.append(Finding("seats", False,
                                        f"seat folder {index:02d} is missing managed file {managed}",
                                        {"index": index, "managed": managed}))
            if not is_regular_file(staged):
                findings.append(Finding("seats", False,
                                        f"staged seat folder {index:02d} is missing {managed}",
                                        {"index": index, "managed": managed}))
        wrapper = config.staged_seat_root / f"{index:02d}" / SEAT_WRAPPER
        if is_regular_file(wrapper):
            text = wrapper.read_text(encoding="utf-8", errors="replace")
            if not any(marker in text for marker in REQUIRED_SEAT_ENV) \
                    and "--boards registry" not in text:
                findings.append(Finding("seats", False,
                                        f"staged seat wrapper {index:02d} does not preserve the "
                                        "registry board scope", {"index": index}))
            if config.target_url not in text:
                findings.append(Finding("seats", False,
                                        f"staged seat wrapper {index:02d} does not bind the target "
                                        "resource", {"index": index}))
            for needle in FORBIDDEN_CA_ENV + FORBIDDEN_WAIT_FLAGS + FORBIDDEN_LIFECYCLE_COMMANDS:
                if needle in text:
                    findings.append(Finding("seats", False,
                                            f"staged seat wrapper {index:02d} references the "
                                            f"disallowed '{needle}'", {"index": index}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("seats", True,
                                "all authoritative seat folders are prepared and preserved",
                                {"seats": len(seats)}))
    return findings


def gate_configs(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    if not config.config_files:
        return [Finding("configs", False,
                        "no dependent configuration declared; coordinator, dashboard and bridge "
                        "coherence cannot be proven", {})]
    identifiers = {spec.identifier for spec in config.config_files}
    for required in ("coordinator", "dashboard", "bridge"):
        if not any(required in identifier for identifier in identifiers):
            findings.append(Finding("configs", False,
                                    f"no staged configuration declared for {required}", {}))
    for spec in config.config_files:
        findings.extend(_private_file_findings("configs", f"staged config {spec.identifier}",
                                               spec.staged, require_mode=0))
        if not spec.live.exists():
            findings.append(Finding("configs", False,
                                    f"config {spec.identifier}: live target absent",
                                    {"path": str(spec.live)}))
        if not is_regular_file(spec.staged):
            continue
        text = spec.staged.read_text(encoding="utf-8", errors="replace")
        for needle in spec.required_substrings:
            if needle not in text:
                findings.append(Finding("configs", False,
                                        f"config {spec.identifier} is missing required content",
                                        {"identifier": spec.identifier}))
        forbidden = tuple(spec.forbidden_substrings) + FORBIDDEN_CA_ENV \
            + FORBIDDEN_LIFECYCLE_COMMANDS + FORBIDDEN_WAIT_FLAGS
        for needle in dict.fromkeys(forbidden):
            if needle in text:
                findings.append(Finding("configs", False,
                                        f"config {spec.identifier} references the disallowed "
                                        f"'{needle}'", {"identifier": spec.identifier}))
        if "coordinator" in spec.identifier and config.target_url not in text:
            findings.append(Finding("configs", False,
                                    "staged coordinator configuration does not select the target "
                                    "resource", {"identifier": spec.identifier}))
        if "dashboard" in spec.identifier:
            if config.target_url not in text:
                findings.append(Finding("configs", False,
                                        "staged dashboard configuration does not point upstream at "
                                        "the target resource", {"identifier": spec.identifier}))
            if DASHBOARD_UI_URL.split("://", 1)[1] not in text:
                findings.append(Finding("configs", False,
                                        "staged dashboard configuration does not keep its own UI on "
                                        f"{DASHBOARD_UI_URL}", {"identifier": spec.identifier}))
    if not any(not finding.ok for finding in findings):
        findings.append(Finding("configs", True,
                                "staged coordinator, dashboard and bridge configuration is coherent",
                                {"files": len(config.config_files)}))
    return findings


def gate_legacy(config: CutoverConfig) -> list[Finding]:
    findings: list[Finding] = []
    if not config.legacy_markers:
        return [Finding("legacy", False,
                        "no legacy Goose CLI disabled marker declared; preservation cannot be proven",
                        {})]
    missing = 0
    for marker in config.legacy_markers:
        if not marker.exists():
            missing += 1
    if missing:
        findings.append(Finding("legacy", False,
                                f"{missing} legacy disabled marker(s) are missing",
                                {"missing": missing, "declared": len(config.legacy_markers)}))
    else:
        findings.append(Finding("legacy", True,
                                "legacy Goose CLI disabled markers preserved",
                                {"markers": len(config.legacy_markers)}))
    for step in PLAN:
        for command in FORBIDDEN_LIFECYCLE_COMMANDS:
            if command in step.step_id or command in step.summary:
                findings.append(Finding("legacy", False,
                                        f"plan step {step.step_id} references '{command}'", {}))
    return findings


def gate_host_capability(config: CutoverConfig) -> list[Finding]:
    """Report the real Team host capability; never invent a hot-reload API."""
    findings: list[Finding] = []
    adapter = config.team_adapter
    if adapter is not None:
        if is_regular_file(adapter):
            findings.append(Finding(
                "host_capability", True,
                "AionUI Team lifecycle adapter is present; pause/resume is delegated through it",
                {"capability": "team_adapter", "adapter_sha256": sha256_file(adapter)}))
        else:
            findings.append(Finding(
                "host_capability", False,
                "declared Team lifecycle adapter is absent; the toolkit refuses to assume a "
                "hot-reload API that does not exist", {"capability": "declared_but_missing"}))
            return findings
    elif config.helper_bin:
        findings.append(Finding(
            "host_capability", True,
            "Team helper CLI declared; pause/resume is operator executed through it",
            {"capability": "helper_cli"}))
    else:
        findings.append(Finding(
            "host_capability", True,
            "no supported host API rewrites running Team seat connectors; Team pause/resume stays "
            "an operator step on the command sheet", {"capability": "operator_manual"}))
    operator_steps = [step.step_id for step in PLAN
                      if step.execution == "operator" and step.phase in ("activate", "rollback")]
    findings.append(Finding("host_capability", True,
                            f"{len(operator_steps)} service and Team lifecycle step(s) remain "
                            "operator executed", {"operator_steps": operator_steps}))
    return findings


GATES: Mapping[str, Any] = {
    "gate_staging": gate_staging,
    "gate_url": gate_url,
    "gate_artifacts": gate_artifacts,
    "gate_backup": gate_backup,
    "gate_drain": gate_drain,
    "gate_credentials": gate_credentials,
    "gate_membership": gate_membership,
    "gate_seats": gate_seats,
    "gate_configs": gate_configs,
    "gate_legacy": gate_legacy,
    "gate_host_capability": gate_host_capability,
}
PREFLIGHT_GATES: tuple[str, ...] = (
    "gate_staging", "gate_url", "gate_artifacts", "gate_backup",
)
DRY_RUN_GATES: tuple[str, ...] = tuple(GATES)


def run_gates(config: CutoverConfig, names: Sequence[str] = DRY_RUN_GATES) -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        gate = GATES.get(name)
        if gate is None:
            findings.append(Finding(name, False, f"unknown gate '{name}'", {}))
            continue
        findings.extend(gate(config))
    return findings


def findings_ok(findings: Sequence[Finding]) -> bool:
    return all(finding.ok for finding in findings)


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def plan_hash() -> str:
    canonical = json.dumps(
        {"plan": [step.as_dict() for step in PLAN], "target_url": TARGET_URL},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(canonical)


def live_state_fingerprint(config: CutoverConfig) -> dict[str, str]:
    """Hash every live activation target so non-mutation can be proven."""
    fingerprint: dict[str, str] = {}
    for swap in config.swaps():
        if is_regular_file(swap.live_path):
            fingerprint[swap.identifier] = sha256_file(swap.live_path)
        else:
            fingerprint[swap.identifier] = "absent"
    for identifier, path in config.rollback_unit():
        if is_regular_file(path):
            fingerprint.setdefault(f"unit:{identifier}", sha256_file(path))
    return fingerprint


def target_proof(config: CutoverConfig) -> list[dict[str, Any]]:
    proof: list[dict[str, Any]] = []
    for swap in config.swaps():
        entry: dict[str, Any] = {
            "step_id": swap.step_id,
            "identifier": swap.identifier,
            "live_exists": is_regular_file(swap.live_path),
            "staged_exists": is_regular_file(swap.staged_path),
        }
        if is_regular_file(swap.live_path):
            entry["live_mode"] = oct(file_mode(swap.live_path))
        if is_regular_file(swap.staged_path):
            entry["staged_sha256"] = sha256_file(swap.staged_path)
            entry["staged_mode"] = oct(file_mode(swap.staged_path))
        proof.append(entry)
    return proof


def prepare_staging(config: CutoverConfig) -> dict[str, Any]:
    """Create the private staging skeleton. Touches nothing under the live root."""
    ensure_private_dir(config.staging_root)
    for relative in STAGING_SUBDIRS:
        ensure_private_dir(config.staging_root / relative)
    ensure_private_dir(config.backup_root)
    created = [str(config.staging_root / relative) for relative in STAGING_SUBDIRS]
    return {"staging_root": str(config.staging_root), "created": created,
            "backup_root": str(config.backup_root)}


def command_sheet(config: CutoverConfig, phase: str) -> list[str]:
    """Operator command sheet for one phase (private local handoff, real paths)."""
    lines = [
        f"# O1 cutover command sheet: {phase}",
        f"# toolkit: {TOOLKIT_IDENTIFIER}",
        f"# runbook: {RUNBOOK_REFERENCE}",
        f"# target resource: {config.target_url}",
        f"# previous resource: {config.previous_url}",
        "# Service and AionUI Team lifecycle steps are operator executed; the toolkit",
        "# never starts, stops, pauses or resumes a service or a Team.",
        "",
    ]
    for step in PLAN:
        if step.phase != phase:
            continue
        lines.append(f"## {step.step_id} ({step.execution})")
        lines.append(f"#   {step.summary}")
        if step.requires:
            lines.append(f"#   gates: {', '.join(step.requires)}")
        if step.execution == "operator":
            lines.extend(_operator_sheet_lines(config, step.step_id))
        else:
            lines.append(_toolkit_sheet_line(config, step.step_id))
        lines.append("")
    deduped: list[str] = []
    for line in lines:
        if deduped and line == deduped[-1]:
            continue
        deduped.append(line)
    return deduped


def _toolkit_sheet_line(config: CutoverConfig, step_id: str) -> str:
    toolkit = shlex.quote(str(config.repo_root / "tools" / "o1_cutover.py"))
    config_path = shlex.quote(str(config.path))
    command = TOOLKIT_COMMANDS.get(step_id, step_id)
    if command == "activate":
        evidence = shlex.quote(str(
            config.evidence_dir / "<FRESH_DRY_RUN_EVIDENCE>.json"))
        return (f"python {toolkit} --config {config_path} activate --operator-confirmed "
                f"--evidence {evidence}")
    if command == "backup":
        return f"python {toolkit} --config {config_path} backup"
    return f"python {toolkit} --config {config_path} {command}"


def _operator_sheet_lines(config: CutoverConfig, step_id: str) -> list[str]:
    staging = config.staging_root
    toolkit = shlex.quote(str(config.repo_root / "tools" / "o1_cutover.py"))
    config_path = shlex.quote(str(config.path))
    if step_id == "declare-freeze":
        return [
            "# Pause both AionUI Teams through the Team lifecycle adapter if it is installed;",
            "# otherwise pause them in the AionUI Team control plane. Never use start-all.",
            f"node {config.team_adapter} apply --spec {staging}/team-spec.json --dry-run"
            if config.team_adapter else
            "# no adapter declared: pause the Teams manually in the AionUI control plane",
        ]
    if step_id == "drain-work":
        source = shlex.quote(str(staging / "cutover-evidence" / "board-dump.json"))
        out = shlex.quote(str(config.board_snapshot))
        return [
            "# Require zero held tickets on every active board, then snapshot the result:",
            f"python {toolkit} --config {config_path} snapshot-boards "
            f"--from-json {source} --out {out}",
        ]
    if step_id == "record-baseline":
        return [
            f"curl -fsS {config.previous_url.rsplit('/', 1)[0]}/healthz "
            f"> {staging}/cutover-evidence/baseline-health.json",
            "# Record registry summaries, listener ownership, process start times and",
            "# credential/JWKS metadata (never token contents) in the private change record.",
        ]
    if step_id == "stage-target-credentials":
        return [
            f"umask 077 && python {config.operator_toolkit or '/PATH/TO/operator-private'}/jwt_provision.py",
            "# Issue, do not rotate, the worker and reviewer doors into the staged JWKS:",
            f"pursers-door issue --role worker --central-url {config.target_url} "
            f"--jwks {config.jwks_staged} --keys-dir {staging}/target-jwt/door-keys "
            f"> {staging}/target-jwt/worker.door",
            f"pursers-door issue --role reviewer --central-url {config.target_url} "
            f"--jwks {config.jwks_staged} --keys-dir {staging}/target-jwt/door-keys "
            f"> {staging}/target-jwt/reviewer.door",
            "# Door and token files must be mode 0600 and must never be printed.",
        ]
    if step_id == "commit-target-memberships":
        source = shlex.quote(str(
            staging / "cutover-evidence" / "membership-dump.json"))
        out = shlex.quote(str(
            config.membership_snapshot or staging / "membership-snapshot.json"))
        return [
            "# With the current HTTPS admin credential, add every target principal to every",
            "# active board and read the membership back:",
            f"python {config.repo_root / 'tools' / 'wait-bridge' / 'registry_admin.py'} "
            f"--url {config.previous_url} member-add --board <BOARD> "
            "--principal-id <TARGET_PRINCIPAL> --role <ROLE>",
            f"python {toolkit} --config {config_path} snapshot-memberships "
            f"--from-json {source} --out {out}",
        ]
    if step_id == "sandbox-acceptance":
        return [
            "# Use a copied data root, a different loopback port and sandbox credentials",
            "# whose aud/resource equal that sandbox URL. Sandbox evidence never satisfies a",
            "# production URL-bound gate.",
            f"python {config.repo_root / 'tools' / 'central_scaffold.py'} init "
            f"--root {staging}/sandbox-instance --name sandbox --port 8799",
        ]
    if step_id in ("stop-consumers", "start-central", "start-consumers",
                   "verify-memberships-live", "doctor-and-resume", "rollback-restart"):
        return [
            "# Operator executed service lifecycle. The toolkit refuses to run these steps.",
            f"curl -fsS {config.target_url.rsplit('/', 1)[0]}/healthz",
            f"python {config.repo_root / 'tools' / 'wait-bridge' / 'registry_doctor.py'} --json",
            f"python {config.repo_root / 'tools' / 'fleet-dashboard' / 'seat_config.py'} doctor --json",
        ]
    return ["# No toolkit command; see the runbook step for the operator actions."]


def run_prepare(config: CutoverConfig) -> dict[str, Any]:
    validate_layout(config)
    staged = prepare_staging(config)
    sheets: dict[str, str] = {}
    for phase in ("prepare", "activate", "rollback"):
        sheet_path = config.evidence_dir / f"{phase}-command-sheet.txt"
        payload = ("\n".join(command_sheet(config, phase)) + "\n").encode("utf-8")
        write_bytes_atomic(sheet_path, payload, PRIVATE_FILE_MODE)
        sheets[phase] = str(sheet_path)
    return {"phase": "prepare", "staging": staged, "command_sheets": sheets,
            "plan_hash": plan_hash(),
            "activation_steps": list(TOOLKIT_ACTIVATION_STEPS)}


def run_backup(config: CutoverConfig) -> dict[str, Any]:
    """Copy and hash the whole rollback unit into the private backup root."""
    validate_layout(config)
    entries = config.rollback_unit()
    artifacts = copy_rollback_unit(config, entries)
    findings = verify_backup_integrity(config)
    return {"phase": "backup", "identifiers": sorted({a.identifier for a in artifacts}),
            "member_files": len(artifacts),
            "manifest": str(config.backup_manifest_path),
            "manifest_sha256": sha256_file(config.backup_manifest_path),
            "integrity_ok": findings_ok(findings),
            "integrity": [finding.as_dict() for finding in findings]}


def run_preflight(config: CutoverConfig) -> tuple[list[Finding], dict[str, Any]]:
    validate_layout(config)
    findings = run_gates(config, PREFLIGHT_GATES)
    return findings, {"phase": "preflight", "gates": list(PREFLIGHT_GATES)}


def run_dry_run(config: CutoverConfig, evidence_out: Path | None = None) -> tuple[
        list[Finding], dict[str, Any], Path]:
    """Prove every target and gate, and prove that nothing live changed."""
    validate_layout(config)
    before = live_state_fingerprint(config)
    findings = run_gates(config, DRY_RUN_GATES)
    after = live_state_fingerprint(config)
    mutations = sorted(key for key in before if before.get(key) != after.get(key))
    if mutations:
        findings.append(Finding("dry_run", False,
                                f"dry run mutated {len(mutations)} live target(s); aborting",
                                {"mutated_count": len(mutations)}))
    else:
        findings.append(Finding("dry_run", True,
                                "dry run completed with zero live mutations",
                                {"live_targets_proven": len(before)}))
    journal = journal_load(config.journal_path)
    interrupted = interrupted_steps(journal)
    if interrupted:
        findings.append(Finding("dry_run", False,
                                f"a previous activation was interrupted at {len(interrupted)} step(s); "
                                "roll back before preparing another activation",
                                {"interrupted": interrupted}))
    ok = findings_ok(findings)
    evidence_path = staging_output_path(
        config,
        evidence_out or (
            config.evidence_dir / f"{EVIDENCE_PREFIX}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"),
        "dry-run evidence output")
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "runbook": RUNBOOK_REFERENCE,
        "recorded_at": iso_now(),
        "plan_hash": plan_hash(),
        "target_url": config.target_url,
        "previous_url": config.previous_url,
        "ok": ok,
        "live_targets": len(before),
        "mutations": mutations,
        "targets": target_proof(config),
        "findings": [finding.as_dict() for finding in findings],
        "refusals": [finding.as_dict() for finding in findings if not finding.ok],
    }
    ensure_private_dir(evidence_path.parent)
    write_json_atomic(evidence_path, evidence)
    return findings, {"phase": "dry-run", "evidence": str(evidence_path),
                      "targets_proven": len(evidence["targets"]), "mutations": mutations}, evidence_path


def load_evidence(config: CutoverConfig, path: Path) -> dict[str, Any]:
    path = staging_output_path(config, path, "dry-run evidence")
    evidence = read_json(path, "dry-run evidence")
    if not isinstance(evidence, Mapping):
        raise GateFailure("evidence", ["dry-run evidence must be an object"])
    problems: list[str] = []
    if evidence.get("plan_hash") != plan_hash():
        problems.append("evidence plan_hash does not match the current plan")
    if not evidence_url_acceptable(str(evidence.get("target_url") or ""), config.target_url):
        problems.append("evidence was not captured for the exact target resource")
    if evidence.get("ok") is not True:
        problems.append("evidence records a refused dry run")
    if evidence.get("mutations"):
        problems.append("evidence records live mutations during the dry run")
    age = snapshot_age_seconds(str(evidence.get("recorded_at") or ""))
    if age is None:
        problems.append("evidence has no parsable recorded_at timestamp")
    elif age > config.evidence_max_age_s:
        problems.append(f"evidence is stale ({int(age)}s over the {config.evidence_max_age_s}s limit)")
    if problems:
        raise GateFailure("evidence", problems)
    return dict(evidence)


def run_activate(config: CutoverConfig, evidence_path: Path, operator_confirmed: bool,
                 rehearse_fail_at: str | None = None) -> dict[str, Any]:
    """Operator-gated activation of live files only (never service lifecycle)."""
    validate_layout(config)
    if not operator_confirmed:
        raise GateFailure("activation", ["activation requires an explicit operator confirmation"])
    load_evidence(config, evidence_path)
    findings = run_gates(config, DRY_RUN_GATES)
    if not findings_ok(findings):
        refusals = [finding.detail for finding in findings if not finding.ok]
        raise GateFailure("activation", [f"{len(refusals)} gate refusal(s) block activation",
                                         refusals[0] if refusals else ""])
    journal = journal_load(config.journal_path)
    interrupted = interrupted_steps(journal)
    if interrupted:
        raise GateFailure("activation", [
            f"interrupted activation detected at {', '.join(interrupted)}; roll back first"])
    if rehearse_fail_at and rehearse_fail_at not in TOOLKIT_ACTIVATION_STEPS:
        raise GateFailure("activation", [
            f"rehearsal step '{rehearse_fail_at}' is not a toolkit activation step"])

    performed: list[dict[str, Any]] = []
    pending_operator: list[str] = []
    for step in PLAN:
        if step.phase != "activate":
            continue
        if step.execution == "operator":
            pending_operator.append(step.step_id)
            continue
        swaps = config.swaps_for_step(step.step_id)
        targets = []
        for swap in swaps:
            if not is_regular_file(swap.staged_path):
                raise GateFailure("activation", [
                    f"staged source is missing for {swap.identifier}"])
            pre_sha = sha256_file(swap.live_path) if is_regular_file(swap.live_path) else None
            targets.append({
                "identifier": swap.identifier,
                "live": str(swap.live_path),
                "staged": str(swap.staged_path),
                "backup_ref": swap.identifier,
                "pre_sha256": pre_sha,
                "staged_sha256": sha256_file(swap.staged_path),
                "mode": file_mode(swap.staged_path),
            })
        journal = journal_append(config.journal_path, JournalEntry(
            step.step_id, "started", iso_now(), tuple(targets)))
        if rehearse_fail_at == step.step_id:
            raise GateFailure("activation", [
                f"rehearsal interruption injected at {step.step_id}; journal records a partial "
                "activation that rollback must repair"])
        for target in targets:
            destination = Path(target["live"])
            payload = Path(target["staged"]).read_bytes()
            mode = int(target["mode"]) or PRIVATE_FILE_MODE
            if destination.parent != destination:
                destination.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(destination, payload, mode)
            if sha256_file(destination) != target["staged_sha256"]:
                raise GateFailure("activation", [
                    f"post-write verification failed for {target['identifier']}"])
            performed.append({"step_id": step.step_id, "identifier": target["identifier"],
                              "sha256": target["staged_sha256"], "mode": oct(mode)})
        journal = journal_append(config.journal_path, JournalEntry(
            step.step_id, "done", iso_now(), tuple(targets)))
    return {"phase": "activate", "performed": performed,
            "pending_operator_steps": pending_operator,
            "journal": str(config.journal_path),
            "rehearsal": bool(rehearse_fail_at)}


def rollback_coherence_findings(
    config: CutoverConfig, *, from_backup: bool = False
) -> list[Finding]:
    """Prove old tokens pair with the old JWKS, in backup or restored live state."""
    findings: list[Finding] = []
    kids: list[str | None] = []
    state_label = "backup" if from_backup else "restored"
    for credential in config.credentials:
        path = (backup_member_path(
            config.backup_root, f"credentials/{credential.identifier}.jwt")
            if from_backup else credential.live)
        if not is_regular_file(path):
            findings.append(Finding("rollback_coherence", False,
                                    f"{state_label} credential {credential.identifier} is absent", {}))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(Finding("rollback_coherence", finding.ok, finding.detail,
                                finding.evidence)
                        for finding in credential_binding_findings(
                            f"{state_label}-{credential.identifier}", text,
                            config.previous_url))
        kids.append(token_kid(text, credential.identifier))
    jwks_path = (backup_member_path(config.backup_root, "jwks/jwks.json")
                 if from_backup else config.jwks_live)
    if not is_regular_file(jwks_path):
        findings.append(Finding(
            "rollback_coherence", False, f"{state_label} JWKS is absent", {}))
        return findings
    try:
        available = jwks_kids(read_json(jwks_path, f"{state_label} JWKS"))
    except (GateFailure, ConfigError) as exc:
        findings.append(Finding("rollback_coherence", False,
                                f"{state_label} JWKS is unusable: {exc}", {}))
        return findings
    findings.append(kid_coherence_finding(
        "rollback_coherence", f"{state_label} credential set", kids, available))
    return findings


def run_rollback(config: CutoverConfig, operator_confirmed: bool) -> dict[str, Any]:
    """Restore the backup unit in reverse journal order, refusing a mixed restore."""
    validate_layout(config)
    if not operator_confirmed:
        raise GateFailure("rollback", ["rollback requires an explicit operator confirmation"])
    integrity = verify_backup_integrity(config)
    if not findings_ok(integrity):
        raise GateFailure("rollback", [finding.detail for finding in integrity if not finding.ok])
    backup_coherence = rollback_coherence_findings(config, from_backup=True)
    if not findings_ok(backup_coherence):
        raise GateFailure(
            "rollback", [finding.detail for finding in backup_coherence if not finding.ok])
    journal = journal_load(config.journal_path)
    steps = steps_needing_rollback(journal)
    recorded = validate_rollback_journal(config, journal, steps)
    if not steps:
        return {"phase": "rollback", "restored": [], "pending_operator_steps": [],
                "detail": "journal records no toolkit activation to undo"}

    restored: list[dict[str, Any]] = []
    for step_id in steps:
        targets = list(reversed(recorded.get(step_id) or []))
        for target in targets:
            identifier = str(target.get("identifier") or "")
            source = backup_member_path(config.backup_root, identifier)
            destination = Path(str(target.get("live") or ""))
            if not is_regular_file(source):
                raise GateFailure("rollback", [
                    f"backup member is missing for {identifier}; refusing a partial restore"])
            payload = source.read_bytes()
            expected = str(target["pre_sha256"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(destination, payload, PRIVATE_FILE_MODE)
            if sha256_file(destination) != expected:
                raise GateFailure("rollback", [
                    f"restored bytes for {identifier} do not match the recorded pre-activation hash"])
            if is_regular_file(source):
                os.chmod(destination, file_mode(source))
            restored.append({"step_id": step_id, "identifier": identifier,
                             "sha256": sha256_file(destination)})
        journal = journal_append(config.journal_path, JournalEntry(
            step_id, "rolled_back", iso_now(), tuple(recorded.get(step_id) or ())))

    coherence = rollback_coherence_findings(config)
    if not findings_ok(coherence):
        raise GateFailure(
            "rollback", [finding.detail for finding in coherence if not finding.ok]
        )
    pending_operator = [step.step_id for step in PLAN
                        if step.phase == "rollback" and step.execution == "operator"]
    return {"phase": "rollback", "restored": restored,
            "pending_operator_steps": pending_operator,
            "coherence": [finding.as_dict() for finding in coherence],
            "coherence_ok": findings_ok(coherence),
            "journal": str(config.journal_path)}


def snapshot_boards(config: CutoverConfig, source: Path, out: Path) -> dict[str, Any]:
    """Normalize an operator-supplied board dump into a gate-usable snapshot."""
    validate_layout(config)
    out = staging_output_path(config, out, "board snapshot output")
    raw = read_json(source, "board dump")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("boards"), Mapping):
        raise ConfigError("board dump must contain a 'boards' mapping")
    boards: dict[str, dict[str, int]] = {}
    for board, counts in raw["boards"].items():
        if not isinstance(counts, Mapping):
            raise ConfigError(f"board dump entry for {board} must be an object")
        boards[str(board)] = {state: int(counts.get(state, 0) or 0)
                              for state in HELD_TICKET_STATES + ("open",)}
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "recorded_at": str(raw.get("recorded_at") or iso_now()),
        "dispatch_paused": bool(raw.get("dispatch_paused")),
        "teams_paused": bool(raw.get("teams_paused")),
        "boards": boards,
    }
    ensure_private_dir(out.parent)
    write_json_atomic(out, snapshot)
    missing = sorted(set(config.active_boards) - set(boards))
    return {"phase": "snapshot-boards", "out": str(out), "boards": sorted(boards),
            "missing_active_boards": missing}


def snapshot_memberships(config: CutoverConfig, source: Path, out: Path) -> dict[str, Any]:
    """Normalize an operator-supplied membership read-back into a snapshot."""
    validate_layout(config)
    out = staging_output_path(config, out, "membership snapshot output")
    raw = read_json(source, "membership dump")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("boards"), Mapping):
        raise ConfigError("membership dump must contain a 'boards' mapping")
    url = str(raw.get("url") or config.target_url)
    parse_loopback_url(url, "membership dump url")
    boards: dict[str, list[dict[str, str]]] = {}
    for board, rows in raw["boards"].items():
        if not isinstance(rows, list):
            raise ConfigError(f"membership dump entry for {board} must be a list")
        boards[str(board)] = [
            {"principal_id": str(row.get("principal_id")), "role": str(row.get("role"))}
            for row in rows if isinstance(row, Mapping) and row.get("principal_id")]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "recorded_at": str(raw.get("recorded_at") or iso_now()),
        "url": url,
        "boards": boards,
    }
    ensure_private_dir(out.parent)
    write_json_atomic(out, snapshot)
    return {"phase": "snapshot-memberships", "out": str(out), "boards": sorted(boards),
            "url": url}


def build_report(phase: str, config: CutoverConfig, findings: Sequence[Finding],
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "toolkit": TOOLKIT_IDENTIFIER,
        "runbook": RUNBOOK_REFERENCE,
        "phase": phase,
        "recorded_at": iso_now(),
        "target_url": config.target_url,
        "previous_url": config.previous_url,
        "ok": findings_ok(findings),
        "finding_count": len(findings),
        "refusal_count": sum(1 for finding in findings if not finding.ok),
        "findings": [finding.as_dict() for finding in findings],
        "refusals": [finding.as_dict() for finding in findings if not finding.ok],
    }
    report.update(dict(extra or {}))
    return config.sanitizer().structure(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="o1_cutover.py",
        description="O1 staged HTTP-loopback cutover toolkit (preparation, gates, "
                    "operator-gated activation, rollback).")
    parser.add_argument("--config", type=Path, help="operator-private cutover configuration")
    parser.add_argument("--json", action="store_true", default=True,
                        help="emit the sanitized JSON report (default)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("template", help="print a placeholder configuration template")
    sub.add_parser("plan", help="print the ordered plan and its gate requirements")

    prepare = sub.add_parser("prepare", help="create the private staging skeleton and command sheets")
    prepare.add_argument("--out", type=Path, help="optional sheet output directory override")

    sub.add_parser("preflight", help="run the additive preflight gates (non-mutating)")
    sub.add_parser("backup", help="copy and hash the rollback unit into the private backup root")
    dry = sub.add_parser("dry-run", help="prove every target and gate with zero live mutations")
    dry.add_argument("--evidence-out", type=Path, help="where to write the dry-run evidence record")

    activate = sub.add_parser("activate", help="operator-gated activation of live files only")
    activate.add_argument("--operator-confirmed", action="store_true",
                          help="required: the operator accepts the token-invalidating boundary")
    activate.add_argument("--evidence", type=Path, required=True,
                          help="fresh dry-run evidence record matching the current plan")
    activate.add_argument("--rehearse-fail-at", metavar="STEP_ID",
                          help="rehearsal only: interrupt activation after journalling this step")

    rollback = sub.add_parser("rollback", help="restore the backup unit in reverse journal order")
    rollback.add_argument("--operator-confirmed", action="store_true",
                          help="required: the operator accepts the rollback")

    sheet = sub.add_parser("command-sheet", help="write the private operator command sheet")
    sheet.add_argument("--phase", required=True, choices=("prepare", "activate", "rollback"))
    sheet.add_argument("--out", type=Path, help="private output path (default: staging evidence dir)")

    snap_boards = sub.add_parser("snapshot-boards", help="normalize an operator board dump")
    snap_boards.add_argument("--from-json", type=Path, required=True)
    snap_boards.add_argument("--out", type=Path, required=True)

    snap_members = sub.add_parser("snapshot-memberships", help="normalize a membership read-back")
    snap_members.add_argument("--from-json", type=Path, required=True)
    snap_members.add_argument("--out", type=Path, required=True)

    sub.add_parser("verify-artifacts", help="verify release digests, backup unit and launcher hashes")
    return parser


def _load(args: argparse.Namespace) -> CutoverConfig:
    if not args.config:
        raise ConfigError("--config is required for this command")
    return load_config(args.config)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config: CutoverConfig | None = None
    try:
        if args.command == "template":
            print(json.dumps(config_template(), indent=2, sort_keys=True))
            return 0
        if args.command == "plan":
            print(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "toolkit": TOOLKIT_IDENTIFIER,
                "runbook": RUNBOOK_REFERENCE,
                "plan_hash": plan_hash(),
                "target_url": TARGET_URL,
                "phases": sorted({step.phase for step in PLAN}),
                "toolkit_activation_steps": list(TOOLKIT_ACTIVATION_STEPS),
                "steps": [step.as_dict() for step in PLAN],
            }, indent=2, sort_keys=True))
            return 0

        config = _load(args)
        if args.command == "prepare":
            extra = run_prepare(config)
            print(json.dumps(config.sanitizer().structure(
                {"ok": True, **extra}), indent=2, sort_keys=True))
            return 0
        if args.command == "backup":
            extra = run_backup(config)
            report = build_report("backup", config, [], extra)
            report["ok"] = bool(extra["integrity_ok"])
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if extra["integrity_ok"] else 1
        if args.command == "preflight":
            findings, extra = run_preflight(config)
            print(json.dumps(build_report("preflight", config, findings, extra),
                             indent=2, sort_keys=True))
            return 0 if findings_ok(findings) else 1
        if args.command == "dry-run":
            findings, extra, evidence_path = run_dry_run(config, args.evidence_out)
            report = build_report("dry-run", config, findings, extra)
            report["evidence_sha256"] = sha256_file(evidence_path)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if findings_ok(findings) else 1
        if args.command == "verify-artifacts":
            findings = gate_artifacts(config) + gate_backup(config)
            print(json.dumps(build_report("verify-artifacts", config, findings),
                             indent=2, sort_keys=True))
            return 0 if findings_ok(findings) else 1
        if args.command == "command-sheet":
            out = staging_output_path(
                config,
                args.out or (config.evidence_dir / f"{args.phase}-command-sheet.txt"),
                "command sheet output")
            ensure_private_dir(out.parent)
            write_bytes_atomic(out, ("\n".join(command_sheet(config, args.phase)) + "\n").encode("utf-8"))
            print(json.dumps(config.sanitizer().structure(
                {"ok": True, "phase": args.phase, "sheet": str(out)}), indent=2, sort_keys=True))
            return 0
        if args.command == "snapshot-boards":
            result = snapshot_boards(config, args.from_json, args.out)
            print(json.dumps(config.sanitizer().structure({"ok": True, **result}),
                             indent=2, sort_keys=True))
            return 0
        if args.command == "snapshot-memberships":
            result = snapshot_memberships(config, args.from_json, args.out)
            print(json.dumps(config.sanitizer().structure({"ok": True, **result}),
                             indent=2, sort_keys=True))
            return 0
        if args.command == "activate":
            try:
                extra = run_activate(config, args.evidence, args.operator_confirmed,
                                     args.rehearse_fail_at)
            except GateFailure as exc:
                findings = run_gates(config, DRY_RUN_GATES)
                findings.append(Finding("activation", False, "; ".join(exc.reasons),
                                        {"gate": exc.gate}))
                print(json.dumps(build_report("activate", config, findings,
                                              {"refused": True}), indent=2, sort_keys=True))
                return 1
            print(json.dumps(build_report("activate", config, [
                Finding("activation", True,
                        f"activation applied {len(extra['performed'])} file replacement(s)",
                        {"performed": extra["performed"],
                         "pending_operator_steps": extra["pending_operator_steps"]})],
                extra), indent=2, sort_keys=True))
            return 0
        if args.command == "rollback":
            try:
                extra = run_rollback(config, args.operator_confirmed)
            except GateFailure as exc:
                print(json.dumps(build_report("rollback", config, [
                    Finding("rollback", False, "; ".join(exc.reasons), {"gate": exc.gate}),
                ], {"refused": True}), indent=2, sort_keys=True))
                return 1
            ok = bool(extra.get("coherence_ok", True))
            findings = [Finding("rollback", ok,
                                f"restored {len(extra['restored'])} file(s) from the backup unit",
                                {"restored": extra["restored"],
                                 "coherence": extra.get("coherence", []),
                                 "pending_operator_steps": extra["pending_operator_steps"]})]
            print(json.dumps(build_report("rollback", config, findings, extra),
                             indent=2, sort_keys=True))
            return 0 if ok else 1
        parser.error(f"unknown command {args.command}")
        return 2
    except ConfigError as exc:
        message = str(exc)
        if config is not None:
            message = config.sanitizer().text(message)
        print(json.dumps({"ok": False, "error": "config", "detail": message}, indent=2),
              file=sys.stderr)
        return 2
    except GateFailure as exc:
        message = "; ".join(exc.reasons)
        if config is not None:
            message = config.sanitizer().text(message)
        print(json.dumps({"ok": False, "error": "gate", "gate": exc.gate, "detail": message},
                         indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
