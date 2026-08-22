"""Packaging adapter for the byte-identical approved central spike."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import central


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def main() -> None:
    parser = argparse.ArgumentParser(description="Run On Board Central")
    parser.add_argument("--host", default=_env("ONBOARD_CENTRAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(_env("ONBOARD_CENTRAL_PORT", "8766")))
    parser.add_argument(
        "--data-dir", "--data-root", dest="data_dir", type=Path,
        default=_env("ONBOARD_CENTRAL_DATA_DIR"),
    )
    parser.add_argument("--advance-generation", metavar="BOARD_ID")
    parser.add_argument("--expect-generation-sha256", metavar="HEX")
    args = parser.parse_args()
    if args.data_dir is None:
        parser.error(
            "--data-dir/--data-root or ONBOARD_CENTRAL_DATA_DIR is required"
        )
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if bool(args.advance_generation) != bool(args.expect_generation_sha256):
        parser.error(
            "--advance-generation and --expect-generation-sha256 must be supplied together"
        )

    os.environ["CENTRAL_AUTH_MODE"] = "jwt"
    os.environ["STORE_BACKEND"] = "sqlite"
    os.environ["CENTRAL_ADMISSION"] = "invite"
    if args.advance_generation:
        service = central.CentralBoard(args.data_dir)
        result = service.advance_generation(
            args.advance_generation, args.expect_generation_sha256
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "board_id": result["board_id"],
                    "generation_revision": result["generation_revision"],
                    "generation_token_sha256": result[
                        "generation_token_sha256"
                    ],
                },
                sort_keys=True,
            )
        )
        return
    mcp, _ = central.build_server(args.host, args.port, args.data_dir)
    mcp.run(
        transport="streamable-http", host=args.host, port=args.port,
        streamable_http_path="/mcp", stateless_http=True,
    )
