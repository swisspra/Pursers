#!/usr/bin/env python3
"""Seed and verify the shared project registry after Central is redeployed.

Run with:
    tools/wait-bridge/.venv/bin/python tools/wait-bridge/seed_project_registry.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from pursers_client import BoardClient


BOARD_ID = "pursers"
PROJECT_REGISTRY_KEY = "project_registry"
def _load_registry() -> dict[str, Any]:
    """Registry values are machine-local (absolute work_dir paths) and must
    never be committed; pass them as a JSON file via PROJECT_REGISTRY_FILE
    or argv[1]."""
    path = os.environ.get("PROJECT_REGISTRY_FILE") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not path:
        raise SystemExit("usage: seed_project_registry.py <registry.json> (or PROJECT_REGISTRY_FILE=...)")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


PROJECT_REGISTRY: dict[str, Any] = _load_registry()


async def seed() -> None:
    token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
    if not token:
        raise RuntimeError("ONBOARD_CENTRAL_TOKEN is not set")
    url = os.environ.get("ONBOARD_CENTRAL_URL", "https://127.0.0.1:8766/mcp")
    agent_name = os.environ.get(
        "ONBOARD_AGENT_NAME", "project-registry-seeder"
    )
    async with BoardClient(
        url,
        token,
        BOARD_ID,
        agent_name=agent_name,
    ) as client:
        value = json.dumps(PROJECT_REGISTRY, separators=(",", ":"))
        await client.board_state_update(PROJECT_REGISTRY_KEY, value)
        readback = await client.board_state_get(PROJECT_REGISTRY_KEY)

    state = readback.get("state", {})
    stored = state.get("value")
    try:
        parsed = json.loads(stored)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("project_registry read-back is not valid JSON") from exc
    if parsed != PROJECT_REGISTRY:
        raise RuntimeError("project_registry read-back does not match seed payload")
    print(json.dumps(parsed, indent=2))


def main() -> None:
    try:
        asyncio.run(seed())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
