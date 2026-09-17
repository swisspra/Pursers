"""Installed component and packaged-resource provenance checks."""

from __future__ import annotations

import configparser
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import importlib.util
import json
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


class ArtifactVerificationError(RuntimeError):
    """Raised before importing a component whose installed bytes are unapproved."""


_VERIFIED_SOURCE_SENTINEL = object()
_IMPORT_LOCK = threading.RLock()


def _lock_document() -> dict[str, Any]:
    resource = importlib.resources.files("pursers_personal").joinpath(
        "resources/component-lock.json"
    )
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError("component lock is unavailable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ArtifactVerificationError("component lock schema is unsupported")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _locked_console_scripts(
    name: str,
    members: dict[str, str],
    installed_members: dict[str, str],
) -> set[str]:
    entry_points = [
        relative
        for relative in members
        if relative.endswith(".dist-info/entry_points.txt")
    ]
    if not entry_points:
        return set()
    if len(entry_points) != 1:
        raise ArtifactVerificationError(f"invalid {name} entry point lock")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(
            Path(installed_members[entry_points[0]]).read_text(encoding="utf-8")
        )
    except (configparser.Error, OSError, UnicodeError) as exc:
        raise ArtifactVerificationError(f"invalid {name} entry point lock") from exc
    if not parser.has_section("console_scripts"):
        return set()
    return set(parser.options("console_scripts"))


