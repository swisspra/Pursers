"""Runtime diagnostics and the guarded Central health endpoint."""

from __future__ import annotations

import json
import logging
import os
import resource
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

MACHINE_LOGGER_NAME = "pursers_central.machine"


def _machine_logger() -> logging.Logger:
    """Return a non-propagating logger whose records stay one physical line."""
    logger = logging.getLogger(MACHINE_LOGGER_NAME)
    if not any(getattr(handler, "_pursers_machine", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._pursers_machine = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


MACHINE_LOGGER = _machine_logger()


def _fd_snapshot() -> dict[str, int | float | None]:
    """Return bounded process FD pressure without failing diagnostics."""
    count: int | None = None
    for candidate in ("/dev/fd", "/proc/self/fd"):
        try:
            count = len(os.listdir(candidate))
        except OSError:
            continue
        else:
            break
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = None if soft == resource.RLIM_INFINITY else int(soft)
    except (OSError, ValueError):  # pragma: no cover - platform fallback
        limit = None
    pressure = (
        round(count / limit, 4)
        if count is not None and limit not in (None, 0)
        else None
    )
    return {
        "open_file_descriptors": count,
        "soft_file_descriptor_limit": limit,
        "file_descriptor_pressure": pressure,
    }


@dataclass
class RuntimeDiagnostics:
    """Small non-sensitive state shared by tools and health probes."""

    started_monotonic: float = field(default_factory=time.monotonic)
    last_error_class: str | None = None

    def record_error(self, exc: BaseException) -> None:
        self.last_error_class = type(exc).__name__

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(
                max(0.0, time.monotonic() - self.started_monotonic), 3
            ),
            "last_error_class": self.last_error_class,
            **_fd_snapshot(),
        }


def log_runtime_error(
    diagnostics: RuntimeDiagnostics,
    event: str,
    exc: BaseException,
    *,
    include_traceback: bool,
    **fields: Any,
) -> None:
    """Emit one machine-readable line and retain only the error class."""
    diagnostics.record_error(exc)
    payload: dict[str, Any] = {
        "event": event,
        "error_class": type(exc).__name__,
        "error": str(exc),
        **fields,
        **_fd_snapshot(),
    }
    if include_traceback:
        payload["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    MACHINE_LOGGER.error(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _health_counts(service: Any) -> dict[str, int]:
    boards = service.store.iter_documents("boards")
    journals = service.store.iter_documents("journals")
    heads = [max(0, int(row.get("next_seq", 1)) - 1) for row in journals]
    return {
        "board_count": len(boards),
        "journal_head": max(heads, default=0),
    }


def health_response(
    service: Any,
    diagnostics: RuntimeDiagnostics,
    *,
    extra_payload: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
) -> JSONResponse:
    """Build a safe health response and make failures observable."""
    try:
        extra = extra_payload() if callable(extra_payload) else extra_payload
        payload = {
            "status": "ok",
            "store_backend": service.backend,
            **_health_counts(service),
            **diagnostics.snapshot(),
            **dict(extra or {}),
        }
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001 - health boundary must never leak failures
        log_runtime_error(
            diagnostics,
            "healthz_error",
            exc,
            include_traceback=True,
        )
        payload = {
            "status": "error",
            **diagnostics.snapshot(),
        }
        return JSONResponse(
            payload,
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )


def install_health_route(
    app: Any,
    service: Any,
    diagnostics: RuntimeDiagnostics,
    *,
    extra_payload: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
) -> Any:
    """Attach GET /healthz to an MCP Streamable HTTP Starlette app."""

    async def healthz(_request: Request) -> JSONResponse:
        return health_response(
            service,
            diagnostics,
            extra_payload=extra_payload,
        )

    app.add_route("/healthz", healthz, methods=["GET"])
    return app


def create_streamable_http_app(
    mcp: Any,
    service: Any,
    *,
    host: str,
    extra_payload: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
) -> Any:
    """Create the production-shaped stateless app with guarded healthz."""
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=host,
    )
    return install_health_route(
        app,
        service,
        service.diagnostics,
        extra_payload=extra_payload,
    )
