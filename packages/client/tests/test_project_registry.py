from __future__ import annotations

import json

import pytest

from pursers_client import active_registry_boards, parse_project_registry, registry_work_dirs


def state(value: object) -> dict:
    return {"state": {"value": json.dumps(value)}}


def test_registry_parser_active_boards_and_work_dirs() -> None:
    parsed = parse_project_registry(state({
        "schema_version": 1,
        "projects": {
            "home": {"board_id": "pursers", "work_dir": "/repo/home", "status": "active"},
            "alias": {"board_id": "pursers", "work_dir": "/repo/alias", "status": "active"},
            "other": {"board_id": "fullplatts", "work_dir": "/repo/other", "status": "active"},
            "paused": {"board_id": "paused", "work_dir": "/repo/paused", "status": "paused"},
        },
    }))
    assert active_registry_boards(parsed, "pursers") == ["fullplatts", "pursers"]
    assert registry_work_dirs(parsed)["fullplatts"] == "/repo/other"


@pytest.mark.parametrize("value", [
    {"schema_version": 2, "projects": {}},
    {"schema_version": 1, "projects": {"bad": {"board_id": "x", "work_dir": "relative", "status": "active"}}},
])
def test_registry_parser_rejects_invalid_schema(value: object) -> None:
    with pytest.raises(ValueError, match="project_registry"):
        parse_project_registry(state(value))
