"""Executable MCP 2026-07-28 MRTR and dual-era contracts."""

import asyncio
from typing import Annotated, Literal

import pytest
from mcp import Client
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import InvalidSignature
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult, InputRequiredResult
from pydantic import BaseModel


class Choice(BaseModel):
    choice: Literal["yes", "no"]


async def _answer(_context, _params) -> ElicitResult:
    return ElicitResult(action="accept", content={"choice": "yes"})


def _resolver_server(
    keys: list[bytes],
    *,
    ttl: float = 3600.0,
    rendered_message: list[str] | None = None,
) -> MCPServer:
    server = MCPServer(
        "mrtr-contract",
        request_state_security=RequestStateSecurity(keys=keys, ttl=ttl),
    )

    async def ask(message: str) -> Elicit[Choice]:
        return Elicit(rendered_message[0] if rendered_message else message, Choice)

    @server.tool()
    async def decide(
        message: str,
        response: Annotated[ElicitationResult[Choice], Resolve(ask)],
    ) -> str:
        assert isinstance(response, AcceptedElicitation)
        return response.data.choice

    return server


def test_manual_loop_survives_second_worker_and_rejects_tampering() -> None:
    async def exercise() -> None:
        keys = [b"shared-request-state-key-material-0001"]
        first_server = _resolver_server(keys)
        async with Client(first_server, elicitation_callback=_answer) as client:
            first = await client.session.call_tool(
                "decide",
                {"message": "Approve?"},
                allow_input_required=True,
            )
        assert isinstance(first, InputRequiredResult)
        key = next(iter(first.input_requests))
        response = {key: ElicitResult(action="accept", content={"choice": "yes"})}

        second_server = _resolver_server(keys)
        async with Client(second_server, elicitation_callback=_answer) as client:
            tampered = first.request_state[:-1] + (
                "A" if first.request_state[-1] != "A" else "B"
            )
            with pytest.raises(MCPError) as caught:
                await client.session.call_tool(
                    "decide",
                    {"message": "Approve?"},
                    input_responses=response,
                    request_state=tampered,
                    allow_input_required=True,
                )
            assert caught.value.code == -32602
            assert str(caught.value) == "Invalid or expired requestState"

            result = await client.session.call_tool(
                "decide",
                {"message": "Approve?"},
                input_responses=response,
                request_state=first.request_state,
                allow_input_required=True,
            )
        assert not isinstance(result, InputRequiredResult)
        assert result.content[0].text == "yes"

    asyncio.run(exercise())


def test_expired_request_state_uses_frozen_error() -> None:
    async def exercise() -> None:
        keys = [b"shared-request-state-key-material-0002"]
        server = _resolver_server(keys, ttl=0.01)
        async with Client(server, elicitation_callback=_answer) as client:
            first = await client.session.call_tool(
                "decide", {"message": "Approve?"}, allow_input_required=True
            )
            assert isinstance(first, InputRequiredResult)
            await asyncio.sleep(0.03)
            key = next(iter(first.input_requests))
            with pytest.raises(MCPError) as caught:
                await client.session.call_tool(
                    "decide",
                    {"message": "Approve?"},
                    input_responses={
                        key: ElicitResult(
                            action="accept", content={"choice": "yes"}
                        )
                    },
                    request_state=first.request_state,
                    allow_input_required=True,
                )
            assert caught.value.code == -32602
            assert str(caught.value) == "Invalid or expired requestState"

    asyncio.run(exercise())


def test_changed_question_reasks_but_argument_derived_question_completes() -> None:
    async def exercise() -> None:
        keys = [b"shared-request-state-key-material-0003"]
        rendered = ["Question at time one"]
        server = _resolver_server(keys, rendered_message=rendered)
        async with Client(server, elicitation_callback=_answer) as client:
            first = await client.session.call_tool(
                "decide", {"message": "Stable question"}, allow_input_required=True
            )
            assert isinstance(first, InputRequiredResult)
            key = next(iter(first.input_requests))
            rendered[0] = "Question at time two"
            changed = await client.session.call_tool(
                "decide",
                {"message": "Stable question"},
                input_responses={
                    key: ElicitResult(action="accept", content={"choice": "yes"})
                },
                request_state=first.request_state,
                allow_input_required=True,
            )
            assert isinstance(changed, InputRequiredResult)
            assert changed.input_requests[key].params.message == "Question at time two"

        stable = _resolver_server(keys)
        async with Client(stable, elicitation_callback=_answer) as client:
            result = await client.call_tool("decide", {"message": "Stable question"})
            assert result.content[0].text == "yes"

    asyncio.run(exercise())


def test_direct_server_requests_fail_with_exact_dual_era_messages() -> None:
    async def exercise() -> None:
        server = MCPServer("direct-request-contract")

        @server.tool()
        async def direct(ctx: Context) -> str:
            await ctx.elicit("Approve?", Choice)
            return "unreachable"

        async with Client(server) as modern:
            with pytest.raises(MCPError) as caught:
                await modern.call_tool("direct", {})
            assert modern.protocol_version == "2026-07-28"
            assert caught.value.code == -32600
            assert str(caught.value) == (
                "Cannot send 'elicitation/create': this transport context has no "
                "back-channel for server-initiated requests."
            )

        async with Client(server, mode="legacy") as legacy:
            with pytest.raises(MCPError) as caught:
                await legacy.call_tool("direct", {})
            assert legacy.protocol_version == "2025-11-25"
            assert str(caught.value) == "Elicitation not supported"

    asyncio.run(exercise())


def test_resolve_and_hand_built_input_required_cannot_mix() -> None:
    server = MCPServer("invalid-mixed-input")

    async def ask(message: str) -> Elicit[Choice]:
        return Elicit(message, Choice)

    with pytest.raises(InvalidSignature):

        @server.tool()
        async def invalid(
            message: str,
            response: Annotated[ElicitationResult[Choice], Resolve(ask)],
        ) -> dict[str, str] | InputRequiredResult:
            return {"choice": response.action}


def test_hand_built_input_required_is_rejected_on_legacy_connection() -> None:
    async def exercise() -> None:
        server = MCPServer("legacy-input-required")

        @server.tool()
        async def manual() -> dict[str, str] | InputRequiredResult:
            return InputRequiredResult(request_state="still-waiting")

        async with Client(server, mode="legacy") as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("manual", {})
            assert caught.value.code == -32603
            assert str(caught.value) == "Handler returned an invalid result"

    asyncio.run(exercise())
