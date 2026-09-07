from __future__ import annotations

import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_manifest_validates_against_vendored_hub_schema() -> None:
    jsonschema.Draft202012Validator(load("vendor/aion-hub-extension-schema-v0.json")).validate(
        load("aion-extension.json")
    )


def test_manifest_contributes_backend_variants_without_mcp_block() -> None:
    manifest = load("aion-extension.json")
    contributes = manifest["contributes"]
    assert "mcpServers" not in contributes
    variants = {
        (assistant["contextFile"], assistant["presetAgentType"])
        for assistant in contributes["assistants"]
    }
    assert variants == {
        ("contexts/worker.md", "codex"),
        ("contexts/worker.md", "claude"),
        ("contexts/reviewer.md", "codex"),
        ("contexts/reviewer.md", "claude"),
    }


def test_permissions_are_loopback_only_and_moderate() -> None:
    manifest = load("aion-extension.json")
    assert manifest["permissions"]["storage"] is True
    assert set(manifest["permissions"]["network"]["allowedDomains"]) == {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    assert manifest["risk"]["level"] == "moderate"
