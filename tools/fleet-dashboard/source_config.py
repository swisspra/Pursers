"""Guarded local adapter for Board Butler source configuration.

The Board Butler modules remain the authoritative parsers.  This adapter adds
the dashboard's immutable preview/apply/readback flow without exposing private
paths or secret values to the browser.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


PLAN_TTL_SECONDS = 120
PLAN_LIMIT = 25
MAX_DOCUMENT_BYTES = 1_048_576
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PLAN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
REDACTED = "[redacted]"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load authoritative source configuration module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_BUTLER_DIR = Path(__file__).resolve().parents[1] / "board-butler"
_BUTLER = _load_module("fleet_source_contract_butler", _BUTLER_DIR / "board_butler.py")
_ONBOARDING = _load_module(
    "fleet_source_contract_onboarding", _BUTLER_DIR / "project_onboarding.py"
)


def _canonical(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("source configuration must be canonical JSON") from exc
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValueError("source configuration exceeds the 1048576-byte limit")
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _restore_redacted(candidate: Any, current: Any) -> Any:
    """Treat the public redaction marker as preserve-current, never as a value."""
    if candidate == REDACTED:
        if current is None:
            raise ValueError("redacted fields cannot be introduced without a stored value")
        return copy.deepcopy(current)
    if isinstance(candidate, Mapping):
        previous = current if isinstance(current, Mapping) else {}
        return {
            str(key): _restore_redacted(value, previous.get(str(key)))
            for key, value in candidate.items()
        }
    if isinstance(candidate, list):
        previous = current if isinstance(current, list) else []
        return [
            _restore_redacted(value, previous[index] if index < len(previous) else None)
            for index, value in enumerate(candidate)
        ]
    return copy.deepcopy(candidate)


def _private_file(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute():
        raise ValueError("source configuration path must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("source configuration file is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 0 < info.st_size <= MAX_DOCUMENT_BYTES
    ):
        raise ValueError(
            "source configuration must be an owned non-symlink mode-0600 regular file"
        )
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("source configuration file is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("source configuration document must be an object")
    _canonical(document)
    return document, raw


class SourceConfigurationStore:
    """Validate and atomically update configured private source documents."""

    def __init__(
        self,
        connector_path: Path | None,
        onboarding_path: Path | None,
        *,
        board_id: str,
        project_id: str = "registry",
        actor_id: str = "fleet-dashboard",
        now_factory: Any = time.time,
    ) -> None:
        self.paths = {
            "source_connectors": connector_path,
            "source_onboarding": onboarding_path,
        }
        self.board_id = board_id
        self.project_id = project_id
        self.actor_id = actor_id
        self.now_factory = now_factory
        self._plans: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _validate(self, family: str, document: Mapping[str, Any]) -> dict[str, Any]:
        path = self.paths.get(family)
        if path is None:
            raise ValueError(f"{family} private file is not configured")
        if family == "source_onboarding":
            contract = _ONBOARDING.inspect_source_onboarding_configuration(document)
        elif family == "source_connectors":
            encoded = _canonical(document)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.validate-", dir=str(path.parent)
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                contract = _BUTLER.inspect_connector_source_configuration(
                    Path(temporary),
                    default_board_id=self.board_id,
                    default_project_id=self.project_id,
                    default_actor_id=self.actor_id,
                    root=path.parent,
                )
            finally:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
        else:
            raise ValueError("unsupported source configuration family")
        return {
            **contract.export(),
            "comparison": contract.compare(),
            "expected_sha256": _digest(document),
            "status": "configurable",
            "apply_mode": "restart-required",
            "restart_required": True,
            "secret_values_readable": False,
        }

    def snapshot(self, family: str) -> dict[str, Any]:
        path = self.paths.get(family)
        if path is None:
            return {
                "status": "unavailable",
                "reason": f"{family} private file is not configured for Fleet",
                "apply_mode": "restart-required",
                "restart_required": True,
                "secret_values_readable": False,
            }
        try:
            document, _raw = _private_file(path)
            return self._validate(family, document)
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "apply_mode": "restart-required",
                "restart_required": True,
                "secret_values_readable": False,
            }

    def plan(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {
            "family",
            "expected_sha256",
            "document",
        }:
            raise ValueError(
                "source plan must contain family, expected_sha256, and document"
            )
        family = request.get("family")
        if family not in self.paths:
            raise ValueError("unsupported source configuration family")
        expected = request.get("expected_sha256")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        document = request.get("document")
        if not isinstance(document, dict):
            raise ValueError("document must be an object")
        path = self.paths[family]
        if path is None:
            raise ValueError(f"{family} private file is not configured")
        current, _raw = _private_file(path)
        if not hmac.compare_digest(_digest(current), expected):
            raise ValueError("source configuration changed; reload before planning")
        document = _restore_redacted(document, current)
        preview = self._validate(family, document)
        created = float(self.now_factory())
        plan = {
            "plan_id": uuid.uuid4().hex,
            "family": family,
            "expected_sha256": expected,
            "document": copy.deepcopy(document),
            "expires_at_epoch": created + PLAN_TTL_SECONDS,
            "apply_mode": "restart-required",
        }
        plan["digest"] = _digest(plan)
        with self._lock:
            self._plans = {
                key: value
                for key, value in self._plans.items()
                if value["expires_at_epoch"] > created
            }
            if len(self._plans) >= PLAN_LIMIT:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan["plan_id"]] = plan
        return {
            "schema": "fleet_source_config_plan_v1",
            "plan_id": plan["plan_id"],
            "family": family,
            "digest": plan["digest"],
            "expires_at_epoch": plan["expires_at_epoch"],
            "apply_mode": plan["apply_mode"],
            "restart_required": True,
            "preview": preview,
        }

    def apply(self, plan_id: Any, digest: Any) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise ValueError("source configuration plan_id is invalid")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError("source configuration digest is invalid")
        with self._lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None:
            raise KeyError(plan_id)
        if not hmac.compare_digest(plan["digest"], digest):
            raise ValueError("source configuration plan digest mismatch")
        if float(self.now_factory()) >= plan["expires_at_epoch"]:
            raise ValueError("source configuration plan expired")
        family = plan["family"]
        path = self.paths[family]
        assert path is not None
        current, before_raw = _private_file(path)
        if not hmac.compare_digest(_digest(current), plan["expected_sha256"]):
            raise ValueError("source configuration changed; prepare a new plan")
        encoded = json.dumps(
            plan["document"], indent=2, sort_keys=True, ensure_ascii=False
        ).encode("utf-8") + b"\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.apply-", dir=str(path.parent))
        rollback = {"attempted": False, "succeeded": None}
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            readback_document, _raw = _private_file(path)
            readback = self._validate(family, readback_document)
            if readback["expected_sha256"] != _digest(plan["document"]):
                raise RuntimeError("source configuration read-back did not match")
        except Exception:
            rollback["attempted"] = True
            try:
                restore_fd, restore_name = tempfile.mkstemp(
                    prefix=f".{path.name}.rollback-", dir=str(path.parent)
                )
                try:
                    os.fchmod(restore_fd, 0o600)
                    with os.fdopen(restore_fd, "wb") as handle:
                        handle.write(before_raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(restore_name, path)
                finally:
                    try:
                        Path(restore_name).unlink()
                    except OSError:
                        pass
                rollback["succeeded"] = True
            except Exception:
                rollback["succeeded"] = False
            raise
        finally:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
        return {
            "ok": True,
            "schema": "fleet_source_config_receipt_v1",
            "plan_id": plan_id,
            "family": family,
            "apply_mode": "restart-required",
            "restart_required": True,
            "restart_performed": False,
            "rollback": rollback,
            "readback": readback,
        }
