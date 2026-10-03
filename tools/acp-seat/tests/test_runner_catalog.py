from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


catalog = load_module("runner_catalog")
preset = load_module("runner_preset")


def registry(
    *, command: str = "./demo", binary_target: str = "darwin-aarch64"
) -> bytes:
    return json.dumps(
        {
            "version": "1.0.0",
            "extensions": [],
            "agents": [
                {
                    "id": "demo-agent",
                    "name": "Demo",
                    "version": "2.3.4",
                    "distribution": {
                        "binary": {
                            binary_target: {
                                "archive": "https://example.invalid/demo.tgz",
                                "cmd": command,
                                "args": ["acp", "; echo not-a-shell"],
                                "sha256": "a" * 64,
                            }
                        },
                        "npx": {"package": "demo-agent@2.3.4", "args": ["--acp"]},
                    },
                }
            ],
        }
    ).encode()


def test_refresh_cache_offline_stale_and_outage_preserve_last_good(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "catalog.json"
    view = catalog.refresh_catalog(
        cache, fetch=lambda *_: registry(), now=100.0, timeout_s=1.0
    )
    assert view.stale is False
    assert view.registry_revision.startswith("sha256:")
    original = cache.read_bytes()

    def outage(*_args):
        raise catalog.CatalogError("registry_refresh_failed")

    with pytest.raises(catalog.CatalogError, match="registry_refresh_failed"):
        catalog.refresh_catalog(cache, fetch=outage, now=200.0)
    assert cache.read_bytes() == original
    offline = catalog.load_cached_catalog(cache, now=200.0, max_age_s=50.0)
    assert offline.stale is True
    assert offline.list_agents("darwin-aarch64")[0]["enabled"] is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "registry_malformed"),
        (b'{"version":"1.0.0","agents":{}}', "registry_agents_invalid"),
    ],
)
def test_refresh_rejects_malformed_registry(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    with pytest.raises(catalog.CatalogError, match=message):
        catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: payload)


def test_refresh_rejects_oversized_registry(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="registry_oversized"):
        catalog.refresh_catalog(
            tmp_path / "cache", max_bytes=32, fetch=lambda *_: b"x" * 33
        )


def test_platform_resolution_pin_drift_and_disabled_reason(tmp_path: Path) -> None:
    view = catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: registry())
    linux = view.list_agents("linux-aarch64")[0]
    assert linux["enabled"] is True
    assert linux["distribution_kinds"] == ["npx"]
    with pytest.raises(catalog.CatalogError, match="distribution_unavailable"):
        view.resolve("demo-agent", "2.3.4", "linux-aarch64", "binary")
    with pytest.raises(catalog.CatalogError, match="pin_drift"):
        view.resolve("demo-agent", "2.3.5", "darwin-aarch64")

    binary_only = json.loads(registry())
    del binary_only["agents"][0]["distribution"]["npx"]
    view = catalog.refresh_catalog(
        tmp_path / "binary-cache", fetch=lambda *_: json.dumps(binary_only).encode()
    )
    row = view.list_agents("windows-x86_64")[0]
    assert row["enabled"] is False
    assert row["disabled_reason"] == "unsupported_platform_or_distribution"


def test_argv_contract_never_parses_shell_and_rejects_unsafe_command(
    tmp_path: Path,
) -> None:
    view = catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: registry())
    resolved = view.resolve("demo-agent", "2.3.4", "darwin-aarch64", "binary")
    assert resolved["launch"]["argv"] == ["./demo", "acp", "; echo not-a-shell"]
    assert resolved["distribution"]["integrity"]["digest"] == "a" * 64
    with pytest.raises(catalog.CatalogError, match="registry_binary_cmd_unsafe"):
        catalog.refresh_catalog(
            tmp_path / "unsafe", fetch=lambda *_: registry(command="demo;rm -rf /tmp/x")
        )


def test_selection_lock_is_immutable_and_idempotent(tmp_path: Path) -> None:
    view = catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: registry())
    resolved = view.resolve("demo-agent", "2.3.4", "darwin-aarch64", "npx")
    lock = tmp_path / "selection.json"
    assert catalog.persist_selection_lock(lock, resolved) is True
    assert catalog.persist_selection_lock(lock, resolved) is False
    changed = deepcopy(resolved)
    changed["agent_version"] = "2.3.5"
    changed["distribution"]["source"]["package"] = "demo-agent@2.3.5"
    changed["launch"]["argv"][1] = "demo-agent@2.3.5"
    with pytest.raises(catalog.CatalogError, match="selection_lock_immutable"):
        catalog.persist_selection_lock(lock, changed)
    assert catalog.load_selection_lock(lock) == resolved


