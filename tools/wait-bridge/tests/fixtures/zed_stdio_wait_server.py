"""Run the product wait tool over stdio with one injected runtime failure."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))

import pursers_wait_server as wait_server  # noqa: E402


class _Client:
    meter = None


async def _client_for_tool(_ctx: object) -> _Client:
    return _Client()


async def _fail_wait(*_args: object, **_kwargs: object) -> dict[str, Any]:
    raise RuntimeError("TOKEN_PLACEHOLDER /private/host/path")


wait_server._client_for_tool = _client_for_tool
wait_server._a2a_wait_impl = _fail_wait


if __name__ == "__main__":
    wait_server.mcp.run(transport="stdio")
