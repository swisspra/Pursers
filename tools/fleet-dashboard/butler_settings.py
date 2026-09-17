"""Secret-safe Board Butler settings for the loopback Fleet dashboard."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


MAX_KEY_BYTES = 8_192
MAX_ENDPOINT_CHARS = 300
MAX_MODEL_CHARS = 200
MAX_HEADER_COUNT = 16
MAX_HEADER_VALUE_CHARS = 1_000
DEFAULT_VALIDATION_PATH = "models"
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SECRET_HEADER = re.compile(r"(?:authorization|api[-_]?key|token|secret|cookie)", re.I)
_MANAGED_KEY_REFERENCE = re.compile(r"^file:([A-Za-z0-9._-]{1,160}\.key)$")


class ButlerSettingsError(ValueError):
    """One bounded, key-free settings error safe for the local page."""


@dataclass(frozen=True)
class ValidationResult:
    outcome: str
    message: str
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "message": self.message,
            "http_status": self.http_status,
        }


def _text(value: Any, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ButlerSettingsError(f"{label} must be text")
    clean = value.strip()
    if (not clean and not allow_empty) or len(clean) > limit:
        raise ButlerSettingsError(f"{label} is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in clean):
        raise ButlerSettingsError(f"{label} contains control characters")
    return clean


def validate_endpoint(value: Any) -> str:
    endpoint = _text(value, "endpoint", MAX_ENDPOINT_CHARS).rstrip("/")
    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ButlerSettingsError(
            "endpoint must be an http(s) URL without credentials, query, or fragment"
        )
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ButlerSettingsError("endpoint must use https unless it is loopback")
    return endpoint


def validate_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_HEADER_COUNT:
        raise ButlerSettingsError("extra_headers must be a bounded object")
    result: dict[str, str] = {}
    lowered: set[str] = set()
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not _HEADER_NAME.fullmatch(raw_name):
            raise ButlerSettingsError("extra_headers contains an invalid name")
        if _SECRET_HEADER.search(raw_name):
            raise ButlerSettingsError(
                "secret-bearing headers must use the write-only key field"
            )
        name = raw_name.strip()
        folded = name.casefold()
        if folded in lowered:
            raise ButlerSettingsError("extra_headers names must be unique")
        lowered.add(folded)
        result[name] = _text(
            raw_value, f"extra_headers.{name}", MAX_HEADER_VALUE_CHARS, allow_empty=True
        )
    return result


def validate_request(value: Any) -> dict[str, Any]:
    expected = {
        "endpoint",
        "model",
        "api_key",
        "extra_headers",
        "key_header",
        "key_prefix",
        "validation_path",
        "expected_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ButlerSettingsError("request fields are invalid")
    key_header = _text(value["key_header"], "key_header", 128)
    if not _HEADER_NAME.fullmatch(key_header):
        raise ButlerSettingsError("key_header is invalid")
    key_prefix = _text(value["key_prefix"], "key_prefix", 80, allow_empty=True)
    validation_path = _text(
        value["validation_path"], "validation_path", 500, allow_empty=True
    ) or DEFAULT_VALIDATION_PATH
    parsed_path = urlsplit(validation_path)
    if parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment:
        raise ButlerSettingsError("validation_path must be a relative URL path")
    api_key = value["api_key"]
    if not isinstance(api_key, str) or len(api_key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ButlerSettingsError("api_key is invalid")
    expected_sha256 = value["expected_sha256"]
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ButlerSettingsError("expected_sha256 is invalid")
    headers = validate_headers(value["extra_headers"])
    if key_header.casefold() in {name.casefold() for name in headers}:
        raise ButlerSettingsError("key_header must not duplicate extra_headers")
    return {
        "endpoint": validate_endpoint(value["endpoint"]),
        "model": _text(value["model"], "model", MAX_MODEL_CHARS),
        "api_key": api_key,
        "extra_headers": headers,
        "key_header": key_header,
        "key_prefix": key_prefix,
        "validation_path": validation_path,
        "expected_sha256": expected_sha256,
    }


def reject_readable_credential(settings: Mapping[str, Any], api_key: str) -> None:
    """Reject a credential duplicated into any persisted or readable setting."""
    if not api_key:
        return
    readable = [
        str(settings[name])
        for name in (
            "endpoint",
            "model",
            "key_header",
            "key_prefix",
            "validation_path",
        )
    ]
    for name, value in dict(settings["extra_headers"]).items():
        readable.extend((str(name), str(value)))
    if any(api_key in value for value in readable):
        raise ButlerSettingsError(
            "credential must not appear in persisted or readable settings"
        )


def validate_board_butler_document(value: Any) -> dict[str, Any]:
    """Validate the documented Board Butler envelope before a dashboard write."""
    required = {"schema_version", "global"}
    allowed = required | {"projects", "boards"}
    if (
        not isinstance(value, Mapping)
        or not required <= set(value)
        or not set(value) <= allowed
    ):
        raise ButlerSettingsError("board_butler fields are invalid")
    if value.get("schema_version") != 1:
        raise ButlerSettingsError("board_butler.schema_version must be 1")
    setting_keys = {
        "mode",
        "answer_scope",
        "required_evidence_kinds",
        "ceilings",
        "hold_before_post_s",
        "active_windows",
        "kill_switch",
        "auto_demote",
        "classification",
        "drafting",
    }
    provider_keys = {
        "model",
        "endpoint_ref",
        "key_ref",
        "extra_headers",
        "key_header",
        "key_prefix",
        "validation_path",
    }

    def provider(candidate: Any, path: str) -> None:
        if not isinstance(candidate, Mapping) or not set(candidate) <= provider_keys:
            raise ButlerSettingsError(f"{path} fields are invalid")
        for name in ("model", "endpoint_ref", "key_ref"):
            selected = candidate.get(name)
            if selected is not None and (
                not isinstance(selected, str)
                or not selected.strip()
                or len(selected) > 300
            ):
                raise ButlerSettingsError(f"{path}.{name} is invalid")
        if "extra_headers" in candidate:
            validate_headers(candidate["extra_headers"])
        if "key_header" in candidate:
            selected = candidate["key_header"]
            if not isinstance(selected, str) or not _HEADER_NAME.fullmatch(selected):
                raise ButlerSettingsError(f"{path}.key_header is invalid")
        if "key_prefix" in candidate:
            _text(candidate["key_prefix"], f"{path}.key_prefix", 80, allow_empty=True)
        if "validation_path" in candidate:
            _text(candidate["validation_path"], f"{path}.validation_path", 500)

    def settings(candidate: Any, path: str) -> None:
        if not isinstance(candidate, Mapping) or not set(candidate) <= setting_keys:
            raise ButlerSettingsError(f"{path} fields are invalid")
        if "mode" in candidate and candidate["mode"] not in {"shadow", "active"}:
            raise ButlerSettingsError(f"{path}.mode is invalid")
        for name in ("answer_scope", "ceilings", "auto_demote"):
            if name in candidate and not isinstance(candidate[name], Mapping):
                raise ButlerSettingsError(f"{path}.{name} must be an object")
        if "required_evidence_kinds" in candidate and not isinstance(
            candidate["required_evidence_kinds"], list
        ):
            raise ButlerSettingsError(f"{path}.required_evidence_kinds must be a list")
        if "hold_before_post_s" in candidate and (
            type(candidate["hold_before_post_s"]) is not int
            or not 0 <= candidate["hold_before_post_s"] <= 604_800
        ):
            raise ButlerSettingsError(f"{path}.hold_before_post_s is invalid")
        if "active_windows" in candidate and not isinstance(
            candidate["active_windows"], list
        ):
            raise ButlerSettingsError(f"{path}.active_windows must be a list")
        if "kill_switch" in candidate and type(candidate["kill_switch"]) is not bool:
            raise ButlerSettingsError(f"{path}.kill_switch must be boolean")
        for task in ("classification", "drafting"):
            if task in candidate:
                provider(candidate[task], f"{path}.{task}")

    settings(value["global"], "board_butler.global")
    for collection_name in ("projects", "boards"):
        collection = value.get(collection_name, {})
        if not isinstance(collection, Mapping) or len(collection) > 100:
            raise ButlerSettingsError(f"board_butler.{collection_name} is invalid")
        for name, candidate in collection.items():
            if not isinstance(name, str) or not name or len(name) > 200:
                raise ButlerSettingsError(
                    f"board_butler.{collection_name} name is invalid"
                )
            settings(candidate, f"board_butler.{collection_name}.{name}")
    return copy.deepcopy(dict(value))


def _model_ids(document: Any) -> set[str]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping):
        candidate = document.get("data", document.get("models", []))
        rows = candidate if isinstance(candidate, list) else []
    else:
        rows = []
    result: set[str] = set()
    for row in rows[:10_000]:
        if isinstance(row, str):
            result.add(row)
        elif isinstance(row, Mapping):
            selected = row.get("id", row.get("name", row.get("model")))
            if isinstance(selected, str):
                result.add(selected)
    return result


def validate_provider(
    settings: Mapping[str, Any],
    api_key: str,
    *,
    opener: Callable[..., Any] = urlopen,
    timeout_s: float = 5.0,
) -> ValidationResult:
    """Make one bounded call and return only a fixed, key-free outcome."""
    validation_url = urljoin(
        str(settings["endpoint"]).rstrip("/") + "/",
        str(settings["validation_path"]).lstrip("/"),
    )
    headers = {"Accept": "application/json", **dict(settings["extra_headers"])}
    if api_key:
        prefix = str(settings["key_prefix"])
        headers[str(settings["key_header"])] = f"{prefix} {api_key}".strip()
    request = Request(validation_url, headers=headers, method="GET")
    try:
        with opener(request, timeout=timeout_s) as response:
            status = int(getattr(response, "status", 200))
            payload = response.read(1_000_001)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            return ValidationResult(
                "rejected_credential", "The endpoint rejected the credential.", exc.code
            )
        return ValidationResult(
            "unreachable", "The endpoint returned an unusable response.", exc.code
        )
    except (URLError, TimeoutError, OSError, ValueError):
        return ValidationResult("unreachable", "The endpoint could not be reached.")
    if not 200 <= status < 300 or len(payload) > 1_000_000:
        return ValidationResult(
            "unreachable", "The endpoint returned an unusable response.", status
        )
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ValidationResult(
            "wrong_model", "The selected model was not present in the validation response.", status
        )
    if str(settings["model"]) not in _model_ids(document):
        return ValidationResult(
            "wrong_model", "The selected model was not present in the validation response.", status
        )
    return ValidationResult("reachable", "Endpoint, credential, and model validated.", status)


class ButlerSettingsManager:
    """Manage one CAS-backed config slice and locally versioned key files."""

    def __init__(
        self,
        root: str | Path,
        *,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.opener = opener
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    @staticmethod
    def _global(config: Any) -> dict[str, Any]:
        if not isinstance(config, Mapping):
            return {}
        butler = config.get("board_butler")
        if not isinstance(butler, Mapping):
            return {}
        global_settings = butler.get("global")
        return dict(global_settings) if isinstance(global_settings, Mapping) else {}

    @staticmethod
    def _provider(global_settings: Mapping[str, Any]) -> dict[str, Any]:
        for name in ("drafting", "classification"):
            candidate = global_settings.get(name)
            if isinstance(candidate, Mapping):
                return dict(candidate)
        return {}

    def _managed_reference(self, value: Any) -> Path | None:
        if not isinstance(value, str) or (match := _MANAGED_KEY_REFERENCE.fullmatch(value)) is None:
            return None
        path = self.root / match.group(1)
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root)
            info = resolved.stat()
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        return resolved

    @staticmethod
    def _reference(path: Path) -> str:
        return f"file:{path.name}"

    def _read_key(self, reference: Any) -> str:
        path = self._managed_reference(reference)
        if path is None:
            return ""
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        return value if len(value.encode("utf-8")) <= MAX_KEY_BYTES else ""

    def view(self, config_payload: Mapping[str, Any], central: str) -> dict[str, Any]:
        config = config_payload.get("config")
        global_settings = self._global(config)
        provider = self._provider(global_settings)
        key_ref = provider.get("key_ref")
        key_path = self._managed_reference(key_ref)
        mode = "off" if global_settings.get("kill_switch", True) else "shadow"
        return {
            "schema_version": 1,
            "central": central,
            "mode": mode,
            "endpoint": provider.get("endpoint_ref") or "",
            "model": provider.get("model") or "",
            "extra_headers": (
                dict(provider.get("extra_headers", {}))
                if isinstance(provider.get("extra_headers"), Mapping)
                else {}
            ),
            "key_header": provider.get("key_header") or "Authorization",
            "key_prefix": provider.get("key_prefix") or "Bearer",
            "validation_path": provider.get("validation_path") or DEFAULT_VALIDATION_PATH,
            "key_present": key_path is not None,
            "key_location": key_ref if key_path is not None else None,
            "expected_sha256": config_payload.get("expected_sha256"),
        }

    def _write_key(self, central: str, value: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        name = re.sub(r"[^A-Za-z0-9._-]", "-", central)[:80] or "default"
        path = self.root / f"{name}-{uuid.uuid4().hex}.key"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(
        self,
        config_payload: Mapping[str, Any],
        request: Any,
        central: str,
        save_config: Callable[[dict[str, Any], str | None], Mapping[str, Any]],
    ) -> dict[str, Any]:
        clean = validate_request(request)
        if clean["expected_sha256"] != config_payload.get("expected_sha256"):
            raise ButlerSettingsError("configuration changed; reload before saving")
        current = self.view(config_payload, central)
        config = config_payload.get("config")
        if not isinstance(config, Mapping):
            raise ButlerSettingsError("coordinator config is unavailable")
        api_key = clean["api_key"] or self._read_key(current.get("key_location"))
        reject_readable_credential(clean, api_key)
        validation = validate_provider(clean, api_key, opener=self.opener)
        if validation.outcome != "reachable":
            return {
                **current,
                "saved": False,
                "validation": validation.as_dict(),
            }
        with self._lock:
            new_key_path: Path | None = None
            old_key_path = self._managed_reference(current.get("key_location"))
            if clean["api_key"]:
                new_key_path = self._write_key(central, clean["api_key"])
                key_ref = self._reference(new_key_path)
            else:
                key_ref = current.get("key_location") if old_key_path is not None else None
            updated = copy.deepcopy(dict(config))
            updated.pop("updated_at", None)
            updated.pop("updated_by", None)
            butler = updated.setdefault(
                "board_butler",
                {"schema_version": 1, "global": {}, "projects": {}, "boards": {}},
            )
            if not isinstance(butler, dict):
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise ButlerSettingsError("board_butler config is malformed")
            butler.setdefault("schema_version", 1)
            butler.setdefault("projects", {})
            butler.setdefault("boards", {})
            global_settings = butler.setdefault("global", {})
            if not isinstance(global_settings, dict):
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise ButlerSettingsError("board_butler.global is malformed")
            provider = {
                "model": clean["model"],
                "endpoint_ref": clean["endpoint"],
                "key_ref": key_ref,
                "extra_headers": clean["extra_headers"],
                "key_header": clean["key_header"],
                "key_prefix": clean["key_prefix"],
                "validation_path": clean["validation_path"],
            }
            global_settings["classification"] = copy.deepcopy(provider)
            global_settings["drafting"] = copy.deepcopy(provider)
            try:
                saved = save_config(updated, clean["expected_sha256"])
            except BaseException:
                if new_key_path is not None:
                    new_key_path.unlink(missing_ok=True)
                raise
            if (
                new_key_path is not None
                and old_key_path is not None
                and old_key_path != new_key_path
            ):
                old_key_path.unlink(missing_ok=True)
            timestamp = self.now()
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return {
                "schema_version": 1,
                "central": central,
                "mode": "off" if global_settings.get("kill_switch", True) else "shadow",
                "endpoint": clean["endpoint"],
                "model": clean["model"],
                "extra_headers": clean["extra_headers"],
                "key_header": clean["key_header"],
                "key_prefix": clean["key_prefix"],
                "validation_path": clean["validation_path"],
                "key_present": key_ref is not None,
                "key_location": key_ref,
                "expected_sha256": saved.get("expected_sha256"),
                "saved": True,
                "reload": "next_cycle",
                "validated_at": timestamp.astimezone(timezone.utc).isoformat(),
                "validation": validation.as_dict(),
            }
