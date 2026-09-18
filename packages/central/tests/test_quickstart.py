from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import textwrap
import time
import urllib.request
import venv
from pathlib import Path

import jwt
import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from pursers_central.quickstart import MANAGED_FILES, QuickstartError, init_instance


QUICKSTART_COMMANDS = {
    REPOSITORY_ROOT / "packages" / "central" / "README.md": (
        "python -m pip install pursers-central",
        "pursers-central init ./pursers-local",
        "pursers-central run ./pursers-local",
    ),
    REPOSITORY_ROOT / "packages" / "pursers" / "README.md": (
        "python -m pip install pursers",
        "pursers-central init ./pursers-local",
        "pursers-central run ./pursers-local",
    ),
}


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _quickstart_commands(readme: Path) -> tuple[str, ...]:
    match = re.search(
        r"## Quickstart\s+.*?```bash\n(?P<commands>.*?)```",
        readme.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert match is not None, f"missing Quickstart bash block in {readme}"
    return tuple(line for line in match.group("commands").splitlines() if line)


def _installed_quickstart(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    for readme, expected in QUICKSTART_COMMANDS.items():
        assert _quickstart_commands(readme) == expected

    packaging_environment = os.environ.copy()
    packaging_environment.pop("PYTHONPATH", None)
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    projects = (
        REPOSITORY_ROOT / "packages" / "client",
        REPOSITORY_ROOT / "packages" / "central",
        REPOSITORY_ROOT / "packages" / "personal",
        REPOSITORY_ROOT / "packages" / "import",
        REPOSITORY_ROOT / "packages" / "pursers",
    )
    for project in projects:
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(project),
            ],
            cwd=tmp_path,
            env=packaging_environment,
        )

    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=True).create(runtime)
    scripts = runtime / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    console = scripts / ("pursers-central.exe" if os.name == "nt" else "pursers-central")
    wheels = {
        name: next(wheel_dir.glob(f"{name}-*.whl"))
        for name in ("pursers_central", "pursers")
    }
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--find-links",
            str(wheel_dir),
            str(wheels["pursers_central"]),
        ],
        cwd=tmp_path,
        env=packaging_environment,
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--find-links",
            str(wheel_dir),
            str(wheels["pursers"]),
        ],
        cwd=tmp_path,
        env=packaging_environment,
    )
    assert console.is_file()

    environment = os.environ.copy()
    environment["TMPDIR"] = str(tmp_path / "tmp")
    Path(environment["TMPDIR"]).mkdir()
    environment.pop("PYTHONPATH", None)
    for key in tuple(environment):
        if key.startswith("CENTRAL_") or key.startswith("ONBOARD_CENTRAL_"):
            environment.pop(key)
    return console, environment


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
    console, environment = await asyncio.to_thread(_installed_quickstart, tmp_path)
    command = [str(console)]
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

        client_program = textwrap.dedent(
            """
            import asyncio
            import json
            import sys
            from pathlib import Path

            from pursers_client import BoardClient

            async def main():
                root = Path(sys.argv[1])
                url = sys.argv[2]
                admin_token = (root / "admin.jwt").read_text(encoding="utf-8").strip()
                worker_token = (root / "worker.jwt").read_text(encoding="utf-8").strip()
                async with BoardClient(
                    url, admin_token, "local", agent_name="local-admin", role="worker"
                ):
                    pass
                async with BoardClient(
                    url, worker_token, "local", agent_name="local-worker", role="worker"
                ) as client:
                    created = await client.ticket_create(
                        "TK-quickstart-e2e", "First ticket"
                    )
                    snapshot = await client.board_snapshot()
                    listed = await client.board_list()
                print(json.dumps({
                    "created": created["ok"],
                    "tickets": [ticket["ticket_id"] for ticket in snapshot["tickets"]],
                    "boards": [board["board_id"] for board in listed["boards"]],
                    "ticket_count": listed["boards"][0]["ticket_count"],
                }))

            asyncio.run(main())
            """
        )
        checked = await asyncio.to_thread(
            _run,
            [
                str(console.parent / ("python.exe" if os.name == "nt" else "python")),
                "-c",
                client_program,
                str(root),
                f"http://127.0.0.1:{port}/mcp",
            ],
            cwd=tmp_path,
            env=environment,
            timeout=30,
        )
        assert json.loads(checked.stdout) == {
            "created": True,
            "tickets": ["TK-quickstart-e2e"],
            "boards": ["local"],
            "ticket_count": 1,
        }
    finally:
        server.terminate()
        try:
            await asyncio.to_thread(server.wait, 10)
        except subprocess.TimeoutExpired:
            server.kill()
            await asyncio.to_thread(server.wait, 10)