@pytest.mark.parametrize(
    ("kind", "package"),
    [
        ("npx", "demo-agent"),
        ("npx", "demo-agent@latest"),
        ("npx", "--yes"),
        ("npx", "@scope/demo-agent@2.3.5"),
        ("uvx", "demo-agent"),
        ("uvx", "demo-agent==latest"),
        ("uvx", "--from"),
    ],
)
def test_registry_rejects_unpinned_mismatched_or_option_like_packages(
    tmp_path: Path, kind: str, package: str
) -> None:
    document = json.loads(registry())
    document["agents"][0]["distribution"] = {kind: {"package": package}}
    with pytest.raises(catalog.CatalogError, match=f"registry_{kind}_package_invalid"):
        catalog.refresh_catalog(
            tmp_path / f"{kind}-cache", fetch=lambda *_: json.dumps(document).encode()
        )


@pytest.mark.parametrize(
    ("kind", "package"),
    [
        ("npx", "demo-agent@2.3.4"),
        ("npx", "@scope/demo-agent@2.3.4"),
        ("uvx", "demo-agent==2.3.4"),
        ("uvx", "demo-agent@2.3.4"),
    ],
)
def test_registry_accepts_exact_official_package_pin_forms(
    tmp_path: Path, kind: str, package: str
) -> None:
    document = json.loads(registry())
    document["agents"][0]["distribution"] = {kind: {"package": package}}
    view = catalog.refresh_catalog(
        tmp_path / f"{kind}-cache", fetch=lambda *_: json.dumps(document).encode()
    )
    resolved = view.resolve("demo-agent", "2.3.4", "darwin-aarch64", kind)
    assert resolved["launch"]["argv"] == [kind, package]


def test_cached_catalog_rejects_content_tampering_under_old_revision(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "catalog.json"
    catalog.refresh_catalog(cache, fetch=lambda *_: registry())
    tampered = json.loads(cache.read_text())
    tampered["agents"][0]["distribution"]["npx"]["package"] = (
        "attacker-controlled@2.3.4"
    )
    cache.write_text(json.dumps(tampered))
    with pytest.raises(catalog.CatalogError, match="cache_revision_mismatch"):
        catalog.load_cached_catalog(cache)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("agent_version", None),
        lambda value: value["distribution"].__setitem__("source", {"package": "--yes"}),
        lambda value: value["distribution"].__setitem__("integrity", "not-valid"),
        lambda value: value["launch"].__setitem__("argv", ["npx", "different@2.3.4"]),
    ],
)
def test_selection_lock_rejects_invalid_or_inconsistent_resolution_on_create_and_load(
    tmp_path: Path, mutate
) -> None:
    view = catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: registry())
    resolved = view.resolve("demo-agent", "2.3.4", "darwin-aarch64", "npx")
    invalid = deepcopy(resolved)
    mutate(invalid)

    with pytest.raises(catalog.CatalogError):
        catalog.persist_selection_lock(tmp_path / "create-lock.json", invalid)

    load_lock = tmp_path / "load-lock.json"
    load_lock.write_text(
        json.dumps({"schema": catalog.LOCK_SCHEMA, "resolved": invalid})
    )
    with pytest.raises(catalog.CatalogError):
        catalog.load_selection_lock(load_lock)


def test_binary_selection_lock_validates_source_integrity_and_launch_consistency(
    tmp_path: Path,
) -> None:
    view = catalog.refresh_catalog(tmp_path / "cache", fetch=lambda *_: registry())
    resolved = view.resolve("demo-agent", "2.3.4", "darwin-aarch64", "binary")
    assert catalog.persist_selection_lock(tmp_path / "valid.json", resolved) is True

    invalid_source = deepcopy(resolved)
    invalid_source["distribution"]["source"] = {"package": "demo@2.3.4"}
    with pytest.raises(catalog.CatalogError):
        catalog.persist_selection_lock(tmp_path / "source.json", invalid_source)

    invalid_integrity = deepcopy(resolved)
    invalid_integrity["distribution"]["integrity"]["digest"] = "a" * 63
    with pytest.raises(catalog.CatalogError):
        catalog.persist_selection_lock(tmp_path / "integrity.json", invalid_integrity)

    invalid_launch = deepcopy(resolved)
    invalid_launch["launch"]["cwd"] = None
    with pytest.raises(catalog.CatalogError):
        catalog.persist_selection_lock(tmp_path / "launch.json", invalid_launch)


