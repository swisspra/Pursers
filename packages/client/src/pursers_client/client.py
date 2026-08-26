"""Agent-facing async client for the synthetic On Board central service."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import SubscriptionLost


class BoardClientError(RuntimeError):
    pass


class ScrubRejectedError(BoardClientError):
    def __init__(self, fields: list[str], rules: list[str]):
        self.fields = tuple(fields)
        self.rules = tuple(rules)
        super().__init__(f"write rejected by scrub policy: {', '.join(self.rules)}")


DEFAULT_EVENT_KINDS = frozenset(
    {"ticket_created", "ticket_status_changed", "ticket_assigned"}
)
KNOWN_EVENT_KINDS = DEFAULT_EVENT_KINDS | {"memory_written"}
GENERATION_META_KEY = "io.onboard/expected-generation"
# Cleanup is best-effort after this bound so a broken transport cannot wedge a
# host shutdown or mask the original __aenter__ failure indefinitely.
TRANSPORT_CLOSE_TIMEOUT_S = 2.0
STATUS_ICONS = {
    "open": "📭",
    "claimed": "📌",
    "in_progress": "🔧",
    "creating_report": "📝",
    "submitted": "📤",
    "reviewing": "🔍",
    "in_review": "📤",
    "closed": "✅",
    "rejected": "❌",
    "canceled": "🚫",
    "terminated": "⛔",
}
PRIORITY_ICONS = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}


def _subscription_loss(exc: BaseException) -> SubscriptionLost | None:
    if isinstance(exc, SubscriptionLost):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _subscription_loss(nested)
            if found:
                return found
    return None


def _contains_exception(exc: BaseException, expected: type[BaseException]) -> bool:
    if isinstance(exc, expected):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_exception(item, expected) for item in exc.exceptions)
    return False


def _retryable_connection_error(exc: BaseException) -> bool:
    if _subscription_loss(exc):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_retryable_connection_error(item) for item in exc.exceptions)
    module = type(exc).__module__
    text = str(exc).lower()
    transport_words = ("connection", "stream ended", "disconnected", "refused")
    return (
        module.startswith(("httpx2", "httpcore2"))
        or (module.startswith("mcp") and any(word in text for word in transport_words))
        or "connection closed" in text
    )


@dataclass(frozen=True)
class JoinedIdentity:
    board_id: str
    agent_id: str
    principal_id: str
    agent_name: str
    role: str


class BoardClient:
    def __init__(
        self,
        url: str,
        token: str,
        board_id: str,
        *,
        agent_name: str = "pursers-client",
        reconnect_delay_s: float = 0.05,
        claim_ttl_s: int | None = None,
    ):
        self.url = url
        self.token = token
        self.board_id = board_id
        self.agent_name = agent_name
        self.reconnect_delay_s = reconnect_delay_s
        self.claim_ttl_s = claim_ttl_s
        self.identity: JoinedIdentity | None = None
        self.generation_token: str | None = None
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._local_events: list[dict[str, Any]] = []
        self._watched_uris: set[str] = set()

    def _http(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx2.Timeout(10.0, read=None),
            trust_env=False,
        )

    async def _close_transport(self) -> None:
        """Close the active transport for at most TRANSPORT_CLOSE_TIMEOUT_S."""
        stack = self._stack
        self._stack = None
        self._client = None
        if stack is None:
            return
        close_timeout = asyncio.timeout(TRANSPORT_CLOSE_TIMEOUT_S)
        try:
            async with close_timeout:
                await stack.aclose()
        except TimeoutError:
            if not close_timeout.expired():
                raise

    async def __aenter__(self) -> "BoardClient":
        self._stack = AsyncExitStack()
        try:
            http = await self._stack.enter_async_context(self._http())
            transport = streamable_http_client(self.url, http_client=http)
            self._client = await self._stack.enter_async_context(
                Client(transport, mode="2026-07-28", cache=None)
            )
            await self.board_join(self.claim_ttl_s)
        except BaseException:
            await self._close_transport()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_transport()

    @staticmethod
    def _decode(result) -> dict[str, Any]:
        if result.is_error:
            raise BoardClientError(str(result.content))
        if result.structured_content:
            value = result.structured_content.get("result", result.structured_content)
        else:
            value = json.loads(result.content[0].text)
        if not isinstance(value, dict):
            raise BoardClientError("server returned a non-object tool result")
        return value

    def _refresh_generation(self, result: dict[str, Any]) -> None:
        """Replace the write generation after a join/onboard refresh.

        Legacy servers omit the additive field, so a successful refresh against
        one clears any previously cached generation and preserves the old call
        shape for subsequent tools.
        """
        token = result.get("generation_token")
        if token is None:
            self.generation_token = None
            return
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or len(token) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
            raise BoardClientError("server returned an invalid generation_token")
        self.generation_token = token

    async def _call_with(self, client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"board_id": self.board_id, **arguments}
        if self.generation_token is None:
            result = await client.call_tool(name, payload)
        else:
            result = await client.call_tool(
                name,
                payload,
                meta={GENERATION_META_KEY: self.generation_token},
            )
        return self._decode(result)

    async def _call_refresh(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Join/onboard without stale generation metadata, then cache the reply."""
        if self._client is None:
            raise RuntimeError("BoardClient is not entered")
        payload = {"board_id": self.board_id, **arguments}
        result = self._decode(await self._client.call_tool(name, payload))
        self._refresh_generation(result)
        return result

    async def _call_refresh_uncached(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a refresh tool without changing shared generation state."""
        if self._client is None:
            raise RuntimeError("BoardClient is not entered")
        payload = {"board_id": self.board_id, **arguments}
        return self._decode(await self._client.call_tool(name, payload))

    async def _call_unscoped(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("BoardClient is not entered")
        return self._decode(await self._client.call_tool(name, arguments))

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("BoardClient is not entered")
        return await self._call_with(self._client, name, arguments)

    def _remember_event(self, result: dict[str, Any]) -> None:
        event = result.get("event")
        if isinstance(event, dict) and event.get("id"):
            self._local_events.append(event)
            if event.get("payload_ref"):
                self._watched_uris.add(event["payload_ref"])

    def watch_resource(self, uri: str) -> None:
        prefix = f"board://{self.board_id}/"
        if not uri.startswith(prefix):
            raise ValueError(f"resource URI must start with {prefix}")
        self._watched_uris.add(uri)

    def watch_ticket(self, ticket_id: str) -> None:
        self.watch_resource(f"board://{self.board_id}/ticket/{ticket_id}")

    async def board_join(
        self,
        claim_ttl_s: int | None = None,
        *,
        agent_platform: str | None = None,
        task_focus: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        selected_name = self.agent_name if agent_name is None else agent_name
        arguments: dict[str, Any] = {"agent_name": selected_name}
        if claim_ttl_s is not None:
            arguments["claim_ttl_s"] = claim_ttl_s
        if agent_platform is not None:
            arguments["agent_platform"] = agent_platform
        if task_focus is not None:
            arguments["task_focus"] = task_focus
        joined = await (
            self._call_refresh("board_join", arguments)
            if agent_name is None
            else self._call_refresh_uncached("board_join", arguments)
        )
        identity = JoinedIdentity(
            joined["board_id"],
            joined["agent_id"],
            joined["principal_id"],
            joined["agent_name"],
            joined["role"],
        )
        if agent_name is None:
            self.identity = identity
        else:
            joined = {**joined, "identity": identity}
        return joined

    async def board_onboard(
        self,
        *,
        claim_ttl_s: int | None = None,
        agent_platform: str | None = None,
        task_focus: str | None = None,
        token_budget: int = 4_000,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "token_budget": token_budget,
        }
        optional = {
            "claim_ttl_s": claim_ttl_s,
            "agent_platform": agent_platform,
            "task_focus": task_focus,
            "ticket_id": ticket_id,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        result = await self._call_refresh("board_onboard", arguments)
        self.identity = JoinedIdentity(
            result["board_id"],
            result["agent_id"],
            result["principal_id"],
            result["agent_name"],
            result["role"],
        )
        return result

    async def board_snapshot(
        self,
        *,
        limit: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if limit is not None:
            arguments["limit"] = limit
        if max_bytes is not None:
            arguments["max_bytes"] = max_bytes
        result = await self._call("board_snapshot", arguments)
        for ticket in result.get("tickets", []):
            payload_ref = ticket.get("payload_ref")
            if payload_ref:
                self.watch_resource(payload_ref)
        return result

    async def board_list(self) -> dict[str, Any]:
        return await self._call_unscoped("board_list", {})

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        result = await self._call("ticket_get", {"ticket_id": ticket_id})
        payload_ref = result.get("ticket", {}).get("payload_ref")
        if payload_ref:
            self.watch_resource(payload_ref)
        return result

    async def ticket_create(
        self,
        ticket_id: str | None,
        title: str,
        *,
        description: str | None = None,
        scope: str | None = None,
        required_fields: list[str] | None = None,
        forbidden: list[str] | None = None,
        priority: str = "medium",
        tags: list[str] | None = None,
        related_files: list[str] | None = None,
        target_url: str | None = None,
        assigned_to: str | None = None,
        unassigned: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "title": title,
            "priority": priority,
            "unassigned": unassigned,
        }
        optional = {
            "ticket_id": ticket_id,
            "description": description,
            "scope": scope,
            "required_fields": required_fields,
            "forbidden": forbidden,
            "tags": tags,
            "related_files": related_files,
            "target_url": target_url,
            "assigned_to": assigned_to,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        result = await self._call("ticket_create", arguments)
        self._remember_event(result)
        return result

    async def ticket_claim(self, ticket_id: str) -> dict[str, Any]:
        self._watched_uris.add(f"board://{self.board_id}/ticket/{ticket_id}")
        result = await self._call("ticket_claim", {"agent_name": self.agent_name, "ticket_id": ticket_id})
        self._remember_event(result)
        return result

    async def ticket_submit(
        self,
        ticket_id: str,
        *,
        summary: str | None = None,
        files_changed: list[str] | None = None,
        notes: str | None = None,
        stay_active: bool = True,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "ticket_id": ticket_id,
            "stay_active": stay_active,
        }
        optional = {
            "summary": summary,
            "files_changed": files_changed,
            "notes": notes,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        result = await self._call("ticket_submit", arguments)
        self._remember_event(result)
        return result

    async def lease_renew(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("lease_renew", {"ticket_id": ticket_id})

    async def board_reap(self) -> dict[str, Any]:
        result = await self._call("board_reap", {})
        for event in result.get("release_events", []):
            if event.get("payload_ref"):
                self.watch_resource(event["payload_ref"])
        return result

    async def ticket_review(
        self,
        ticket_id: str,
        verdict: str,
        *,
        review_notes: str | None = None,
        fix_instructions: str | None = None,
    ) -> dict[str, Any]:
        self._watched_uris.add(f"board://{self.board_id}/ticket/{ticket_id}")
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "ticket_id": ticket_id,
            "verdict": verdict,
        }
        if review_notes is not None:
            arguments["review_notes"] = review_notes
        if fix_instructions is not None:
            arguments["fix_instructions"] = fix_instructions
        result = await self._call("ticket_review", arguments)
        self._remember_event(result)
        return result

    async def ticket_cancel(self, ticket_id: str, *, reason: str | None = None) -> dict[str, Any]:
        arguments = {"agent_name": self.agent_name, "ticket_id": ticket_id}
        if reason is not None:
            arguments["reason"] = reason
        result = await self._call("ticket_cancel", arguments)
        self._remember_event(result)
        return result

    async def ticket_terminate(self, ticket_id: str, *, reason: str | None = None) -> dict[str, Any]:
        arguments = {"agent_name": self.agent_name, "ticket_id": ticket_id}
        if reason is not None:
            arguments["reason"] = reason
        result = await self._call("ticket_terminate", arguments)
        self._remember_event(result)
        return result

    async def ticket_list(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"include_closed": include_closed, "limit": limit}
        if status is not None:
            arguments["status"] = status
        if assigned_to is not None:
            arguments["assigned_to"] = assigned_to
        return await self._call("ticket_list", arguments)

    async def memory_write(
        self,
        title: str,
        content: str,
        scope: str,
        *,
        memory_type: str = "context",
        tags: list[str] | None = None,
        priority: int = 0,
        pinned_summary: str | None = None,
        retracts: str | None = None,
        related_files: list[str] | None = None,
        related_tickets: list[str] | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "title": title,
            "content": content,
            "scope": scope,
            "memory_type": memory_type,
            "priority": priority,
        }
        optional = {
            "tags": tags,
            "pinned_summary": pinned_summary,
            "retracts": retracts,
            "related_files": related_files,
            "related_tickets": related_tickets,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        result = await self._call("memory_write", arguments)
        if result.get("ok") is False and result.get("error") == "write rejected by scrub policy":
            raise ScrubRejectedError(result.get("fields", []), result.get("rules", []))
        self._remember_event(result)
        return result

    async def memory_read(
        self,
        *,
        memory_type: str | None = None,
        tag: str | None = None,
        author: str | None = None,
        since: str | None = None,
        since_minutes: int | None = None,
        pinned_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "pinned_only": pinned_only,
            "limit": limit,
        }
        optional = {
            "memory_type": memory_type,
            "tag": tag,
            "author": author,
            "since": since,
            "since_minutes": since_minutes,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        result = await self._call("memory_read", arguments)
        return result["memories"]

    async def memory_unpin(self, memory_id: str, *, reason: str | None = None) -> dict[str, Any]:
        arguments = {"agent_name": self.agent_name, "memory_id": memory_id}
        if reason is not None:
            arguments["reason"] = reason
        return await self._call("memory_unpin", arguments)

    async def memory_search(
        self,
        query: str,
        *,
        tag: str | None = None,
        author: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query, "limit": limit}
        if tag is not None:
            arguments["tag"] = tag
        if author is not None:
            arguments["author"] = author
        return await self._call("memory_search", arguments)

    async def memory_links(
        self,
        *,
        memory_id: str | None = None,
        ticket_id: str | None = None,
        file: str | None = None,
        author: str | None = None,
        depth: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"depth": depth, "limit": limit}
        optional = {
            "memory_id": memory_id,
            "ticket_id": ticket_id,
            "file": file,
            "author": author,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return await self._call("memory_links", arguments)

    async def memory_checkpoint(
        self,
        summary: str,
        *,
        remaining_tasks: list[str] | None = None,
        files: list[str] | None = None,
        next_steps: list[str] | None = None,
        active_branch: str | None = None,
        blockers: list[str] | None = None,
        scope: str = "project",
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "summary": summary,
            "scope": scope,
        }
        optional = {
            "remaining_tasks": remaining_tasks,
            "files": files,
            "next_steps": next_steps,
            "active_branch": active_branch,
            "blockers": blockers,
        }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return await self._call("memory_checkpoint", arguments)

    async def memory_handoff(
        self,
        summary: str,
        next_steps: list[str],
        *,
        files: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name,
            "summary": summary,
            "next_steps": next_steps,
        }
        if files is not None:
            arguments["files"] = files
        if warnings is not None:
            arguments["warnings"] = warnings
        return await self._call("memory_handoff", arguments)

    async def board_get_briefing(
        self, *, token_budget: int = 4_000, ticket_id: str | None = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"token_budget": token_budget}
        if ticket_id is not None:
            arguments["ticket_id"] = ticket_id
        return await self._call("board_get_briefing", arguments)

    async def board_status(self) -> dict[str, Any]:
        return await self._call("board_status", {})

    async def board_review_policy_set(self, review_policy: str) -> dict[str, Any]:
        """Set the board review policy through Central's admin-gated tool."""
        return await self._call(
            "board_review_policy_set",
            {
                "agent_name": self.agent_name,
                "review_policy": review_policy,
            },
        )

    async def board_state_update(self, key: str, value: str) -> dict[str, Any]:
        return await self._call(
            "board_state_update",
            {"agent_name": self.agent_name, "key": key, "value": value},
        )

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        arguments = {} if key is None else {"key": key}
        return await self._call("board_state_get", arguments)

    async def board_catchup(
        self,
        *,
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
        agent_name: str | None = None,
        max_events: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_name": self.agent_name if agent_name is None else agent_name,
            "cursor": cursor,
            "limit": limit,
            "ack": ack,
        }
        if max_events is not None:
            arguments["max_events"] = max_events
        if max_bytes is not None:
            arguments["max_bytes"] = max_bytes
        result = await self._call("board_catchup", arguments)
        for event in result.get("events", []):
            payload_ref = event.get("payload_ref")
            if payload_ref:
                self.watch_resource(payload_ref)
        return result

    async def cold_discover(self, board_id: str | None = None) -> dict[str, Any]:
        """List visible boards, select this bound board, then snapshot/catchup splice."""
        listed = await self.board_list()
        board_ids = [item["board_id"] for item in listed.get("boards", [])]
        selected = board_id or self.board_id
        if selected not in board_ids:
            raise BoardClientError(
                f"board {selected!r} is not visible; available boards: {board_ids}"
            )
        if selected != self.board_id:
            raise BoardClientError(
                f"client is bound to {self.board_id!r}; create a client for {selected!r}"
            )
        snapshot = await self.board_snapshot()
        splice = await self.board_catchup(
            cursor=int(snapshot["latest_seq"]), limit=100, ack=True
        )
        return {
            "boards": listed["boards"],
            "selected_board_id": selected,
            "snapshot": snapshot,
            "splice": splice,
            "events": splice["events"],
        }

    async def list_tickets(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
    ) -> str:
        """Render the central snapshot in the proto memory_list_tickets shape."""
        snapshot = await self.board_snapshot()
        tickets = [
            (await self.ticket_get(ticket["ticket_id"]))["ticket"]
            for ticket in snapshot.get("tickets", [])
        ]
        agent_names = {
            agent["agent_id"]: agent.get("agent_name", agent["agent_id"])
            for agent in snapshot.get("agents", [])
        }
        if status:
            tickets = [ticket for ticket in tickets if ticket.get("status") == status]
        if assigned_to:
            needle = assigned_to.lower()

            def matches(ticket: dict[str, Any]) -> bool:
                ids = (
                    ticket.get("assigned_to_agent_id"),
                    ticket.get("claimed_by_agent_id"),
                )
                values = [value for value in ids if value]
                values.extend(agent_names.get(value, "") for value in tuple(values))
                return any(needle in value.lower() for value in values)

            tickets = [ticket for ticket in tickets if matches(ticket)]
        if not tickets:
            return "No tickets matching filters."

        lines = [f"# 🎫 Tickets ({len(tickets)})\n"]
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tickets.sort(
            key=lambda item: (
                priority_order.get(item.get("priority", "medium"), 9),
                item["ticket_id"],
            )
        )
        for ticket in tickets:
            ticket_status = ticket.get("status", "unknown")
            priority = ticket.get("priority", "medium")
            assigned_id = ticket.get("assigned_to_agent_id")
            claimed_id = ticket.get("claimed_by_agent_id")
            creator_id = ticket.get("created_by_agent_id")
            assigned = (
                f"→ `{agent_names.get(assigned_id, assigned_id)}`" if assigned_id else "→ any"
            )
            claimed = (
                f" ⚡ `{agent_names.get(claimed_id, claimed_id)}`" if claimed_id else ""
            )
            rejected = int(ticket.get("rejection_count", 0))
            abandoned = int(ticket.get("abandoned_count", 0))
            badges = ""
            if rejected:
                badges += f" (rejected {rejected}x)"
            if abandoned:
                badges += f" (abandoned {abandoned}x)"
            lines.append(
                f"### {STATUS_ICONS.get(ticket_status, '❓')} "
                f"{PRIORITY_ICONS.get(priority, '⚪')} `{ticket['ticket_id']}` — "
                f"{ticket.get('title', '(untitled)')}{badges}"
            )
            lines.append(
                f"*By `{agent_names.get(creator_id, creator_id)}` "
                f"{assigned}{claimed} | {ticket_status}*"
            )
            description = str(ticket.get("description", ""))
            if description:
                lines.append(f"{description[:200]}{'...' if len(description) > 200 else ''}")
            lines.append("\n---\n")
        lines.append(
            "📁 `tickets/` = open queue | `tickets/review/` = submitted | "
            "`tickets/closed/` = done | `tickets/rejected/` = failed"
        )
        return "\n".join(lines)

    async def _drain(
        self,
        client: Client,
        seen: set[str],
        cursor: int | None = None,
        *,
        kinds: frozenset[str],
        only_mine: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            page = await self._call_with(
                client, "board_catchup",
                {"agent_name": self.agent_name, "cursor": cursor, "limit": 100, "ack": False},
            )
            start = page["next_cursor"] - page["scan_count"]
            for event in page["events"]:
                if event["id"] not in seen:
                    seen.add(event["id"])
                    payload_ref = event.get("payload_ref")
                    if payload_ref:
                        self.watch_resource(payload_ref)
                    if await self._event_matches(
                        client, event, kinds=kinds, only_mine=only_mine
                    ):
                        yield event
            if page["scan_count"]:
                await self._call_with(
                    client, "board_catchup",
                    {"agent_name": self.agent_name, "cursor": start, "limit": page["scan_count"], "ack": True},
                )
            cursor = page["next_cursor"]
            if not page["has_more"]:
                return

    async def _event_matches(
        self,
        client: Client,
        event: dict[str, Any],
        *,
        kinds: frozenset[str],
        only_mine: bool,
    ) -> bool:
        if self.identity is None:
            raise RuntimeError("BoardClient has no joined identity")
        if event.get("actor") == self.identity.agent_id:
            return False
        if event.get("kind") not in kinds:
            return False
        if not only_mine:
            return True
        ticket_id = event.get("ticket_id")
        if not ticket_id:
            return self.identity.agent_id in event.get("recipient_identities", [])
        try:
            result = await self._call_with(client, "ticket_get", {"ticket_id": ticket_id})
        except BoardClientError:
            return False
        ticket = result["ticket"]
        mine = self.identity.agent_id
        return (
            ticket.get("assigned_to_agent_id") == mine
            or ticket.get("created_by_agent_id") == mine
            or ticket.get("claimed_by_agent_id") == mine
            or (
                ticket.get("status") == "open"
                and ticket.get("assigned_to_agent_id") is None
            )
        )

    async def events(
        self,
        from_cursor: int | None = None,
        *,
        only_mine: bool = True,
        kinds: Iterable[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open live first, drain/dedup, then reconnect and drain after stream loss."""
        seen: set[str] = set()
        initial_cursor = from_cursor
        selected_kinds = frozenset(kinds) if kinds is not None else DEFAULT_EVENT_KINDS
        unknown = selected_kinds - KNOWN_EVENT_KINDS
        if unknown:
            raise ValueError(f"unknown event kinds: {sorted(unknown)}")
        if not self._watched_uris:
            await self.cold_discover()
        while True:
            try:
                async with self._http() as http:
                    transport = streamable_http_client(self.url, http_client=http)
                    async with Client(transport, mode="2026-07-28", cache=None) as event_client:
                        uris = sorted(self._watched_uris)
                        if not uris:
                            raise BoardClientError("events() requires a known ticket/memory URI")
                        async with event_client.listen(resource_subscriptions=uris) as subscription:
                            honored = {
                                str(uri)
                                for uri in (subscription.honored.resource_subscriptions or ())
                            }
                            missing = set(uris) - honored
                            if missing:
                                raise BoardClientError(
                                    f"server did not honor subscriptions: {sorted(missing)}"
                                )
                            while self._local_events:
                                event = self._local_events.pop(0)
                                seen.add(event["id"])
                            async for event in self._drain(
                                event_client,
                                seen,
                                initial_cursor,
                                kinds=selected_kinds,
                                only_mine=only_mine,
                            ):
                                yield event
                            initial_cursor = None
                            async for _cue in subscription:
                                while self._local_events:
                                    local = self._local_events.pop(0)
                                    seen.add(local["id"])
                                async for event in self._drain(
                                    event_client,
                                    seen,
                                    kinds=selected_kinds,
                                    only_mine=only_mine,
                                ):
                                    yield event
            except BaseException as exc:
                if _contains_exception(exc, GeneratorExit):
                    return
                if not _retryable_connection_error(exc):
                    raise
                await asyncio.sleep(self.reconnect_delay_s)
