"""Public, side-effect-free views of Board Butler configuration.

The runtime parsers remain authoritative.  They pass their validated desired and
effective documents to this module so dashboard adapters can inspect, redact,
export, and compare configuration without resolving credentials or calling a
connector tool.
"""

from __future__ import annotations

import copy
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CONTRACT_ID = "pursers.board_butler.configuration"
CONTRACT_VERSION = 1
MAX_DOCUMENT_BYTES = 1_048_576
REDACTED = "[redacted]"

_PRIVATE_PATH_KEYS = frozenset(
    {
        "auth_file",
        "cwd",
        "executable",
        "file",
        "fleet_clone_dir",
        "profile_file",
        "projects_root",
        "secret_file",
        "token_file",
        "work_dir",
    }
)
_SECRET_VALUE_KEYS = frozenset(
    {"authorization", "cookie", "credential", "password", "token"}
)


def _json_copy(value: Any, path: str = "configuration") -> Any:
    """Return a bounded canonical JSON value or fail with a stable message."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        result = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} must be canonical JSON") from exc
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{path} exceeds the {MAX_DOCUMENT_BYTES}-byte limit")
    return result


def read_private_json(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Read one owned 0600 JSON file without following a symlink.

    ``root`` is optional because existing deployments keep configuration outside
    the checkout.  When supplied, both the lexical and resolved path must remain
    below it.  This function never opens credential references found in the file.
    """
    selected = Path(path)
    if not selected.is_absolute():
        raise ValueError("configuration path must be absolute")
    if root is not None:
        boundary = Path(root)
        if not boundary.is_absolute():
            raise ValueError("configuration root must be absolute")
        try:
            selected.relative_to(boundary)
            selected.resolve(strict=True).relative_to(boundary.resolve(strict=True))
        except (OSError, ValueError):
            raise ValueError("configuration path escapes its configured root") from None
    try:
        info = selected.lstat()
    except OSError as exc:
        raise ValueError("configuration file is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or not 0 < info.st_size <= MAX_DOCUMENT_BYTES
    ):
        raise ValueError(
            "configuration file must be an owned non-symlink mode-0600 regular file"
        )
    try:
        raw = selected.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("configuration file is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("configuration document must be an object")
    return _json_copy(document)


def _is_sensitive(path: tuple[str, ...], key: str, value: Any) -> bool:
    lowered = key.casefold()
    if key in _PRIVATE_PATH_KEYS:
        return isinstance(value, str)
    if lowered in _SECRET_VALUE_KEYS or lowered.endswith(("_password", "_token")):
        return True
    if path and path[0] == "secrets":
        return True
    if "static_headers" in path and lowered in {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }:
        return True
    return False


def redact_configuration(value: Any, _path: tuple[str, ...] = ()) -> Any:
    """Deep-copy configuration while removing credentials and private paths."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            result[key] = (
                REDACTED
                if _is_sensitive(_path, key, item)
                else redact_configuration(item, (*_path, key))
            )
        return result
    if isinstance(value, list):
        return [redact_configuration(item, (*_path, str(index))) for index, item in enumerate(value)]
    return copy.deepcopy(value)


def _diff(
    desired: Any,
    effective: Any,
    path: tuple[str, ...],
    output: list[dict[str, Any]],
) -> None:
    if isinstance(desired, Mapping) and isinstance(effective, Mapping):
        for key in sorted(set(desired) | set(effective), key=str):
            child = (*path, str(key))
            if key not in desired:
                selected = effective[key]
                output.append(
                    {
                        "path": ".".join(child),
                        "change": "added",
                        "effective": (
                            REDACTED
                            if _is_sensitive(path, str(key), selected)
                            else redact_configuration(selected, child)
                        ),
                    }
                )
            elif key not in effective:
                selected = desired[key]
                output.append(
                    {
                        "path": ".".join(child),
                        "change": "removed",
                        "desired": (
                            REDACTED
                            if _is_sensitive(path, str(key), selected)
                            else redact_configuration(selected, child)
                        ),
                    }
                )
            else:
                _diff(desired[key], effective[key], child, output)
        return
    if isinstance(desired, list) and isinstance(effective, list):
        if desired != effective:
            key = path[-1] if path else "configuration"
            sensitive = _is_sensitive(path[:-1], key, desired) or _is_sensitive(
                path[:-1], key, effective
            )
            output.append(
                {
                    "path": ".".join(path),
                    "change": "changed",
                    "desired": REDACTED if sensitive else redact_configuration(desired, path),
                    "effective": REDACTED if sensitive else redact_configuration(effective, path),
                }
            )
        return
    if desired != effective:
        key = path[-1] if path else "configuration"
        sensitive = _is_sensitive(path[:-1], key, desired) or _is_sensitive(
            path[:-1], key, effective
        )
        output.append(
            {
                "path": ".".join(path),
                "change": "changed",
                "desired": REDACTED if sensitive else copy.deepcopy(desired),
                "effective": REDACTED if sensitive else copy.deepcopy(effective),
            }
        )


@dataclass(frozen=True)
class ConfigurationContract:
    """Validated desired/effective snapshot safe for configuration adapters."""

    kind: str
    desired: Mapping[str, Any]
    effective: Mapping[str, Any]
    capabilities: Mapping[str, Any]
    unsupported: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"connector_source", "source_onboarding"}:
            raise ValueError("configuration contract kind is unsupported")
        object.__setattr__(self, "desired", _json_copy(self.desired, "desired"))
        object.__setattr__(self, "effective", _json_copy(self.effective, "effective"))
        object.__setattr__(
            self, "capabilities", _json_copy(self.capabilities, "capabilities")
        )
        if any(not isinstance(item, str) or not item for item in self.unsupported):
            raise ValueError("unsupported capabilities must be named strings")

    def export(self) -> dict[str, Any]:
        """Return a public JSON value. Credential material is never exportable."""
        return {
            "contract": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "kind": self.kind,
            "desired": redact_configuration(self.desired),
            "effective": redact_configuration(self.effective),
            "capabilities": copy.deepcopy(self.capabilities),
            "unsupported": list(self.unsupported),
        }

    def compare(self) -> dict[str, Any]:
        """Compare desired to effective without returning sensitive values."""
        changes: list[dict[str, Any]] = []
        _diff(self.desired, self.effective, (), changes)
        return {
            "contract": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "kind": self.kind,
            "equal": not changes,
            "changes": changes,
        }


def build_contract(
    *,
    kind: str,
    desired: Mapping[str, Any],
    effective: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    unsupported: tuple[str, ...] = (),
) -> ConfigurationContract:
    return ConfigurationContract(
        kind=kind,
        desired=desired,
        effective=effective,
        capabilities=capabilities,
        unsupported=unsupported,
    )
