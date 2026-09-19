#!/usr/bin/env python3
"""Drive deterministic media demos against a disposable Pursers Central.

This program never records a production board.  It creates short-lived
credentials under the caller-provided private root and writes only synthetic
names and text to the disposable board.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization

from pursers_central.quickstart import init_instance, load_profile
from pursers_client import BoardClient, BoardClientError


BOARD_ID = "media-demo"
COORDINATOR = "coordinator"
WORKER = "worker-a"
REVIEWER = "reviewer"
HERO_TICKET = "TK-000000000101"
WAKE_TICKET = "TK-000000000102"
REJECT_TICKET = "TK-000000000103"

CAP_COORDINATOR = {
    "can_work": False,
    "can_review": False,
    "tier_max": 2,
    "max_parallel": 1,
}
CAP_WORKER = {
    "can_work": True,
    "can_review": False,
    "tier_max": 2,
    "max_parallel": 1,
}
CAP_REVIEWER = {
    "can_work": False,
    "can_review": True,
    "tier_max": 2,
    "max_parallel": 1,
}


def emit(step: str, **details: object) -> None:
    """Print a stable, credential-free recording cue."""
    print(json.dumps({"step": step, **details}, sort_keys=True), flush=True)


def token_principal(client_id: str, issuer: str, subject: str) -> str:
    canonical = json.dumps([client_id, issuer, subject], separators=(",", ":"))
    return "PR-" + hashlib.sha256(canonical.encode()).hexdigest()


def mint_token(
    root: Path,
    label: str,
    scopes: str,
    *,
    client_id: str | None = None,
    subject: str | None = None,
    board_bound: bool = True,
) -> tuple[Path, str]:
    profile = load_profile(root)
    jwks = json.loads((root / "jwks.json").read_text(encoding="utf-8"))
    kid = jwks["keys"][0]["kid"]
    key = serialization.load_pem_private_key(
        (root / "signing-key.pem").read_bytes(), password=None
    )
    client_id = client_id or f"media-{label}"
    subject = subject or f"media-{label}-principal"
    now = int(time.time())
    claims = {
            "iss": profile["CENTRAL_JWT_ISSUER"],
            "sub": subject,
            "aud": profile["CENTRAL_JWT_AUDIENCE"],
            "resource": profile["CENTRAL_JWT_AUDIENCE"],
            "scope": scopes,
            "client_id": client_id,
            "iat": now,
            "nbf": now - 5,
            "exp": now + 3_600,
    }
    if board_bound:
        claims["pursers_board"] = BOARD_ID
    value = jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )
    path = root / f"{label}.jwt"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path, token_principal(
        client_id, profile["CENTRAL_JWT_ISSUER"], subject
    )


def prepare(root: Path, port: int) -> None:
    init_instance(root, port=port, board_id=BOARD_ID)
    coordinator_path, _coordinator_principal = mint_token(
        root,
        "coordinator",
        "board:read board:write board:review board:coordinate",
        client_id="pursers-central-quickstart",
        subject="local-board-owner",
        board_bound=False,
    )
    worker_path, worker_principal = mint_token(
        root, "worker", "board:read board:write"
    )
    reviewer_path, reviewer_principal = mint_token(
        root, "reviewer", "board:read board:review"
    )
    summary = {
        "board_id": BOARD_ID,
        "central_url": f"http://127.0.0.1:{port}/mcp",
        "dashboard_token_file": coordinator_path.name,
        "worker_token_file": worker_path.name,
        "reviewer_token_file": reviewer_path.name,
        "worker_principal_id": worker_principal,
        "reviewer_principal_id": reviewer_principal,
    }
    (root / "media-demo.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "media-demo.json").chmod(0o600)
    emit("prepared", board_id=BOARD_ID, central_port=port)


def client(
    url: str,
    token_path: Path,
    name: str,
    role: str,
    capabilities: dict[str, object],
) -> BoardClient:
    return BoardClient(
        url,
        token_path.read_text(encoding="utf-8").strip(),
        BOARD_ID,
        agent_name=name,
        role=role,
        capabilities=capabilities,
        allow_takeover=True,
    )


@asynccontextmanager
async def open_clients(root: Path, url: str):
    async with client(
        url,
        root / "admin.jwt",
        COORDINATOR,
        "worker",
        CAP_COORDINATOR,
    ):
        pass
    async with client(
        url,
        root / "coordinator.jwt",
        COORDINATOR,
        "coordinator",
        CAP_COORDINATOR,
    ) as coordinator:
        summary = json.loads(
            (root / "media-demo.json").read_text(encoding="utf-8")
        )
        for principal, role in (
            (summary["worker_principal_id"], "member"),
            (summary["reviewer_principal_id"], "reviewer"),
        ):
            try:
                await coordinator._call(
                    "board_member_add",
                    {
                        "agent_name": COORDINATOR,
                        "principal_id": principal,
                        "role": role,
                    },
                )
            except BoardClientError as exc:
                if "already a board member" not in str(exc):
                    raise
        async with client(
            url, root / "worker.jwt", WORKER, "worker", CAP_WORKER
        ) as worker:
            async with client(
                url,
                root / "reviewer.jwt",
                REVIEWER,
                "reviewer",
                CAP_REVIEWER,
            ) as reviewer:
                await coordinator.board_dispatch_policy_set(
                    offer_ttl_s=120,
                    broadcast_reoffer_s=120,
                    second_opinion=True,
                    fallback_broadcast=True,
                )
                registry = json.dumps(
                    {
                        "schema_version": 1,
                        "projects": {
                            "media-demo": {
                                "board_id": BOARD_ID,
                                "status": "active",
                                "fleet": True,
                                "repository_url": "https://example.invalid/media-demo",
                                "work_dir": "/PATH/TO/DEMO",
                            }
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                await coordinator.board_state_update("project_registry", registry)
                yield coordinator, worker, reviewer


async def create_ticket(
    coordinator: BoardClient,
    ticket_id: str,
    title: str,
) -> None:
    await coordinator.ticket_create(
        ticket_id,
        title,
        description="Synthetic demo work on a disposable local board.",
        scope="interactive",
        required_fields=["files_changed", "test-output", "observations"],
        forbidden=["real customer data", "credentials"],
        priority="high",
        tags=["demo", "synthetic"],
        target_url="https://example.invalid/media-demo",
        unassigned=True,
        prefer_agents=[WORKER],
    )


async def hero(root: Path, url: str, delay: float) -> None:
    async with open_clients(root, url) as (coordinator, worker, reviewer):
        emit("hero-ready", ticket_id=HERO_TICKET)
        await asyncio.sleep(delay)
        await create_ticket(coordinator, HERO_TICKET, "Validate release checklist")
        emit("hero-offered", ticket_id=HERO_TICKET)
        await asyncio.sleep(delay)
        await worker.ticket_claim(HERO_TICKET)
        emit("hero-claimed", ticket_id=HERO_TICKET, lease="active")
        await asyncio.sleep(delay)
        await worker.ticket_submit(
            HERO_TICKET,
            summary="Evidence is ready for independent review.",
            files_changed=["src/checklist.py", "tests/test_checklist.py"],
            notes=(
                "commit: 0123456789abcdef0123456789abcdef01234567\n"
                "test-output: 12 passed in 0.42s\n"
                "observations: synthetic fixture only"
            ),
        )
        emit("hero-submitted", ticket_id=HERO_TICKET, test_output="12 passed")
        await asyncio.sleep(delay)
        await reviewer.ticket_review_claim(HERO_TICKET)
        emit("hero-reviewing", ticket_id=HERO_TICKET)
        await asyncio.sleep(delay)
        await reviewer.ticket_review(
            HERO_TICKET,
            "approve",
            review_notes="Evidence and tests verified independently.",
        )
        emit("hero-approved", ticket_id=HERO_TICKET)
        await asyncio.sleep(delay)


async def wake(root: Path, url: str, delay: float) -> None:
    async with open_clients(root, url) as (coordinator, worker, _reviewer):
        cursor = (await worker.board_snapshot(limit=10))["latest_seq"]
        wait_bridge = Path(__file__).resolve().parents[1] / "wait-bridge"
        sys.path.insert(0, str(wait_bridge))
        from pursers_wait_server import _a2a_wait_impl
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpx2").setLevel(logging.WARNING)

        async def wait_for_offer() -> dict[str, object]:
            return await _a2a_wait_impl(
                worker,
                since_seq={BOARD_ID: cursor},
                timeout_s=10,
                only_mine=True,
                agent_name=WORKER,
                boards=[BOARD_ID],
                wait_for="claimable",
            )

        waiter = asyncio.create_task(wait_for_offer())
        await asyncio.sleep(0.5)
        emit("wake-waiting", state="blocked-on-push")
        await asyncio.sleep(delay)
        await create_ticket(coordinator, WAKE_TICKET, "Refresh dependency snapshot")
        result = await asyncio.wait_for(waiter, timeout=10)
        event = result["events"][0]
        emit(
            "wake-returned",
            ticket_id=event["ticket_id"],
            event_kind=event["kind"],
            transport=result["mode"],
            reason=result["reason"],
        )
        await asyncio.sleep(delay)
        await coordinator.ticket_cancel(WAKE_TICKET, reason="Wake demo complete.")


async def reject_fix(root: Path, url: str, delay: float) -> None:
    async with open_clients(root, url) as (coordinator, worker, reviewer):
        await create_ticket(coordinator, REJECT_TICKET, "Check export formatting")
        await worker.ticket_claim(REJECT_TICKET)
        await worker.ticket_submit(
            REJECT_TICKET,
            summary="First pass is ready.",
            files_changed=["src/export.py"],
            notes="test-output: 8 passed\nobservations: synthetic fixture only",
        )
        await reviewer.ticket_review_claim(REJECT_TICKET)
        await reviewer.ticket_review(
            REJECT_TICKET,
            "reject",
            review_notes="One edge case needs coverage.",
            fix_instructions="Add the empty-input regression test.",
        )
        emit("fix-rejected", ticket_id=REJECT_TICKET)
        await asyncio.sleep(delay)
        await worker.ticket_claim(REJECT_TICKET)
        await worker.ticket_submit(
            REJECT_TICKET,
            summary="Regression coverage added.",
            files_changed=["src/export.py", "tests/test_export.py"],
            notes="test-output: 9 passed\nobservations: synthetic fixture only",
        )
        await reviewer.ticket_review_claim(REJECT_TICKET)
        await reviewer.ticket_review(
            REJECT_TICKET,
            "approve",
            review_notes="Fix and regression test verified.",
        )
        emit("fix-approved", ticket_id=REJECT_TICKET)
        await asyncio.sleep(delay)


async def drive(root: Path, scenario: str, delay: float) -> None:
    profile = load_profile(root)
    url = profile["CENTRAL_JWT_AUDIENCE"]
    if scenario == "seed":
        async with open_clients(root, url):
            pass
        emit("seeded", board_id=BOARD_ID)
        return
    selected = {"hero": hero, "wake": wake, "reject-fix": reject_fix}[scenario]
    await selected(root, url, delay)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, required=True)
    prepare_parser.add_argument("--port", type=int, required=True)
    drive_parser = subparsers.add_parser("drive")
    drive_parser.add_argument("--root", type=Path, required=True)
    drive_parser.add_argument(
        "--scenario", choices=("seed", "hero", "wake", "reject-fix"), required=True
    )
    drive_parser.add_argument("--delay", type=float, default=2.0)
    return result


def main() -> None:
    args = parser().parse_args()
    root = args.root.expanduser().resolve()
    if args.command == "prepare":
        prepare(root, args.port)
    else:
        asyncio.run(drive(root, args.scenario, args.delay))


if __name__ == "__main__":
    main()
