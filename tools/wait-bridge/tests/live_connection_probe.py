"""Operator-run local socket probe for wait-bridge connection hygiene."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "packages" / "client" / "src"))
sys.path.insert(
    0,
    str(REPOSITORY_ROOT / "packages" / "central" / "src" / "pursers_central"),
)
sys.path.insert(0, str(ROOT))

import central  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402
from runtime_health import create_streamable_http_app  # noqa: E402


def jwt_fixture(root: Path, audience: str) -> tuple[Path, str]:
    private_key = rsa.generate_private_key(
        public_exponent=65_537, key_size=2_048
    )
    public = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public.update({"kid": "connection-probe", "alg": "RS256", "use": "sig"})
    jwks = root / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}), encoding="utf-8")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": "https://issuer.example",
            "sub": "connection-probe",
            "aud": audience,
            "resource": audience,
            "scope": "board:read board:write board:review",
            "client_id": "connection-probe",
            "iat": now,
            "nbf": now - timedelta(seconds=5),
            "exp": now + timedelta(minutes=10),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "connection-probe"},
    )
    return jwks, token


def client_socket_count(port: int) -> int:
    completed = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(os.getpid()), f"-iTCP:{port}"],
        check=False,
        capture_output=True,
        text=True,
    )
    marker = f"->127.0.0.1:{port}"
    return sum(
        marker in line and "ESTABLISHED" in line
        for line in completed.stdout.splitlines()
    )


async def wait_for_stream(service: central.CentralBoard) -> None:
    deadline = time.monotonic() + 2
    while service.active_stream_count < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    if service.active_stream_count < 1:
        raise RuntimeError("probe subscription did not become active")


async def run_probe() -> None:
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        root = Path(temporary)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = int(listener.getsockname()[1])
        url = f"http://127.0.0.1:{port}/mcp"
        jwks, token = jwt_fixture(root, url)
        environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": url,
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
                "PURSERS_CENTRAL_CONNECTION_CAP": "4",
            },
        )
        environment.start()
        server: uvicorn.Server | None = None
        thread: threading.Thread | None = None
        try:
            mcp, service = central.build_server(
                "127.0.0.1", port, root / "data"
            )
            app = create_streamable_http_app(mcp, service, host="127.0.0.1")
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    access_log=False,
                )
            )
            thread = threading.Thread(
                target=server.run,
                kwargs={"sockets": [listener]},
                daemon=True,
            )
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if not server.started:
                raise RuntimeError("local Central did not start")

            meter = wait_server.BridgeStats(root / "bridge-stats.json")
            client = wait_server.MeteredBoardClient(
                url,
                token,
                "pursers",
                agent_name="connection-probe",
                meter=meter,
                allow_takeover=True,
            )
            cursor = 0
            peak_sockets = 0
            async with client:
                with (
                    patch.object(wait_server, "WAIT_MODE", "push"),
                    patch.object(
                        wait_server, "clamp_timeout", return_value=0.15
                    ),
                ):
                    for _cycle in range(20):
                        waiting = asyncio.create_task(
                            wait_server._wait_for_work(
                                client,
                                since_seq=cursor,
                                timeout_s=1,
                                only_mine=True,
                            )
                        )
                        await wait_for_stream(service)
                        peak_sockets = max(
                            peak_sockets, client_socket_count(port)
                        )
                        for connection in list(server.server_state.connections):
                            connection.shutdown()
                        result = await asyncio.wait_for(waiting, timeout=2)
                        cursor = int(result["new_seq"])

            await asyncio.sleep(0.1)
            final_sockets = client_socket_count(port)
            if peak_sockets > 4 or client.connection_limiter.peak > 4:
                raise RuntimeError(
                    "connection cap exceeded: "
                    f"sockets={peak_sockets} logical={client.connection_limiter.peak}"
                )
            print(
                "live-connection-probe: cycles=20 forced_disconnects=20 "
                f"peak_client_sockets={peak_sockets} "
                f"final_client_sockets={final_sockets} "
                f"logical_peak={client.connection_limiter.peak} cap=4"
            )
        finally:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                await asyncio.to_thread(thread.join, 5)
            listener.close()
            environment.stop()


if __name__ == "__main__":
    asyncio.run(run_probe())