def test_legacy_native_preset_roundtrip_preserves_identity_and_profile() -> None:
    legacy = {
        "schema_version": 1,
        "provider": "codex",
        "codex_profile": "work",
        "role": "worker",
        "board_id": "pursers",
        "agent_name": "worker-13",
        "account_ref": "codex:company",
        "config_ref": "codex:work",
        "session_options": {"model": "gpt-5.6-sol"},
    }
    migrated = preset.normalize_preset(legacy)
    assert migrated["seat"] == {
        "agent_name": "worker-13",
        "board_id": "pursers",
        "role": "worker",
    }
    assert migrated["runner"]["codex_profile"] == "work"
    assert (
        preset.normalize_preset(json.loads(preset.dumps_preset(migrated))) == migrated
    )


def test_acp_preset_separates_account_pin_and_session_options() -> None:
    value = {
        "schema": preset.PRESET_SCHEMA,
        "seat": {"agent_name": "worker-13", "board_id": "pursers", "role": "worker"},
        "runner": {
            "kind": "acp",
            "account_ref": "gemini:company",
            "catalog_pin": {
                "agent_id": "gemini-cli",
                "agent_version": "1.2.3",
                "platform": "darwin-aarch64",
                "registry_revision": "sha256:" + "b" * 64,
                "distribution_kind": "npx",
            },
        },
        "session_options": {"model": "gemini-pro", "mode": "plan"},
    }
    assert preset.normalize_preset(value) == value
    assert preset.normalize_preset(json.loads(preset.dumps_preset(value))) == value


def acp_preset() -> dict:
    return {
        "schema": preset.PRESET_SCHEMA,
        "seat": {"agent_name": "worker-13", "board_id": "pursers", "role": "worker"},
        "runner": {
            "kind": "acp",
            "account_ref": "gemini:company",
            "catalog_pin": {
                "agent_id": "gemini-cli",
                "agent_version": "1.2.3",
                "platform": "darwin-aarch64",
                "registry_revision": "sha256:" + "b" * 64,
                "distribution_kind": "npx",
            },
        },
        "session_options": {"model": "gemini-pro"},
    }


@pytest.mark.parametrize("version", ["latest", "stable", "preview"])
def test_acp_preset_rejects_channel_versions_on_create_and_roundtrip(
    version: str,
) -> None:
    value = acp_preset()
    value["runner"]["catalog_pin"]["agent_version"] = version
    with pytest.raises(preset.PresetError, match="catalog_pin_version_invalid"):
        preset.normalize_preset(value)
    with pytest.raises(preset.PresetError, match="catalog_pin_version_invalid"):
        preset.dumps_preset(value)


@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    [
        ("agent_id", "--agent", "catalog_pin_agent_id_invalid"),
        ("platform", "made-up-os", "catalog_pin_platform_invalid"),
        ("distribution_kind", "shell", "catalog_pin_distribution_invalid"),
    ],
)
def test_acp_preset_rejects_invalid_catalog_pin_on_create_and_roundtrip(
    field: str, invalid: str, message: str
) -> None:
    value = acp_preset()
    value["runner"]["catalog_pin"][field] = invalid
    with pytest.raises(preset.PresetError, match=message):
        preset.normalize_preset(value)
    with pytest.raises(preset.PresetError, match=message):
        preset.dumps_preset(value)


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_preset_rejects_recursive_non_finite_session_options(
    non_finite: float,
) -> None:
    value = acp_preset()
    value["session_options"] = {"nested": [{"value": non_finite}]}
    with pytest.raises(preset.PresetError, match="session_options_non_finite"):
        preset.normalize_preset(value)
    with pytest.raises(preset.PresetError, match="session_options_non_finite"):
        preset.dumps_preset(value)


@pytest.mark.parametrize(
    "options",
    [{"api_token": "secret"}, {"config": "/PATH/TO/private.json"}],
)
def test_preset_rejects_secrets_and_private_paths(options: dict[str, str]) -> None:
    legacy = {
        "provider": "goose",
        "role": "worker",
        "board_id": "pursers",
        "agent_name": "worker-13",
        "session_options": options,
    }
    with pytest.raises(preset.PresetError):
        preset.normalize_preset(legacy)
