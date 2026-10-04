from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contract_api = load("board_butler_configuration_contract", "configuration_contract.py")
butler = load("board_butler_configuration_contract_runtime", "board_butler.py")
onboarding = load("board_butler_configuration_contract_onboarding", "project_onboarding.py")


def connector_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "approved_connector_ids": ["connector:test"],
        "secrets": {"secret:test": "/private/connector.secret"},
        "connectors": [
            {
                "connector_id": "connector:test",
                "transport": "stdio",
                "protocol_revision": "2026-07-28",
                "endpoint_ref": "endpoint:test",
                "secret_ref": "secret:test",
                "tools_read_only": ["fetch", "count", "preflight"],
                "tools_risky_mutating": ["writeback"],
                "tools_denied": ["admin"],
            }
        ],
        "endpoints": {
            "endpoint:test": {
                "transport": "stdio",
                "executable": "/private/bin/source-server",
                "args": ["--stdio"],
            }
        },
        "sources": [
            {
                "source_id": "issues",
                "connector_id": "connector:test",
                "list_tool": "fetch",
                "fixed_args": {"status": "open"},
                "items_path": "issues",
                "field_map": {
                    "external_id": "key",
                    "revision": "revision",
                    "title": "title",
                    "body": "body",
                    "link": "url",
                    "project_hint": "project",
                },
                "routing": {"project_hint_is_registry_key": True},
                "mode": "ask",
                "observation": {
                    "read_tool": "count",
                    "arguments": {"status": "open"},
                    "count_path": "paging.total",
                },
                "grouping": {"kind": "sonar", "max_in_flight": 4},
                "writeback": {
                    "on": "approved",
                    "tool": "writeback",
                    "arg_template": {
                        "ticket": "{ticket_id}",
                        "sha": "{approved_sha}",
                    },
                    "preflight": {
                        "read_tool": "preflight",
                        "arg_template": {"branch": "{source_branch}"},
                        "refs_path": "refs",
                        "name_path": "name",
                        "sha_path": "sha",
                    },
                },
            }
        ],
    }


def write_private(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)


def test_connector_contract_normalizes_legacy_and_redacts_without_tool_calls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connectors.json"
    write_private(path, connector_document())

    contract = butler.inspect_connector_source_configuration(
        path,
        default_board_id="pursers",
        default_project_id="registry",
        default_actor_id="butler",
    )
    public = contract.export()

    assert public["contract_version"] == 1
    assert public["effective"]["connectors"][0]["enabled"] is True
    assert public["effective"]["connectors"][0]["transport"] == "stdio"
    assert public["effective"]["connectors"][0]["tools"][0]["effect"] == "read_only"
    assert public["effective"]["sources"][0]["content_type"] == "structured"
    assert public["effective"]["sources"][0]["observation"]["max_age_s"] == 120
    encoded = json.dumps(public, sort_keys=True)
    assert "/private/connector.secret" not in encoded
    assert "/private/bin/source-server" not in encoded
    assert contract_api.REDACTED in encoded
    assert "tool_execution" in public["unsupported"]


def test_connector_contract_rejects_stale_version_unknown_fields_and_bad_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connectors.json"
    document = connector_document()
    document["schema_version"] = 0
    write_private(path, document)
    with pytest.raises(butler.ConnectorConfigError, match="schema"):
        butler.inspect_connector_source_configuration(
            path,
            default_board_id="pursers",
            default_project_id="registry",
            default_actor_id="butler",
        )

    document = connector_document()
    document["invented_top_level"] = True
    write_private(path, document)
    with pytest.raises(butler.ConnectorConfigError, match="unknown keys"):
        butler.inspect_connector_source_configuration(
            path,
            default_board_id="pursers",
            default_project_id="registry",
            default_actor_id="butler",
        )

    document = connector_document()
    document["sources"][0]["invented_adapter"] = True
    write_private(path, document)
    with pytest.raises(butler.ConnectorConfigError, match="unknown keys"):
        butler.inspect_connector_source_configuration(
            path,
            default_board_id="pursers",
            default_project_id="registry",
            default_actor_id="butler",
        )

    for tool_shape in (
        {
            "tools": [
                {
                    "name": "fetch",
                    "effect": "read_only",
                    "replay": "never",
                    "stable_call_id_field": None,
                }
            ],
            "risky_tools": [],
            "denied_tools": [],
        },
        {
            "tools_read_only": ["fetch"],
            "tools_risky_mutating": [],
            "tools_denied": [],
        },
    ):
        document = connector_document()
        connector = document["connectors"][0]
        for key in (
            "tools_read_only",
            "tools_risky_mutating",
            "tools_denied",
        ):
            connector.pop(key, None)
        connector.update(tool_shape)
        connector["invented_adapter"] = True
        write_private(path, document)
        with pytest.raises(butler.ConnectorConfigError, match="unknown keys"):
            butler.inspect_connector_source_configuration(
                path,
                default_board_id="pursers",
                default_project_id="registry",
                default_actor_id="butler",
            )

    document = connector_document()
    document["sources"][0]["routing"] = {
        "project_hint_is_registry_key": True,
        "project_map": {"a": "A"},
    }
    write_private(path, document)
    with pytest.raises(butler.ConnectorConfigError, match="exactly one"):
        butler.inspect_connector_source_configuration(
            path,
            default_board_id="pursers",
            default_project_id="registry",
            default_actor_id="butler",
        )


