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


def test_central_environment_registry_is_covered_by_source_inventory() -> None:
    inventory = generate_reference_docs._environment_inventory()
    assert generate_reference_docs._server_json_environment() <= inventory["Central"]
