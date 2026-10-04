from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "source_config.py"
SPEC = importlib.util.spec_from_file_location("fleet_source_config_test", MODULE_PATH)
assert SPEC and SPEC.loader
source_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source_config
SPEC.loader.exec_module(source_config)


def private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)


def connector_document() -> dict:
    return {
        "schema_version": 1,
        "approved_connector_ids": ["connector:issues"],
        "connectors": [
            {
                "connector_id": "connector:issues",
                "enabled": False,
                "transport": "streamable_http",
                "protocol_revision": "2026-07-28",
                "endpoint_ref": "endpoint:issues",
                "secret_ref": "/PRIVATE/issues.secret",
                "tools": [
                    {
                        "name": "issues_list",
                        "effect": "read_only",
                        "replay": "never",
                        "stable_call_id_field": None,
                    }
                ],
                "resources": [],
                "risky_tools": [],
                "denied_tools": [],
                "limits": {
                    "timeout_ms": 30_000,
                    "max_input_bytes": 65_536,
                    "max_output_bytes": 1_000_000,
                    "max_concurrency": 2,
                    "calls_per_minute": 60,
                },
            }
        ],
        "endpoints": {
            "endpoint:issues": {
                "transport": "streamable_http",
                "url": "https://mcp.example.invalid/mcp",
            }
        },
        "sources": [
            {
                "source_id": "issues",
                "connector_id": "connector:issues",
                "enabled": False,
                "list_tool": "issues_list",
                "fixed_args": {"status": "open"},
                "items_path": "issues",
                "field_map": {
                    "external_id": "id",
                    "revision": "revision",
                    "title": "title",
                    "body": "body",
                    "link": "url",
                    "project_hint": "project",
                },
                "routing": {"project_hint_is_registry_key": True},
                "mode": "ask",
                "content_type": "structured",
            }
        ],
    }


def onboarding_document() -> dict:
    return {
        "sources": {
            "issues": {
                "domain": "work",
                "projects_root": "/PRIVATE/projects",
                "auto_onboard": False,
                "per_cycle_cap": 2,
                "retry_limit": 3,
                "retry_backoff_s": 60,
                "repositories": {
                    "service": {
                        "repository_url": "https://example.invalid/org/service.git",
                        "integration_ref": "main",
                    }
                },
                "member_roles": {},
                "activate_delivery_policy": False,
            }
        }
    }


def test_source_store_redacts_previews_and_preserves_private_values(tmp_path: Path) -> None:
    connector = tmp_path / "connectors.json"
    onboarding = tmp_path / "onboarding.json"
    private_json(connector, connector_document())
    private_json(onboarding, onboarding_document())
    store = source_config.SourceConfigurationStore(
        connector, onboarding, board_id="pursers"
    )

    snapshot = store.snapshot("source_connectors")
    encoded = json.dumps(snapshot)
    assert snapshot["status"] == "configurable"
    assert "/PRIVATE/" not in encoded
    assert "[redacted]" in encoded

    candidate = snapshot["desired"]
    candidate["connectors"][0]["enabled"] = True
    plan = store.plan(
        {
            "family": "source_connectors",
            "expected_sha256": snapshot["expected_sha256"],
            "document": candidate,
        }
    )
    assert "/PRIVATE/" not in json.dumps(plan)
    receipt = store.apply(plan["plan_id"], plan["digest"])
    assert receipt["restart_required"] is True
    stored = json.loads(connector.read_text(encoding="utf-8"))
    assert stored["connectors"][0]["enabled"] is True
    assert stored["connectors"][0]["secret_ref"] == "/PRIVATE/issues.secret"


def test_source_store_rejects_stale_plan(tmp_path: Path) -> None:
    connector = tmp_path / "connectors.json"
    onboarding = tmp_path / "onboarding.json"
    private_json(connector, connector_document())
    private_json(onboarding, onboarding_document())
    store = source_config.SourceConfigurationStore(
        connector, onboarding, board_id="pursers"
    )
    snapshot = store.snapshot("source_onboarding")
    plan = store.plan(
        {
            "family": "source_onboarding",
            "expected_sha256": snapshot["expected_sha256"],
            "document": snapshot["effective"],
        }
    )
    changed = onboarding_document()
    changed["sources"]["issues"]["per_cycle_cap"] = 4
    private_json(onboarding, changed)
    with pytest.raises(ValueError, match="changed"):
        store.apply(plan["plan_id"], plan["digest"])