def test_contract_compare_reports_secret_change_without_values() -> None:
    snapshot = contract_api.build_contract(
        kind="connector_source",
        desired={"secrets": {"auth": "/private/one"}, "mode": "ask"},
        effective={"secrets": {"auth": "/private/two"}, "mode": "auto"},
        capabilities={},
    )

    result = snapshot.compare()

    assert result["equal"] is False
    assert {row["path"] for row in result["changes"]} == {"mode", "secrets.auth"}
    encoded = json.dumps(result, sort_keys=True)
    assert "/private/one" not in encoded
    assert "/private/two" not in encoded

    added = contract_api.build_contract(
        kind="connector_source",
        desired={"secrets": {}},
        effective={"secrets": {"auth": "/private/three"}},
        capabilities={},
    ).compare()
    assert added["changes"][0]["effective"] == contract_api.REDACTED
    assert "/private/three" not in json.dumps(added)


def test_contract_redacts_absolute_secret_references_in_export_and_compare(
    tmp_path: Path,
) -> None:
    private_ref = str(tmp_path / "legacy-connector.secret")
    nested_ref = str(tmp_path / "legacy-header.secret")
    document = connector_document()
    document["connectors"][0].update(
        {
            "transport": "streamable_http",
            "secret_ref": private_ref,
        }
    )
    document["endpoints"]["endpoint:test"] = {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:8123/mcp",
        "secret_headers": {
            "Authorization": {"secret_ref": nested_ref, "prefix": "Bearer"}
        },
    }
    path = tmp_path / "connectors.json"
    write_private(path, document)

    snapshot = butler.inspect_connector_source_configuration(
        path,
        default_board_id="pursers",
        default_project_id="registry",
        default_actor_id="butler",
    )

    exported = json.dumps(snapshot.export(), sort_keys=True)
    compared = json.dumps(snapshot.compare(), sort_keys=True)

    for private_path in (
        private_ref,
        nested_ref,
    ):
        assert private_path not in exported
        assert private_path not in compared
    assert contract_api.REDACTED in exported
    assert contract_api.REDACTED in compared


def test_contract_keeps_opaque_secret_reference_ids() -> None:
    snapshot = contract_api.build_contract(
        kind="connector_source",
        desired={"connectors": [{"secret_ref": "secret:connector"}]},
        effective={"connectors": [{"secret_ref": "secret:connector"}]},
        capabilities={},
    )

    assert (
        snapshot.export()["desired"]["connectors"][0]["secret_ref"]
        == "secret:connector"
    )


def test_private_reader_rejects_relative_symlink_and_root_escape(tmp_path: Path) -> None:
    root = tmp_path / "config"
    root.mkdir()
    target = root / "onboarding.json"
    write_private(target, {"sources": {}})
    link = root / "link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="absolute"):
        contract_api.read_private_json(Path("onboarding.json"))
    with pytest.raises(ValueError, match="non-symlink"):
        contract_api.read_private_json(link)
    with pytest.raises(ValueError, match="escapes"):
        contract_api.read_private_json(target, root=tmp_path / "other")


def test_onboarding_contract_normalizes_aliases_defaults_and_repository_mapping(
    tmp_path: Path,
) -> None:
    desired = {
        "sources": {
            "sonarqube": {
                "domain": "work",
                "projects_root": str(tmp_path / "projects"),
                "auto_onboard": False,
                "max_new_projects_per_cycle": 2,
                "repository_map": {
                    "alpha": "https://example.invalid/org/alpha.git"
                },
                "discovery": {"kind": "sonar_ado"},
            }
        }
    }

    contract = onboarding.inspect_source_onboarding_configuration(desired)
    public = contract.export()
    effective = public["effective"]["sources"]["sonarqube"]

    assert effective["per_cycle_cap"] == 2
    assert effective["retry_limit"] == 3
    assert effective["retry_backoff_s"] == 60
    assert effective["repositories"]["alpha"]["integration_ref"] == "main"
    assert effective["discovery"]["refresh_seconds"] == 900
    assert effective["projects_root"] == contract_api.REDACTED
    assert "automatic_connector_enablement" in public["unsupported"]

    invalid = json.loads(json.dumps(desired))
    invalid["sources"]["sonarqube"]["unknown"] = True
    with pytest.raises(ValueError, match="unsupported"):
        onboarding.inspect_source_onboarding_configuration(invalid)
