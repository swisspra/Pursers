"""Persistent, board-pinned backend for the AionUi Home ticket lifecycle UI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable

import door_state
from pursers_client import BoardClient, BoardClientError

CAPABILITIES = {
    "can_work": False,
    "can_review": False,
}


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


def _classify(exc: BaseException) -> dict[str, Any]:
    message = str(exc).casefold()
    if "permission" in message or "denied" in message:
        return _failure("permission_denied", "Central denied this action for the connected principal.")
    if "not found" in message:
        return _failure("ticket_not_found", "The ticket no longer exists. Refresh the list.")
    if "already" in message or "terminal" in message or "conflict" in message:
        return _failure("conflict", "The ticket changed. Refresh its persisted state before retrying.")
    if isinstance(exc, (BoardClientError, OSError, RuntimeError)):
        return _failure("backend_unavailable", "The board connection is unavailable. Restart or reconnect the helper, then retry.", retryable=True)
    return _failure("invalid_input", "Central rejected the request.")


class TicketLifecycleService:
    def __init__(self, client: Any, board: str) -> None:
        self.client = client
        self.board = board

    async def dispatch(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("board") != self.board:
            return _failure("board_mismatch", f"Use the configured board {self.board}.")
        try:
            if operation == "status":
                return {"ok": True, "board": self.board, "connected": True, "actor": self.client.agent_name}
            if operation == "list":
                result = await self.client.ticket_list(include_closed=True, include_archived=True, limit=100)
                return {"ok": True, "board": self.board, "tickets": result.get("tickets", []), "latest_seq": result.get("latest_seq")}
            if operation == "get":
                result = await self.client.ticket_get(payload["ticket_id"])
                return {"ok": True, "board": self.board, "ticket": result["ticket"], "latest_seq": result.get("latest_seq")}
            if operation == "create":
                result = await self.client.ticket_create(
                    None,
                    payload["title"],
                    description=payload["description"],
                    target_url=payload["target_url"],
                    scope=payload["scope"],
                    required_fields=payload["required_fields"],
                    forbidden=payload.get("forbidden"),
                    priority=payload.get("priority", "medium"),
                    tier=payload.get("tier", 2),
                    tags=payload.get("tags"),
                    related_files=payload.get("related_files"),
                    unassigned=True,
                )
                return {"ok": True, "board": self.board, "ticket": result["ticket"]}
            if operation == "cancel":
                result = await self.client.ticket_cancel(payload["ticket_id"], reason=payload.get("reason"))
                ticket = result.get("ticket")
                if ticket is None:
                    ticket = (await self.client.ticket_get(payload["ticket_id"]))["ticket"]
                return {"ok": True, "board": self.board, "ticket": ticket}
            return _failure("unsupported_action", "This Home action is not supported.")
        except (BoardClientError, KeyError, OSError, RuntimeError, ValueError) as exc:
            return _classify(exc)


def _entry(state_dir: str, board: str) -> dict[str, Any]:
    document = door_state.load(door_state.state_path(state_dir))
    candidates = [entry for entry in document["doors"] if entry["b"] == board]
    if not candidates:
        raise ValueError(f"no stored door for board {board}")
    return sorted(candidates, key=lambda entry: entry["r"] != "worker")[0]


def create_sidecar_client(
    entry: dict[str, Any],
    board: str,
    client_factory: Callable[..., Any] = BoardClient,
) -> Any:
    """Build the persistent actor using only Central-supported capabilities."""
    return client_factory(
        entry["u"], entry["t"], board,
        agent_name=f"pursers-home-ticket-lifecycle-{entry['r']}",
        role=entry["r"], capabilities=CAPABILITIES, allow_takeover=True,
    )


async def _serve(args: argparse.Namespace, client_factory: Callable[..., Any] = BoardClient) -> None:
    entry = _entry(args.state_dir, args.board)
    client = create_sidecar_client(entry, args.board, client_factory)
    async with client:
        service = TicketLifecycleService(client, args.board)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or not isinstance(request.get("payload"), dict):
                    raise ValueError
                result = await service.dispatch(str(request.get("operation") or ""), request["payload"])
                response = {"id": request.get("id"), "result": result}
            except (TypeError, ValueError, json.JSONDecodeError):
                response = {"id": None, "result": _failure("invalid_input", "The sidecar request is invalid.")}
            print(json.dumps(response, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pursers-wait-bridge ticket-lifecycle")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--board", required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except (BoardClientError, OSError, RuntimeError, ValueError) as exc:
        print(f"pursers-wait-bridge: ticket lifecycle unavailable: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
