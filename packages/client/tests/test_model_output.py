import asyncio
import json
from contextlib import asynccontextmanager

from mcp import Client, types

from pursers_client.mcp_proxy import CentralRelay, build_server
from test_mcp_proxy import ExistingIdentityClient, _json_result


def test_compact_model_result_survives_mcp_client_round_trip(tmp_path):
    async def exercise():
        ticket = {"ticket_id": "TK-fixture", "latest_submission": {"test_output": "literal 7 passed"},
                  "submission_history": [{"test_output": "literal 7 passed"}]}
        class Upstream(ExistingIdentityClient):
            async def call_tool(self, name, arguments=None, **kwargs):
                if name == "ticket_get":
                    return _json_result({"ok": True, "ticket": ticket})
                return await super().call_tool(name, arguments, **kwargs)
        upstream = Upstream(["worker-a"], "PR-existing", roles=["worker"])
        upstream.tool = types.Tool(name="ticket_get", inputSchema={"type": "object", "properties": {
            "board_id": {"type": "string"}, "ticket_id": {"type": "string"}, "view": {"type": "string"}}})
        @asynccontextmanager
        async def connect(_token): yield upstream
        token = tmp_path / "token"; token.write_text("fixture")
        relay = CentralRelay(central_url="http://localhost:8766", board="fixture", token_file=token,
            tools_mode="worker", connection_factory=connect)
        async with Client(build_server(relay), mode="2026-07-28", cache=None) as client:
            await client.list_tools()
            result = await client.call_tool("ticket_get", {"ticket_id": "TK-fixture"})
        assert not result.is_error
        assert result.structured_content["ticket"]["latest_submission"]["test_output"] == "literal 7 passed"
        assert "submission_history" not in result.structured_content["ticket"]
        assert json.loads(result.content[0].text) == result.structured_content
    asyncio.run(exercise())


import pytest


@pytest.mark.parametrize("mode", ["default", "all"])
@pytest.mark.parametrize("tool_name,defaults", [
    ("ticket_list", {"view": "summary", "limit": 10, "max_bytes": 32768}),
    ("board_snapshot", {"limit": 10, "max_bytes": 16384}),
])
def test_model_bulk_reads_default_to_bounded_progressive_views(tmp_path, mode, tool_name, defaults):
    async def exercise():
        narrative = "long submission and review evidence " * 2000
        class Upstream(ExistingIdentityClient):
            async def call_tool(self, name, arguments=None, **kwargs):
                if name == tool_name:
                    self.calls.append((name, dict(arguments)))
                    if name == "ticket_list":
                        row = {"ticket_id": "TK-two" if arguments.get("cursor") else "TK-one", "title": "Repair", "status": "open"}
                        if arguments.get("view") != "summary": row["description"] = narrative
                        return _json_result({"ok": True, "tickets": [row], "total_matching": 2,
                            "next_cursor": None if arguments.get("cursor") else "opaque-next",
                            "has_more": not bool(arguments.get("cursor"))})
                    return _json_result({"ok": True, "requested_limit": arguments.get("limit"),
                                         "requested_max_bytes": arguments.get("max_bytes")})
                return await super().call_tool(name, arguments, **kwargs)
        upstream = Upstream(["coordinator"], "PR-existing", roles=["orchestrator"])
        properties = {"board_id": {"type": "string"}, "limit": {"type": "integer", "default": 100}}
        if tool_name == "ticket_list":
            properties.update(view={"type": "string", "default": "work"}, cursor={"type": "string"},
                              max_bytes={"type": "integer", "default": 196608})
        else: properties["max_bytes"] = {"type": "integer", "default": 300000}
        upstream.tool = types.Tool(name=tool_name, inputSchema={"type": "object", "properties": properties})
        @asynccontextmanager
        async def connect(_token): yield upstream
        token = tmp_path/"token"; token.write_text("fixture")
        relay = CentralRelay(central_url="http://localhost:8766", board="fixture", token_file=token,
            tools_mode=mode, connection_factory=connect)
        async with Client(build_server(relay), mode="2026-07-28", cache=None) as client:
            catalog = await client.list_tools()
            tool = next(tool for tool in catalog.tools if tool.name == tool_name)
            for key,value in defaults.items(): assert tool.input_schema["properties"][key]["default"] == value
            result = await client.call_tool(tool_name, {})
            assert not result.is_error
            first = upstream.calls[-1][1]
            for key,value in defaults.items(): assert first[key] == value
            if tool_name == "ticket_list":
                assert "description" not in result.structured_content["tickets"][0]
                assert result.structured_content["next_cursor"] == "opaque-next"
                following = await client.call_tool(tool_name, {"cursor": "opaque-next"})
                assert following.structured_content["tickets"][0]["ticket_id"] == "TK-two"
                assert following.structured_content["has_more"] is False
                explicit = await client.call_tool(tool_name, {"view": "work", "limit": 50})
                assert explicit.structured_content["tickets"][0]["description"] == narrative
                assert len(json.dumps(result.structured_content)) < len(json.dumps(explicit.structured_content)) / 10
            else:
                explicit = await client.call_tool(tool_name, {"limit": 40, "max_bytes": 64000})
                assert explicit.structured_content["requested_limit"] == 40
                assert explicit.structured_content["requested_max_bytes"] == 64000
        assert upstream.tool.input_schema["properties"]["limit"]["default"] == 100
    asyncio.run(exercise())
