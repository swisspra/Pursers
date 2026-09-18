from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import jwt
import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "packages" / "client" / "src"))

from pursers_central.quickstart import MANAGED_FILES, QuickstartError, init_instance
from pursers_client import BoardClient


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_init_creates_private_credentials_and_requires_force(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    created = init_instance(root, port=9123, board_id="local-test")

    assert _mode(root) == 0o700
    assert _mode(created["data"]) == 0o700
    assert all(_mode(root / name) == 0o600 for name in MANAGED_FILES)
    admin = (root / "admin.jwt").read_text(encoding="utf-8").strip()
    worker = (root / "worker.jwt").read_text(encoding="utf-8").strip()
    admin_claims = jwt.decode(admin, options={"verify_signature": False})
    worker_claims = jwt.decode(worker, options={"verify_signature": False})
    assert "pursers_board" not in admin_claims
    assert worker_claims["pursers_board"] == "local-test"
    assert admin_claims["sub"] == worker_claims["sub"]
    original_key = hashlib.sha256((root / "signing-key.pem").read_bytes()).digest()

    with pytest.raises(QuickstartError, match="pass --force"):
        init_instance(root, port=9123, board_id="local-test")
    assert hashlib.sha256((root / "signing-key.pem").read_bytes()).digest() == original_key

    init_instance(root, port=9123, board_id="local-test", force=True)
    assert hashlib.sha256((root / "signing-key.pem").read_bytes()).digest() != original_key


def test_packaged_quickstart_serves_and_worker_creates_and_lists_ticket(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_packaged_quickstart(tmp_path))


async def _exercise_packaged_quickstart(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["TMPDIR"] = str(tmp_path / "tmp")
    Path(environment["TMPDIR"]).mkdir()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(PACKAGE_ROOT / "src"),
            str(REPOSITORY_ROOT / "packages" / "client" / "src"),
        )
    )
    for key in tuple(environment):
        if key.startswith("CENTRAL_") or key.startswith("ONBOARD_CENTRAL_"):
            environment.pop(key)

    command = [sys.executable, "-m", "pursers_central"]
    port = _free_port()
    root = tmp_path / "quickstart"
    initialized = await asyncio.to_thread(
        subprocess.run,
        [*command, "init", str(root), "--port", str(port), "--board", "local"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    admin_token = (root / "admin.jwt").read_text(encoding="utf-8").strip()
    worker_token = (root / "worker.jwt").read_text(encoding="utf-8").strip()
    assert admin_token not in initialized.stdout + initialized.stderr
    assert worker_token not in initialized.stdout + initialized.stderr
    assert str(root / "admin.jwt") in initialized.stdout
    assert str(root / "worker.jwt") in initialized.stdout

    server = subprocess.Popen(
        [*command, "run", str(root), "--log-level", "error"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        health_url = f"http://127.0.0.1:{port}/healthz"
        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = await asyncio.to_thread(urllib.request.urlopen, health_url, timeout=1)
                payload = json.loads(response.read())
                if payload.get("status") == "ok":
                    break
            except Exception as exc:  # pragma: no cover - retained for assertion detail
                last_error = exc
                await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"runtime did not become healthy: {last_error}")

        url = f"http://127.0.0.1:{port}/mcp"
        async with BoardClient(
            url, admin_token, "local", agent_name="local-admin", role="worker"
        ):
            pass
        async with BoardClient(
            url, worker_token, "local", agent_name="local-worker", role="worker"
        ) as client:
            created = await client.ticket_create("TK-quickstart-e2e", "First ticket")
            assert created["ok"] is True
            snapshot = await client.board_snapshot()
            assert [ticket["ticket_id"] for ticket in snapshot["tickets"]] == [
                "TK-quickstart-e2e"
            ]
            listed = await client.board_list()
            assert [board["board_id"] for board in listed["boards"]] == ["local"]
            assert listed["boards"][0]["ticket_count"] == 1
    finally:
        server.terminate()
        try:
            await asyncio.to_thread(server.wait, 10)
        except subprocess.TimeoutExpired:
            server.kill()
            await asyncio.to_thread(server.wait, 10)
