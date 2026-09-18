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


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _upgrade_fixture(
    tmp_path: Path, *, dependency_changed: bool
) -> tuple[Path, Path, Path, dict[str, str], str, str]:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    checkout = tmp_path / "fleet"
    _git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    _git("clone", "-q", str(origin), str(operator), cwd=tmp_path)
    _git("config", "user.email", "test@example.invalid", cwd=operator)
    _git("config", "user.name", "Test", cwd=operator)
    for package in ("client", "central"):
        manifest = operator / "packages" / package / "pyproject.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('[project]\nversion = "1"\n', encoding="utf-8")
    (operator / "revision").write_text("one\n", encoding="utf-8")
    _git("add", ".", cwd=operator)
    _git("commit", "-qm", "one", cwd=operator)
    _git("branch", "-M", "main", cwd=operator)
    _git("push", "-qu", "origin", "main", cwd=operator)
    first = _git("rev-parse", "HEAD", cwd=operator)
    _git("clone", "-q", "--branch", "main", str(origin), str(checkout), cwd=tmp_path)

    (operator / "revision").write_text("two\n", encoding="utf-8")
    if dependency_changed:
        (operator / "packages" / "client" / "pyproject.toml").write_text(
            '[project]\nversion = "2"\n', encoding="utf-8"
        )
    _git("commit", "-qam", "two", cwd=operator)
    _git("push", "-q", "origin", "main", cwd=operator)
    second = _git("rev-parse", "HEAD", cwd=operator)

    events = tmp_path / "events"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "launchctl",
        '#!/bin/sh\nprintf "launchctl:%s\\n" "$*" >>"$EVENTS"\nexit 0\n',
    )
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "uv:%s\\n" "$*" >>"$EVENTS"\nexit "${UV_EXIT:-0}"\n',
    )
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        '#!/bin/sh\nprintf "python:%s\\n" "$*" >>"$EVENTS"\n'
        'exit "${PYTHON_EXIT:-0}"\n',
    )
    state = tmp_path / "state"
    environment = {
        **os.environ,
        "EVENTS": str(events),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PURSERS_FLEET_PYTHON": str(fake_python),
        "PURSERS_FLEET_REPO": str(checkout),
        "PURSERS_FLEET_STATE_DIR": str(state),
    }
    return checkout, state, events, environment, first, second


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


def test_upgrade_without_dependency_change_skips_reinstall(tmp_path: Path) -> None:
    checkout, state, events, environment, first, second = _upgrade_fixture(
        tmp_path, dependency_changed=False
    )
    subprocess.run(["/bin/sh", str(UPGRADER), second], check=True, env=environment)
    assert _git("rev-parse", "HEAD", cwd=checkout) == second
    assert (state / "deployments" / "previous-sha").read_text().strip() == first
    assert (state / "deployments" / "current-sha").read_text().strip() == second
    actions = events.read_text(encoding="utf-8").splitlines()
    assert not any(action.startswith("uv:") for action in actions)
    assert any(action.startswith("python:-c ") for action in actions)
    assert any(action.startswith("launchctl:kickstart -k ") for action in actions)


def test_upgrade_reinstalls_changed_dependencies_before_restart(tmp_path: Path) -> None:
    checkout, _, events, environment, _, second = _upgrade_fixture(
        tmp_path, dependency_changed=True
    )
    subprocess.run(["/bin/sh", str(UPGRADER), second], check=True, env=environment)
    assert _git("rev-parse", "HEAD", cwd=checkout) == second
    actions = events.read_text(encoding="utf-8").splitlines()
    reinstall = next(i for i, action in enumerate(actions) if action.startswith("uv:"))
    probe = next(i for i, action in enumerate(actions) if action.startswith("python:-c "))
    restart = next(
        i
        for i, action in enumerate(actions)
        if action.startswith("launchctl:kickstart -k ")
    )
    assert reinstall < probe < restart
    assert f"--python {environment['PURSERS_FLEET_PYTHON']}" in actions[reinstall]
    assert f"-e {checkout / 'packages' / 'client'}" in actions[reinstall]
    assert f"-e {checkout / 'packages' / 'central'}" in actions[reinstall]


def test_upgrade_falls_back_to_selected_interpreter_pip(tmp_path: Path) -> None:
    checkout, _, events, environment, _, second = _upgrade_fixture(
        tmp_path, dependency_changed=True
    )
    fake_bin = Path(environment["PURSERS_FLEET_PYTHON"]).parent
    (fake_bin / "uv").unlink()
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    subprocess.run(["/bin/sh", str(UPGRADER), second], check=True, env=environment)
    assert _git("rev-parse", "HEAD", cwd=checkout) == second
    actions = events.read_text(encoding="utf-8").splitlines()
    install = next(
        i for i, action in enumerate(actions) if action.startswith("python:-m pip install ")
    )
    probe = next(i for i, action in enumerate(actions) if action.startswith("python:-c "))
    restart = next(
        i
        for i, action in enumerate(actions)
        if action.startswith("launchctl:kickstart -k ")
    )
    assert install < probe < restart


def test_upgrade_reinstall_failure_restores_checkout_without_restart(
    tmp_path: Path,
) -> None:
    checkout, state, events, environment, first, second = _upgrade_fixture(
        tmp_path, dependency_changed=True
    )
    environment["UV_EXIT"] = "1"
    result = subprocess.run(
        ["/bin/sh", str(UPGRADER), second], check=False, env=environment
    )
    assert result.returncode == 69
    assert _git("rev-parse", "HEAD", cwd=checkout) == first
    assert (state / "deployments" / "current-sha").read_text().strip() == first
    actions = events.read_text(encoding="utf-8").splitlines()
    assert any(action.startswith("uv:") for action in actions)
    assert not any(action.startswith("python:") for action in actions)
    assert not any(action.startswith("launchctl:kickstart -k ") for action in actions)


def test_upgrade_import_probe_failure_restores_checkout_without_restart(
    tmp_path: Path,
) -> None:
    checkout, state, events, environment, first, second = _upgrade_fixture(
        tmp_path, dependency_changed=False
    )
    environment["PYTHON_EXIT"] = "1"
    result = subprocess.run(
        ["/bin/sh", str(UPGRADER), second], check=False, env=environment
    )
    assert result.returncode == 69
    assert _git("rev-parse", "HEAD", cwd=checkout) == first
    assert (state / "deployments" / "current-sha").read_text().strip() == first
    actions = events.read_text(encoding="utf-8").splitlines()
    assert any(action.startswith("python:-c ") for action in actions)
    assert not any(action.startswith("launchctl:kickstart -k ") for action in actions)


def test_launchagent_template_uses_repository_launcher_and_external_paths() -> None:
    with PLIST.open("rb") as source:
        document = plistlib.load(source)
    assert document["ProgramArguments"][-1].endswith(
        "tools/fleet-dashboard/launch.sh"
    )
    environment = document["EnvironmentVariables"]
    assert environment["PURSERS_FLEET_RUNTIME_DIR"].startswith("/PATH/TO/private/")
    assert environment["PURSERS_FLEET_STATE_DIR"].startswith("/PATH/TO/private/")
