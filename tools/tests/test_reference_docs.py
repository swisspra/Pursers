from __future__ import annotations

from pathlib import Path

from tools import generate_reference_docs


def test_reference_docs_match_generated_output() -> None:
    generated = generate_reference_docs.generated_documents()
    assert set(generated) == {
        Path("docs/reference/mcp-tools.md"),
        Path("docs/reference/cli.md"),
        Path("docs/reference/environment.md"),
    }
    for relative, expected in generated.items():
        actual = (generate_reference_docs.ROOT / relative).read_text(encoding="utf-8")
        assert actual == expected, (
            f"{relative} is stale; run python3 tools/generate_reference_docs.py"
        )


def test_generator_metadata_exactly_covers_live_tool_registry() -> None:
    live = {tool.name for tool in generate_reference_docs._load_central_tools()}
    assert generate_reference_docs._tool_names() == live
    assert set(generate_reference_docs.TOOL_SCOPES) == live


def test_ticket_cancel_scope_matches_unconditional_authorization() -> None:
    assert generate_reference_docs._direct_required_scopes()["ticket_cancel"] == (
        "board:write",
    )
    assert generate_reference_docs.TOOL_SCOPES["ticket_cancel"] == (
        "`board:write`; creator/executor/reviewer checks apply"
    )


def test_central_environment_registry_is_covered_by_source_inventory() -> None:
    inventory = generate_reference_docs._environment_inventory()
    assert generate_reference_docs._server_json_environment() <= inventory["Central"]


def test_profile_client_legacy_overrides_are_distinct_from_bridge_inputs() -> None:
    rows = {
        (name, component): (default, meaning)
        for name, component, default, meaning in generate_reference_docs._environment_rows(
            generate_reference_docs._environment_inventory()
        )
    }
    for name in (
        "ONBOARD_AGENT_NAME",
        "ONBOARD_BOARD_ID",
        "ONBOARD_CENTRAL_TOKEN",
        "ONBOARD_CENTRAL_URL",
    ):
        client_default, client_meaning = rows[(name, "Client")]
        bridge_default, bridge_meaning = rows[(name, "Wait bridge")]
        assert client_default == "ignored"
        assert "Legacy override detection only" in client_meaning
        assert "profile-backed" in client_meaning
        assert bridge_default != "ignored"
        assert "Legacy override detection only" not in bridge_meaning
