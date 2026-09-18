"""Packaging adapter for the byte-identical approved central spike."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from . import central
from .quickstart import QuickstartError, apply_runtime_profile, init_instance
from .runtime_health import create_streamable_http_app


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_first(*names: str) -> str | None:
    for name in names:
        if (value := _env(name)) is not None:
            return value
    return None


def _env_hosts(*names: str) -> tuple[str, ...]:
    value = _env_first(*names)
    if value is None:
        return ()
    return tuple(host.strip() for host in value.split(",") if host.strip())


def _acquire_data_lock(
    parser: argparse.ArgumentParser, data_dir: Path
) -> central.CentralDataLock:
    lock = central.CentralDataLock(data_dir)
    try:
        lock.__enter__()
    except (OSError, RuntimeError) as exc:
        parser.error(f"cannot use data directory {data_dir}: {exc}")
    return lock


def _serve(argv: Sequence[str] | None = None) -> None:
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
    parser.add_argument(
        "--tls-certfile", "--ssl-certfile",
        dest="tls_certfile",
        type=Path,
        default=_env_first(
            "ONBOARD_CENTRAL_TLS_CERTFILE", "ONBOARD_CENTRAL_SSL_CERTFILE"
        ),
        help="TLS certificate supplied by the operator (requires --tls-keyfile)",
    )
    parser.add_argument(
        "--tls-keyfile", "--ssl-keyfile",
        dest="tls_keyfile",
        type=Path,
        default=_env_first(
            "ONBOARD_CENTRAL_TLS_KEYFILE", "ONBOARD_CENTRAL_SSL_KEYFILE"
        ),
        help="TLS private key supplied by the operator (requires --tls-certfile)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        metavar="HOST",
        help=(
            "additional bare Host name; repeat the flag or set the comma-separated "
            "ONBOARD_CENTRAL_ALLOWED_HOSTS value"
        ),
    )
    parser.add_argument("--advance-generation", metavar="BOARD_ID")
    parser.add_argument("--expect-generation-sha256", metavar="HEX")
    parser.epilog = (
        "For a local first run: pursers-central init DIR, then "
        "pursers-central run DIR."
    )
    args = parser.parse_args(argv)
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
    if bool(args.tls_certfile) != bool(args.tls_keyfile):
        parser.error(
            "--tls-certfile and --tls-keyfile (or their ONBOARD_CENTRAL_* "
            "environment variables) must be supplied together"
        )
    allowed_hosts = tuple(
        args.allowed_host
        or _env_hosts("ONBOARD_CENTRAL_ALLOWED_HOSTS", "CENTRAL_ALLOWED_HOSTS")
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
        try:
            app = create_streamable_http_app(
                mcp,
                service,
                host=args.host,
                allowed_hosts=allowed_hosts,
            )
        except ValueError as exc:
            parser.error(str(exc))
        scheme = "https" if args.tls_certfile else "http"
        bind_url = f"{scheme}://{args.host}:{args.port}/mcp"
        health_url = f"{scheme}://{args.host}:{args.port}/healthz"
        print(
            "Pursers Central starting: "
            f"bind={bind_url} data_dir={args.data_dir} health={health_url}",
            file=sys.stderr,
            flush=True,
        )
        if args.tls_certfile:
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                server_header=False,
                access_log=False,
                ssl_certfile=str(args.tls_certfile),
                ssl_keyfile=str(args.tls_keyfile),
            )
        else:
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


def _init(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="pursers-central init",
        description="Create a private local Central profile and credentials",
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--board", default="pursers-local")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        paths = init_instance(
            args.directory,
            port=args.port,
            board_id=args.board,
            force=args.force,
        )
    except QuickstartError as exc:
        parser.error(str(exc))
    print(f"Pursers Central quickstart initialized: {paths['root']}")
    print(f"profile: {paths['profile.env']}")
    print(f"signing key: {paths['signing-key.pem']}")
    print(f"JWKS: {paths['jwks.json']}")
    print(f"admin token: {paths['admin.jwt']}")
    print(f"worker token: {paths['worker.jwt']}")
    print(f"run: pursers-central run {paths['root']}")


def _run(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="pursers-central run",
        description="Run Central from a generated quickstart profile",
    )
    parser.add_argument("directory", type=Path)
    args, remaining = parser.parse_known_args(argv)
    try:
        apply_runtime_profile(args.directory)
    except QuickstartError as exc:
        parser.error(str(exc))
    _serve(remaining)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "init":
        _init(arguments[1:])
        return
    if arguments and arguments[0] == "run":
        _run(arguments[1:])
        return
    _serve(arguments)


if __name__ == "__main__":
    main()
