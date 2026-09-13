"""Packaging adapter for the byte-identical approved central spike."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import uvicorn

from . import central
from .runtime_health import create_streamable_http_app


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _acquire_data_lock(
    parser: argparse.ArgumentParser, data_dir: Path
) -> central.CentralDataLock:
    lock = central.CentralDataLock(data_dir)
    try:
        lock.__enter__()
    except (OSError, RuntimeError) as exc:
        parser.error(f"cannot use data directory {data_dir}: {exc}")
    return lock


def main() -> None:
    parser = argparse.ArgumentParser(description="Run On Board Central")
    parser.add_argument("--host", default=_env("ONBOARD_CENTRAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(_env("ONBOARD_CENTRAL_PORT", "8766")))
    parser.add_argument(
        "--data-dir", "--data-root", dest="data_dir", type=Path,
        default=_env("ONBOARD_CENTRAL_DATA_DIR"),
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default=_env("ONBOARD_CENTRAL_LOG_LEVEL", "info"),
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
    lock = _acquire_data_lock(parser, args.data_dir)
    try:
        mcp, service = central.build_server(args.host, args.port, args.data_dir)
        app = create_streamable_http_app(mcp, service, host=args.host)
        bind_url = f"http://{args.host}:{args.port}/mcp"
        health_url = f"http://{args.host}:{args.port}/healthz"
        print(
            "Pursers Central starting: "
            f"bind={bind_url} data_dir={args.data_dir} health={health_url}",
            file=sys.stderr,
            flush=True,
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            server_header=False,
            access_log=False,
        )
    finally:
        lock.__exit__(*sys.exc_info())


if __name__ == "__main__":
    main()
