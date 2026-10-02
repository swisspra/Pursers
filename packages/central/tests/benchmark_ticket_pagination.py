"""Reproducible 100/1,000-ticket byte and archive-hydration benchmark."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mcp import Client


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402


async def measure(ticket_count: int) -> dict[str, int]:
    with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as directory:
        root = Path(directory)
        jwks_path = root / "jwks.json"
        jwks_path.write_text('{"keys": []}', encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks_path),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        ):
            mcp, service = central.build_server("localhost", 8765, root / "data")
            principal = central.Principal(
                "PR-benchmark",
                "benchmark",
                frozenset({"board:read", "board:write", "board:review"}),
            )
            original = central.current_principal
            central.current_principal = lambda: principal
            try:
                await mcp.call_tool(
                    "board_join",
                    {
                        "board_id": "pursers",
                        "agent_name": "benchmark-agent",
                        "role": "reviewer",
                        "capabilities": {"can_work": False, "can_review": True},
                    },
                )
                old = central.iso_at(time.time() - 10 * 86_400)

                def seed(document: dict[str, object]) -> None:
                    tickets = document["tickets"]
                    for index in range(ticket_count):
                        ticket_id = f"TK-bench{index:05d}"
                        history = [
                            {"state": "offered", "kind": "work", "cycle": item, "at": old}
                            for item in range(12)
                        ]
                        tickets[ticket_id] = {
                            "ticket_id": ticket_id,
                            "title": f"Benchmark ticket {index}",
                            "description": "x" * 2_000,
                            "status": "closed",
                            "priority": ("high", "medium", "low")[index % 3],
                            "scope": "interactive-no-send",
                            "tier": 2,
                            "created_at": old,
                            "updated_at": old,
                            "closed_at": old,
                            "annotations": [],
                            "dispatch_history": history,
                            "submission_history": [],
                            "review_history": [],
                            "progress_history": [],
                        }

                service.mutate("pursers", seed)
                service.archive_due_tickets("pursers", time.time())

                async with Client(mcp, mode="2026-07-28", cache=None) as client:
                    async def call(**arguments: object) -> dict[str, object]:
                        result = await client.call_tool(
                            "ticket_list", {"board_id": "pursers", **arguments}
                        )
                        return result.structured_content

                    legacy = await call(
                        status="closed", include_closed=True, view="summary", limit=500
                    )
                    legacy_bytes = len(
                        json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    cursor = None
                    page_bytes: list[int] = []
                    hydrated = 0
                    ids: set[str] = set()
                    product_rows: list[dict[str, object]] = []
                    while True:
                        page = await call(
                            status="closed",
                            include_closed=True,
                            view="summary",
                            limit=50,
                            cursor=cursor,
                        )
                        page_bytes.append(
                            len(
                                json.dumps(
                                    page, ensure_ascii=False, separators=(",", ":")
                                ).encode()
                            )
                        )
                        hydrated += int(page["archive_hydrated_count"])
                        product_rows.extend(page["tickets"])
                        ids.update(row["ticket_id"] for row in page["tickets"])
                        cursor = page["next_cursor"]
                        if cursor is None:
                            break
                legacy_shape = {
                    "ok": True,
                    "tickets": product_rows[:500],
                    "count": min(ticket_count, 500),
                    "total_matching": ticket_count,
                    "archived_matching": ticket_count,
                    "filters": legacy["filters"],
                    "latest_seq": legacy["latest_seq"],
                    "view": "summary",
                    "include_dispatch_history": False,
                }
                legacy_emulated_bytes = len(
                    json.dumps(
                        legacy_shape, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
                return {
                    "tickets": ticket_count,
                    "before_legacy_emulated_bytes": legacy_emulated_bytes,
                    "before_legacy_returned": min(ticket_count, 500),
                    "before_archive_hydrated": min(ticket_count, 500),
                    "after_bounded_one_shot_bytes": legacy_bytes,
                    "after_bounded_one_shot_returned": int(legacy["returned_count"]),
                    "after_bounded_one_shot_hydrated": int(legacy["archive_hydrated_count"]),
                    "after_paged_first_bytes": page_bytes[0],
                    "after_paged_max_bytes": max(page_bytes),
                    "after_paged_total_bytes": sum(page_bytes),
                    "after_paged_pages": len(page_bytes),
                    "after_paged_ids": len(ids),
                    "after_paged_archive_hydrated": hydrated,
                }
            finally:
                central.current_principal = original


async def main() -> None:
    results = [await measure(count) for count in (100, 1_000)]
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