def _is_locked_console_script(
    relative: str,
    distribution: importlib.metadata.Distribution,
    declared: set[str],
) -> bool:
    if Path(relative).name not in declared:
        return False
    located = Path(distribution.locate_file(relative))
    if located.is_symlink():
        return False
    try:
        candidate = located.resolve(strict=True)
        scripts_path = sysconfig.get_path("scripts")
        if not scripts_path:
            return False
        scripts_dir = Path(scripts_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return candidate.is_file() and candidate.parent == scripts_dir


def verify_component_artifacts(
    names: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify exact installed versions, members, origins, and packaged View bytes."""
    document = _lock_document()
    components = document.get("components")
    if not isinstance(components, dict):
        raise ArtifactVerificationError("component lock has no components")
    selected = set(components) if names is None else set(names)
    if not selected or not selected.issubset(components):
        raise ArtifactVerificationError("unknown component requested")

    verified: dict[str, dict[str, Any]] = {}
    for name in sorted(selected):
        pin = components[name]
        if not isinstance(pin, dict):
            raise ArtifactVerificationError(f"invalid {name} component lock")
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ArtifactVerificationError(f"{name} is not installed") from exc
        if distribution.version != pin.get("version"):
            raise ArtifactVerificationError(f"unsupported {name} version")
        wheel_provenance = "locked-members"
        direct_url_text = distribution.read_text("direct_url.json")
        if direct_url_text:
            try:
                direct_url = json.loads(direct_url_text)
                archive_info = direct_url.get("archive_info", {})
                archive_hash = archive_info.get("hashes", {}).get("sha256")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ArtifactVerificationError(
                    f"{name} direct wheel provenance is invalid"
                ) from exc
            if archive_hash is not None and archive_hash != pin.get("wheel_sha256"):
                raise ArtifactVerificationError(f"unapproved {name} wheel provenance")
            if archive_hash is not None:
                wheel_provenance = "archive-sha256"
        members = pin.get("members")
        if not isinstance(members, dict) or not members:
            raise ArtifactVerificationError(f"{name} member lock is empty")
        installed_members: dict[str, str] = {}
        for relative, expected in sorted(members.items()):
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise ArtifactVerificationError(f"invalid {name} member lock")
            located = Path(distribution.locate_file(relative))
            if located.is_symlink():
                raise ArtifactVerificationError(f"installed {name} member is unsafe")
            candidate = located.resolve()
            if not candidate.is_file():
                raise ArtifactVerificationError(f"installed {name} member is unsafe")
            actual = _digest(candidate)
            if actual != expected:
                raise ArtifactVerificationError(
                    f"installed {name} artifact drifted; component-lock.json is "
                    "release-owned and regenerated by the operator at merge, not "
                    "by a worker seat"
                )
            installed_members[relative] = str(candidate)
        declared_console_scripts = _locked_console_scripts(
            name, members, installed_members
        )
        recorded = {str(item) for item in distribution.files or ()}
        generated_suffixes = (
            ".dist-info/RECORD",
            ".dist-info/INSTALLER",
            ".dist-info/REQUESTED",
            ".dist-info/direct_url.json",
            ".dist-info/uv_cache.json",
        )
        unapproved = {
            relative
            for relative in recorded
            if relative not in members
            and not relative.endswith(generated_suffixes)
            and not (
                "/__pycache__/" in relative and relative.endswith(".pyc")
            )
            and not _is_locked_console_script(
                relative, distribution, declared_console_scripts
            )
        }
        if unapproved:
            raise ArtifactVerificationError(
                f"installed {name} has unapproved distribution members"
            )
        verified[name] = {
            "version": distribution.version,
            "members": installed_members,
            "wheel_provenance": wheel_provenance,
        }

    view = document.get("view")
    if names is None and isinstance(view, dict):
        resource = importlib.resources.files("pursers_personal").joinpath(
            "resources/dashboard.html"
        )
        try:
            payload = resource.read_bytes()
        except OSError as exc:
            raise ArtifactVerificationError("packaged dashboard is unavailable") from exc
        if hashlib.sha256(payload).hexdigest() != view.get("sha256"):
            raise ArtifactVerificationError("packaged dashboard drifted")
    return verified


def import_verified_component(
    distribution_name: str,
    package_name: str,
    module_name: str,
    *,
    package_member: str,
    module_member: str,
) -> ModuleType:
    """Verify approved sources, then import without using adjacent bytecode caches."""
    verified = verify_component_artifacts({distribution_name})[distribution_name]
    members = verified["members"]
    try:
        approved_package = Path(members[package_member]).resolve()
        approved_module = Path(members[module_member]).resolve()
    except KeyError as exc:
        raise ArtifactVerificationError(
            f"{distribution_name} import members are not approved"
        ) from exc
    approved_source_paths = {
        Path(path).resolve()
        for relative, path in members.items()
        if relative.endswith(".py")
    }

    with _IMPORT_LOCK:
        existing_package = sys.modules.get(package_name)
        existing_module = sys.modules.get(module_name)
        if existing_package is not None or existing_module is not None:
            if (
                existing_package is None
                or existing_module is None
                or getattr(existing_package, "__pursers_verified_source__", None)
                is not _VERIFIED_SOURCE_SENTINEL
                or getattr(existing_module, "__pursers_verified_source__", None)
                is not _VERIFIED_SOURCE_SENTINEL
                or Path(str(existing_package.__file__)).resolve() != approved_package
                or Path(str(existing_module.__file__)).resolve() != approved_module
            ):
                raise ArtifactVerificationError(
                    f"{distribution_name} was imported before source verification"
                )
            return existing_module

        package_spec = importlib.util.find_spec(package_name)
        if (
            package_spec is None
            or package_spec.origin is None
            or Path(package_spec.origin).resolve() != approved_package
        ):
            raise ArtifactVerificationError(
                f"{distribution_name} import origin is not approved"
            )

        previous_modules = set(sys.modules)
        previous_prefix = sys.pycache_prefix
        try:
            # A private empty prefix makes CPython compile the verified .py bytes
            # instead of accepting an adjacent timestamp-valid __pycache__ file.
            with tempfile.TemporaryDirectory(
                prefix="pursers-verified-pycache-"
            ) as cache_root:
                sys.pycache_prefix = cache_root
                importlib.invalidate_caches()
                package = importlib.import_module(package_name)
                module = importlib.import_module(module_name)
        except BaseException:
            for name in tuple(sys.modules):
                if (
                    name not in previous_modules
                    and (name == package_name or name.startswith(f"{package_name}."))
                ):
                    sys.modules.pop(name, None)
            raise
        finally:
            sys.pycache_prefix = previous_prefix
            importlib.invalidate_caches()

        if Path(str(package.__file__)).resolve() != approved_package:
            raise ArtifactVerificationError(
                f"{distribution_name} package origin is not approved"
            )
        if Path(str(module.__file__)).resolve() != approved_module:
            raise ArtifactVerificationError(
                f"{distribution_name} module origin is not approved"
            )
        loaded_components: list[ModuleType] = []
        for name, loaded in tuple(sys.modules.items()):
            if name != package_name and not name.startswith(f"{package_name}."):
                continue
            origin = getattr(loaded, "__file__", None)
            if origin is None or Path(str(origin)).resolve() not in approved_source_paths:
                raise ArtifactVerificationError(
                    f"{distribution_name} loaded an unapproved component module"
                )
            loaded_components.append(loaded)
        for loaded in loaded_components:
            setattr(
                loaded,
                "__pursers_verified_source__",
                _VERIFIED_SOURCE_SENTINEL,
            )
        return module


def safe_component_summary() -> dict[str, Any]:
    """Return only versions and approved logical member names, never local paths."""
    verified = verify_component_artifacts()
    return {
        name: {
            "version": value["version"],
            "members": sorted(value["members"]),
            "wheel_provenance": value["wheel_provenance"],
        }
        for name, value in verified.items()
    }
