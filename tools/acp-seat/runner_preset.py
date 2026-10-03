"""Portable, secret-free runner preset contract for Pursers seats."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

PRESET_SCHEMA = "pursers_runner_preset_v1"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SENSITIVE_KEY = re.compile(
    r"token|secret|password|credential|api[_-]?key", re.IGNORECASE
)
PRIVATE_PATH = re.compile(r"^(?:/|~[/\\]|[A-Za-z]:[/\\])")


class PresetError(ValueError):
    """A preset is unsafe or incompatible."""


def normalize_preset(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate v1 or migrate the bounded legacy native preset shape."""
    if not isinstance(raw, Mapping):
        raise PresetError("preset_invalid")
    value = dict(raw)
    if value.get("schema") == PRESET_SCHEMA:
        return _validate_v1(value)
    return _migrate_legacy(value)


def dumps_preset(raw: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            normalize_preset(raw),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _validate_v1(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "seat", "runner", "session_options"}:
        raise PresetError("preset_fields_invalid")
    seat = value["seat"]
    if not isinstance(seat, dict) or set(seat) != {"agent_name", "board_id", "role"}:
        raise PresetError("preset_seat_invalid")
    for field in ("agent_name", "board_id", "role"):
        _logical_ref(seat[field], f"seat_{field}")
    if seat["role"] not in {"worker", "reviewer", "verifier"}:
        raise PresetError("preset_role_invalid")
    runner = _runner(value["runner"])
    options = _public_json(value["session_options"], "session_options")
    if not isinstance(options, dict):
        raise PresetError("session_options_invalid")
    return {
        "schema": PRESET_SCHEMA,
        "seat": dict(seat),
        "runner": runner,
        "session_options": options,
    }


def _runner(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or "kind" not in raw:
        raise PresetError("preset_runner_invalid")
    kind = raw["kind"]
    if kind == "native":
        if set(raw) != {
            "kind",
            "provider",
            "account_ref",
            "config_ref",
            "codex_profile",
        }:
            raise PresetError("native_runner_invalid")
        if raw["provider"] not in {"codex", "goose"}:
            raise PresetError("native_provider_invalid")
        for field in ("account_ref", "config_ref"):
            _logical_ref(raw[field], field)
        profile = raw["codex_profile"]
        if raw["provider"] == "codex":
            _logical_ref(profile, "codex_profile")
        elif profile is not None:
            raise PresetError("goose_codex_profile_invalid")
        return dict(raw)
    if kind == "acp":
        fields = {"kind", "account_ref", "catalog_pin"}
        if set(raw) != fields:
            raise PresetError("acp_runner_invalid")
        _logical_ref(raw["account_ref"], "account_ref")
        pin = raw["catalog_pin"]
        expected = {
            "agent_id",
            "agent_version",
            "platform",
            "registry_revision",
            "distribution_kind",
        }
        if not isinstance(pin, dict) or set(pin) != expected:
            raise PresetError("catalog_pin_invalid")
        for field in ("agent_id", "agent_version", "platform", "distribution_kind"):
            _logical_ref(pin[field], field)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pin["registry_revision"])):
            raise PresetError("registry_revision_invalid")
        return {
            "kind": "acp",
            "account_ref": raw["account_ref"],
            "catalog_pin": dict(pin),
        }
    raise PresetError("runner_kind_invalid")


def _migrate_legacy(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "provider",
        "codex_profile",
        "role",
        "board_id",
        "agent_name",
        "account_ref",
        "config_ref",
        "session_options",
    }
    if not set(value) <= allowed or value.get("schema_version") not in {None, 1}:
        raise PresetError("legacy_preset_invalid")
    provider = value.get("provider")
    if provider not in {"codex", "goose"}:
        raise PresetError("legacy_provider_invalid")
    profile = value.get("codex_profile")
    if provider == "codex" and not profile:
        raise PresetError("legacy_codex_profile_missing")
    migrated = {
        "schema": PRESET_SCHEMA,
        "seat": {
            "agent_name": value.get("agent_name"),
            "board_id": value.get("board_id"),
            "role": value.get("role"),
        },
        "runner": {
            "kind": "native",
            "provider": provider,
            "account_ref": value.get("account_ref", f"{provider}:default"),
            "config_ref": value.get("config_ref", f"{provider}:default"),
            "codex_profile": profile if provider == "codex" else None,
        },
        "session_options": value.get("session_options", {}),
    }
    return _validate_v1(migrated)


def _logical_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise PresetError(f"{field}_invalid")
    return value


def _public_json(value: Any, field: str, depth: int = 0) -> Any:
    if depth > 8:
        raise PresetError(f"{field}_too_deep")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 4096 or PRIVATE_PATH.match(value) or "\x00" in value:
            raise PresetError(f"{field}_private_or_invalid")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise PresetError(f"{field}_too_large")
        return [_public_json(item, field, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 128:
            raise PresetError(f"{field}_too_large")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE_KEY.search(key):
                raise PresetError(f"{field}_sensitive_key")
            result[key] = _public_json(item, field, depth + 1)
        return result
    raise PresetError(f"{field}_invalid")
