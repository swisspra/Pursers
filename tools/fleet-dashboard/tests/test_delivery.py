from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_dashboard_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)

LAUNCHER = MODULE_PATH.parent / "launch.sh"
UPGRADER = MODULE_PATH.parent / "upgrade.sh"
PLIST = MODULE_PATH.parent / "com.pursers.fleet-dashboard.plist.template"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_deployment_metadata_reports_exact_git_revision(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    source = repository / "tools" / "fleet-dashboard" / "fleet_dashboard.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n", encoding="utf-8")
    _git("init", "-q", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    _git("add", ".", cwd=repository)
    _git("commit", "-qm", "fixture", cwd=repository)
    revision = _git("rev-parse", "HEAD", cwd=repository)

    assert dashboard.deployment_metadata(source) == {
        "schema_version": 1,
        "running_sha": revision,
        "dirty": False,
    }
    source.write_text("# changed\n", encoding="utf-8")
    assert dashboard.deployment_metadata(source)["dirty"] is True


def test_version_endpoint_returns_injected_running_revision() -> None:
    revision = "a" * 40
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            object(),
            deployment={"schema_version": 1, "running_sha": revision, "dirty": False},
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback-only test server.
            f"http://127.0.0.1:{server.server_port}/api/version"
        ) as response:
            assert json.load(response) == {
                "schema_version": 1,
                "running_sha": revision,
                "dirty": False,
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launcher_keeps_runtime_and_state_outside_checkout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    token = tmp_path / "token"
    token.write_text("fixture", encoding="utf-8")
    capture = tmp_path / "capture.json"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "python3 - \"$@\" <<'PY'\n"
        "import json, os, sys\n"
        "json.dump({'args': sys.argv[1:], 'tmp': os.environ['TMPDIR'], "
        "'state': os.environ['PURSERS_STATE_DIR']}, open(os.environ['CAPTURE'], 'w'))\n"
        "PY\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = {
        **os.environ,
        "CAPTURE": str(capture),
        "PURSERS_FLEET_PYTHON": str(fake_python),
        "PURSERS_FLEET_REPO": str(MODULE_PATH.parents[2]),
        "PURSERS_FLEET_RUNTIME_DIR": str(runtime),
        "PURSERS_FLEET_STATE_DIR": str(state),
        "PURSERS_FLEET_URL": "http://127.0.0.1:8766/mcp",
        "PURSERS_FLEET_TOKEN_PATH": str(token),
    }
    subprocess.run(["/bin/sh", str(LAUNCHER)], check=True, env=environment)
    result = json.loads(capture.read_text(encoding="utf-8"))
    assert result["tmp"] == str(runtime / "tmp")
    assert result["state"] == str(state)
    assert result["args"][0] == str(MODULE_PATH)
    assert ["--workers-dir", str(state / "workers")] == result["args"][
        result["args"].index("--workers-dir") : result["args"].index("--workers-dir") + 2
    ]


def test_upgrade_moves_clean_checkout_and_records_rollback_sha(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    checkout = tmp_path / "fleet"
    _git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    _git("clone", "-q", str(origin), str(operator), cwd=tmp_path)
    _git("config", "user.email", "test@example.invalid", cwd=operator)
    _git("config", "user.name", "Test", cwd=operator)
    (operator / "revision").write_text("one\n", encoding="utf-8")
    _git("add", ".", cwd=operator)
    _git("commit", "-qm", "one", cwd=operator)
    _git("branch", "-M", "main", cwd=operator)
    _git("push", "-qu", "origin", "main", cwd=operator)
    first = _git("rev-parse", "HEAD", cwd=operator)
    _git("clone", "-q", "--branch", "main", str(origin), str(checkout), cwd=tmp_path)
    (operator / "revision").write_text("two\n", encoding="utf-8")
    _git("commit", "-qam", "two", cwd=operator)
    _git("push", "-q", "origin", "main", cwd=operator)
    second = _git("rev-parse", "HEAD", cwd=operator)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    state = tmp_path / "state"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PURSERS_FLEET_REPO": str(checkout),
        "PURSERS_FLEET_STATE_DIR": str(state),
    }
    subprocess.run(["/bin/sh", str(UPGRADER), second], check=True, env=environment)
    assert _git("rev-parse", "HEAD", cwd=checkout) == second
    assert (state / "deployments" / "previous-sha").read_text().strip() == first
    assert (state / "deployments" / "current-sha").read_text().strip() == second


def test_launchagent_template_uses_repository_launcher_and_external_paths() -> None:
    with PLIST.open("rb") as source:
        document = plistlib.load(source)
    assert document["ProgramArguments"][-1].endswith(
        "tools/fleet-dashboard/launch.sh"
    )
    environment = document["EnvironmentVariables"]
    assert environment["PURSERS_FLEET_RUNTIME_DIR"].startswith("/PATH/TO/private/")
    assert environment["PURSERS_FLEET_STATE_DIR"].startswith("/PATH/TO/private/")
