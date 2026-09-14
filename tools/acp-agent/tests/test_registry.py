from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "pursers" / "agent.json"
PINNED_FORMAT = (
    "https://github.com/agentclientprotocol/registry/blob/"
    "134db9fa124273eed9133d0fd26a8d3039ea2f2a/FORMAT.md"
)

PINNED_SCHEMA = ROOT / "tests" / "fixtures" / "acp-registry-agent-134db9fa.schema.json"


def test_registry_entry_matches_pinned_format_and_runtime_contract() -> None:
    entry = json.loads(ENTRY.read_text(encoding="utf-8"))
    schema = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator(
        schema, format_checker=FormatChecker()
    ).validate(entry)

    assert PINNED_FORMAT.endswith("/FORMAT.md")
    assert entry["distribution"] == {
        "uvx": {"package": "pursers-acp==0.1.0", "args": []}
    }
    assert entry["platforms"] == ["darwin-aarch64", "darwin-x86_64"]
    assert entry["launch"] == {
        "command": "uvx",
        "args": ["pursers-acp==0.1.0"],
        "env": {},
    }
    assert entry["authentication"] == {
        "agent_method_id": "pursers-personal-profile",
        "terminal_method_id": "pursers-personal-login",
        "terminal_args": ["--login"],
        "credential": "local Pursers Personal profile",
        "secret_in_registry_or_settings": False,
    }
