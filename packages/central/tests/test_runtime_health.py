from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx2
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))
sys.path.insert(0, str(REPOSITORY_ROOT / "packages" / "client" / "src"))

import central
from pursers_client import BoardClient
from runtime_health import (
    MACHINE_LOGGER,
    create_streamable_http_app,
    health_response,
)


def _jwt_fixture(root: Path, audience: str) -> tuple[Path, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public.update({"kid": "runtime-health-test", "alg": "RS256", "use": "sig"})
    jwks = root / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}), encoding="utf-8")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": "https://issuer.example",
            "sub": "runtime-health-principal",
            "aud": audience,
            "resource": audience,
            "scope": "board:read board:write board:review",
            "client_id": "runtime-health-test",
            "iat": now,
            "nbf": now - timedelta(seconds=5),
            "exp": now + timedelta(minutes=10),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "runtime-health-test"},
    )
    return jwks, token


def _fd_count() -> int | None:
    for candidate in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(candidate))
        except OSError:
            continue
    return None


class RuntimeHealthUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temp_dir.name)
        jwks = self.root / "jwks.json"
        jwks.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.principal = central.Principal(
            "PR-runtime-health",
            "runtime-health-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.mcp.call_tool(
            "board_join", {"board_id": "pursers", "agent_name": "health-agent"}
        )
        self.assertFalse(joined.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    def _capture_machine_log(self) -> tuple[io.StringIO, logging.Handler]:
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter("%(message)s"))
        MACHINE_LOGGER.addHandler(handler)
        self.addCleanup(MACHINE_LOGGER.removeHandler, handler)
        return output, handler

    def test_healthz_success_exposes_bounded_runtime_fields(self) -> None:
        response = health_response(self.service, self.service.diagnostics)
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["store_backend"], "sqlite")
        self.assertEqual(payload["board_count"], 1)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertIsNone(payload["last_error_class"])
        self.assertIn("open_file_descriptors", payload)
        self.assertIn("soft_file_descriptor_limit", payload)
        self.assertIn("file_descriptor_pressure", payload)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_healthz_failure_logs_full_single_line_and_safe_payload(self) -> None:
        output, _handler = self._capture_machine_log()
        failure = sqlite3.OperationalError("unable to open database file")

        with patch.object(
            self.service.store, "iter_documents", side_effect=failure
        ):
            response = health_response(self.service, self.service.diagnostics)

        payload = json.loads(response.body)
        lines = output.getvalue().splitlines()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["last_error_class"], "OperationalError")
        self.assertNotIn("unable to open", json.dumps(payload))
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "healthz_error")
        self.assertEqual(record["error_class"], "OperationalError")
        self.assertEqual(record["error"], "unable to open database file")
        self.assertIn("traceback", record)

    async def test_tool_errors_get_unwrapped_machine_record(self) -> None:
        output, _handler = self._capture_machine_log()

        with self.assertRaisesRegex(Exception, "state key not found"):
            await self.mcp.call_tool(
                "board_state_get",
                {"board_id": "pursers", "key": "missing-runtime-health-key"},
            )

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "tool_error")
        self.assertEqual(record["tool"], "board_state_get")
        self.assertEqual(record["error_class"], "ValueError")
        self.assertNotIn("\n", lines[0])
        self.assertEqual(self.service.diagnostics.last_error_class, "ValueError")

    def test_failed_sqlite_mutation_does_not_poison_next_health_read(self) -> None:
        path = self.service.store.path("probe", "rollback.json")

        def fail(_document: dict[str, object]) -> None:
            raise RuntimeError("forced mutation failure")

        with self.assertRaisesRegex(RuntimeError, "forced mutation failure"):
            self.service.store.read_modify_write(path, fail, dict)

        response = health_response(self.service, self.service.diagnostics)
        self.assertEqual(response.status_code, 200)


class RuntimeHealthNetworkStressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temp_dir.name)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.port = int(self.listener.getsockname()[1])
        self.audience = f"http://127.0.0.1:{self.port}/mcp"
        jwks, self.token = _jwt_fixture(self.root, self.audience)
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": self.audience,
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "127.0.0.1", self.port, self.root / "data"
        )
        app = create_streamable_http_app(
            self.mcp, self.service, host="127.0.0.1"
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                access_log=False,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.listener]},
            daemon=True,
        )
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(self.server.started)
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    async def asyncTearDown(self) -> None:
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        if self.thread.is_alive():
            self.server.force_exit = True
            await asyncio.to_thread(self.thread.join, 5)
        self.assertFalse(self.thread.is_alive())
        self.listener.close()
        self.environment.stop()
        self.temp_dir.cleanup()

    async def _forced_disconnect(self) -> None:
        _reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        request = (
            "POST /mcp HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: application/json, text/event-stream\r\n"
            "Content-Length: 4096\r\n\r\n"
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        ).encode()
        writer.write(request)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _ticket_cycle(self, index: int) -> None:
        ticket_id = f"TK-stress-{index:03d}"
        async with BoardClient(
            self.url,
            self.token,
            "stress",
            agent_name=f"stress-agent-{index:03d}",
        ) as client:
            await client.ticket_create(ticket_id, f"stress ticket {index}")
            await client.ticket_claim(ticket_id)
            await client.board_state_update(
                f"stress-{index:03d}", json.dumps({"index": index})
            )
            await client.ticket_submit(ticket_id, summary="stress complete")

    async def test_concurrent_session_churn_disconnects_keep_health_200(self) -> None:
        baseline = _fd_count()

        await asyncio.gather(
            *(self._ticket_cycle(index) for index in range(12)),
            *(self._forced_disconnect() for _ in range(36)),
        )
        await asyncio.sleep(0.5)

        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            trust_env=False,
        ) as client:
            response = await client.get(
                f"http://127.0.0.1:{self.port}/healthz", timeout=5
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["board_count"], 1)
        self.assertGreaterEqual(payload["journal_head"], 36)

        final = _fd_count()
        if baseline is not None and final is not None:
            self.assertLessEqual(final, baseline + 16, (baseline, final))
        print(
            "runtime-health stress: "
            f"ticket_cycles=12 forced_disconnects=36 status={response.status_code} "
            f"baseline_fds={baseline} final_fds={final} "
            f"health_fds={payload['open_file_descriptors']}"
        )
