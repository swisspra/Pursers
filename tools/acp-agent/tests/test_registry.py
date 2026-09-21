from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "pursers" / "agent.json"
ICON = ROOT / "pursers" / "icon.svg"
PINNED_FORMAT = (
    "https://github.com/agentclientprotocol/registry/blob/"
    "9bd27065d5279bc668191e40413aacd361b8e696/FORMAT.md"
)

PINNED_SCHEMA = ROOT / "tests" / "fixtures" / "acp-registry-agent-134db9fa.schema.json"


def test_registry_entry_matches_pinned_format_and_runtime_contract() -> None:
    entry = json.loads(ENTRY.read_text(encoding="utf-8"))
    schema = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator(schema, format_checker=FormatChecker()).validate(entry)

    assert PINNED_FORMAT.endswith("/FORMAT.md")
    assert entry["distribution"] == {
        "uvx": {"package": "pursers-acp==0.1.1", "args": []}
    }
    assert entry["license"] == "Apache-2.0"
    assert entry["license_url"].endswith("/LICENSE")
    assert not ({"icon", "platforms", "launch", "authentication"} & set(entry))


def test_registry_icon_is_exactly_16px_square_and_uses_current_color() -> None:
    root = ET.fromstring(ICON.read_text(encoding="utf-8"))
    assert root.attrib["width"] == "16"
    assert root.attrib["height"] == "16"
    assert root.attrib["viewBox"] == "0 0 16 16"
    colors = {
        value
        for element in root.iter()
        for key, value in element.attrib.items()
        if key in {"fill", "stroke"}
    }
    assert colors <= {"currentColor", "none", "inherit"}
