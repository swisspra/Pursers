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
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from pursers_central.jwt_verifier import JWTTokenVerifier, JWTVerifierConfig
from pursers_central.pursers_central_runtime import main as central_main
from pursers_central.quickstart import (
    MANAGED_FILES,
    QuickstartError,
    init_instance,
    retire_key,
    rotate_key,
)


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


def _jwt_kid(token: str) -> str:
    return str(jwt.get_unverified_header(token)["kid"])


def _verifier(root: Path, *, port: int) -> JWTTokenVerifier:
    return JWTTokenVerifier(
        JWTVerifierConfig(
            issuer=f"http://127.0.0.1:{port}/quickstart",
            audience=f"http://127.0.0.1:{port}/mcp",
            jwks_path=root / "jwks.json",
        )
    )


def test_rotate_and_retire_key_preserve_identity_formats_and_door_keys(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    port = 9123
    paths = init_instance(root, port=port, board_id="local-test")
    old_admin = (root / "admin.jwt").read_text(encoding="utf-8").strip()
    old_worker = (root / "worker.jwt").read_text(encoding="utf-8").strip()
    old_kid = _jwt_kid(old_admin)
    (root / "admin.jwt").write_text(
        f"Authorization: Bearer {old_admin}\n", encoding="utf-8"
    )
    os.chmod(root / "admin.jwt", 0o600)

    door_private = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    door = RSAAlgorithm.to_jwk(door_private.public_key(), as_dict=True)
    door.update(
        {
            "kid": "door-local-test",
            "alg": "RS256",
            "use": "sig",
            "pursers_door": {"board": "local-test", "label": "Local door"},
        }
    )
    jwks = json.loads((root / "jwks.json").read_text(encoding="utf-8"))
    jwks["keys"].append(door)
    (root / "jwks.json").write_text(
        json.dumps(jwks, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(root / "jwks.json", 0o600)
    door_bytes = json.dumps(door, sort_keys=True, separators=(",", ":")).encode()

    result = rotate_key(
        paths["signing-key.pem"],
        paths["jwks.json"],
        [paths["admin.jwt"], paths["worker.jwt"]],
    )
    assert result["old_kid"] == old_kid
    assert result["new_kid"] != old_kid
    assert result["token_count"] == 2
    assert Path(result["retired_key"]).is_file()
    assert _mode(Path(result["retired_key"])) == 0o600
    assert all(
        _mode(path) == 0o600
        for path in (
            root / "signing-key.pem",
            root / "jwks.json",
            root / "admin.jwt",
            root / "worker.jwt",
        )
    )

    admin_file = (root / "admin.jwt").read_text(encoding="utf-8")
    worker_file = (root / "worker.jwt").read_text(encoding="utf-8")
    assert admin_file.startswith("Authorization: Bearer ")
    assert not worker_file.startswith("Authorization: Bearer ")
    new_admin = admin_file.removeprefix("Authorization: Bearer ").strip()
    new_worker = worker_file.strip()
    assert _jwt_kid(new_admin) == result["new_kid"]
    assert _jwt_kid(new_worker) == result["new_kid"]

    old_claims = jwt.decode(old_admin, options={"verify_signature": False})
    new_claims = jwt.decode(new_admin, options={"verify_signature": False})
    assert {
        key: value
        for key, value in old_claims.items()
        if key not in {"iat", "nbf", "exp"}
    } == {
        key: value
        for key, value in new_claims.items()
        if key not in {"iat", "nbf", "exp"}
    }
    assert new_claims["exp"] - new_claims["iat"] == (
        old_claims["exp"] - old_claims["iat"]
    )

    rotated_jwks = json.loads((root / "jwks.json").read_text(encoding="utf-8"))
    rotated_door = next(item for item in rotated_jwks["keys"] if item["kid"] == door["kid"])
    assert json.dumps(
        rotated_door, sort_keys=True, separators=(",", ":")
    ).encode() == door_bytes

    verifier = _verifier(root, port=port)
    old_access = asyncio.run(verifier.verify_token(old_admin))
    new_access = asyncio.run(verifier.verify_token(new_admin))
    old_worker_access = asyncio.run(verifier.verify_token(old_worker))
    new_worker_access = asyncio.run(verifier.verify_token(new_worker))
    assert old_access is not None and new_access is not None
    assert old_worker_access is not None and new_worker_access is not None
    assert (
        old_access.subject,
        old_access.client_id,
        old_access.claims["iss"],
    ) == (
        new_access.subject,
        new_access.client_id,
        new_access.claims["iss"],
    )

    with pytest.raises(QuickstartError, match="current signing key"):
        retire_key(
            paths["signing-key.pem"], paths["jwks.json"], str(result["new_kid"])
        )
    with pytest.raises(QuickstartError, match="door key"):
        retire_key(paths["signing-key.pem"], paths["jwks.json"], str(door["kid"]))

    old_token_path = root / "old.jwt"
    old_token_path.write_text(old_admin + "\n", encoding="utf-8")
    os.chmod(old_token_path, 0o600)
    with pytest.raises(QuickstartError, match="still uses"):
        retire_key(
            paths["signing-key.pem"],
            paths["jwks.json"],
            old_kid,
            check_token_paths=[old_token_path],
        )

    retired = retire_key(paths["signing-key.pem"], paths["jwks.json"], old_kid)
    assert retired["retired_kid"] == old_kid
    assert not Path(result["retired_key"]).exists()
    assert asyncio.run(verifier.verify_token(old_admin)) is None
    assert asyncio.run(verifier.verify_token(new_admin)) is not None
    with pytest.raises(QuickstartError, match="last issuer key"):
        retire_key(
            paths["signing-key.pem"], paths["jwks.json"], str(result["new_kid"])
        )


def test_rotate_key_failure_restores_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "instance"
    paths = init_instance(root)
    protected = {
        path: path.read_bytes()
        for path in (
            root / "signing-key.pem",
            root / "jwks.json",
            root / "admin.jwt",
            root / "worker.jwt",
        )
    }
    real_replace = os.replace
    calls = 0

    def fail_third_replace(source: Path | str, destination: Path | str) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_third_replace)
    with pytest.raises(QuickstartError, match="cannot commit"):
        rotate_key(
            paths["signing-key.pem"],
            paths["jwks.json"],
            [paths["admin.jwt"], paths["worker.jwt"]],
        )
    assert {path: path.read_bytes() for path in protected} == protected
    assert not tuple(root.glob("signing-key.*.retired.pem"))


def test_rotate_key_refuses_foreign_token_without_changes(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    paths = init_instance(root)
    original_token = (root / "admin.jwt").read_text(encoding="utf-8").strip()
    claims = jwt.decode(original_token, options={"verify_signature": False})
    foreign_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    foreign_token = jwt.encode(
        claims,
        foreign_key,
        algorithm="RS256",
        headers={"kid": "foreign-issuer"},
    )
    (root / "admin.jwt").write_text(foreign_token + "\n", encoding="utf-8")
    os.chmod(root / "admin.jwt", 0o600)
    protected = {
        path: path.read_bytes()
        for path in (
            root / "signing-key.pem",
            root / "jwks.json",
            root / "admin.jwt",
            root / "worker.jwt",
        )
    }

    with pytest.raises(QuickstartError, match="current or new issuer"):
        rotate_key(
            paths["signing-key.pem"],
            paths["jwks.json"],
            [paths["admin.jwt"], paths["worker.jwt"]],
        )
    assert {path: path.read_bytes() for path in protected} == protected
    assert not tuple(root.glob("signing-key.*.retired.pem"))


def test_rotate_key_refuses_kid_that_cannot_be_a_retired_filename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    paths = init_instance(root)
    jwks_path = root / "jwks.json"
    jwks = json.loads(jwks_path.read_text(encoding="utf-8"))
    jwks["keys"][0]["kid"] = "../outside"
    jwks_path.write_text(json.dumps(jwks, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(jwks_path, 0o600)

    with pytest.raises(QuickstartError, match="not safe"):
        rotate_key(
            paths["signing-key.pem"],
            paths["jwks.json"],
            [paths["admin.jwt"], paths["worker.jwt"]],
        )
    assert not (tmp_path / "outside.retired.pem").exists()


def test_cli_rotation_output_never_contains_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "instance"
    init_instance(root)
    old_tokens = [
        (root / name).read_text(encoding="utf-8").strip()
        for name in ("admin.jwt", "worker.jwt")
    ]
    old_pem = (root / "signing-key.pem").read_text(encoding="utf-8")

    central_main(["rotate-key", str(root)])
    rotated_output = capsys.readouterr()
    combined = rotated_output.out + rotated_output.err
    assert all(token not in combined for token in old_tokens)
    assert old_pem not in combined
    assert "BEGIN PRIVATE KEY" not in combined
    assert "tokens re-signed: 2" in combined

    new_tokens = [
        (root / name).read_text(encoding="utf-8").strip()
        for name in ("admin.jwt", "worker.jwt")
    ]
    old_kid = next(
        item["kid"]
        for item in json.loads((root / "jwks.json").read_text(encoding="utf-8"))["keys"]
        if item["kid"] != _jwt_kid(new_tokens[0])
    )
    central_main(["retire-key", str(root), "--kid", old_kid])
    retired_output = capsys.readouterr()
    combined = retired_output.out + retired_output.err
    assert all(token not in combined for token in [*old_tokens, *new_tokens])
    assert "BEGIN PRIVATE KEY" not in combined


def test_generic_cli_forms_rotate_and_retire(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    init_instance(root)
    old_kid = _jwt_kid((root / "admin.jwt").read_text(encoding="utf-8").strip())
    central_main(
        [
            "rotate-key",
            "--key",
            str(root / "signing-key.pem"),
            "--jwks",
            str(root / "jwks.json"),
            "--token",
            str(root / "admin.jwt"),
            "--token",
            str(root / "worker.jwt"),
        ]
    )
    central_main(
        [
            "retire-key",
            "--jwks",
            str(root / "jwks.json"),
            "--kid",
            old_kid,
            "--check-token",
            str(root / "admin.jwt"),
            "--check-token",
            str(root / "worker.jwt"),
        ]
    )
    remaining_kids = {
        item["kid"]
        for item in json.loads(
            (root / "jwks.json").read_text(encoding="utf-8")
        )["keys"]
    }
    current_kid = _jwt_kid(
        (root / "admin.jwt").read_text(encoding="utf-8").strip()
    )
    assert remaining_kids == {current_kid}


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
