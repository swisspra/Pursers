from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import stat
import subprocess
import urllib.request
import zipfile

import pytest

from tools import home_acceptance_handoff as handoff


SHA = "a" * 40
RUNTIME_SHA = "f5a24301262bcbe56d993276362dc74d6f1728ad"


def _file(path: Path, data: bytes = b"x", executable: bool = False) -> Path:
    path.write_bytes(data)
    path.chmod(0o700 if executable else 0o600)
    return path


def _args(tmp_path: Path):
    checkout = tmp_path / "candidate"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=checkout, check=True)
    _file(checkout / "tracked")
    subprocess.run(["git", "add", "tracked"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=checkout, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("webui/candidate.json", json.dumps({"candidate_commit": commit}))
    observer = _file(tmp_path / "runner.py", b"runner")
    backend = _file(tmp_path / "browser_observer.py", b"observer")
    harness = _file(tmp_path / "harness.py", b"harness")
    host = tmp_path / "AionUi.app"
    core = host / "Contents" / "Resources" / "bundled-aioncore" / "darwin-arm64" / "aioncore"
    core.parent.mkdir(parents=True)
    _file(core, executable=True)
    with (host / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "com.aionui.app", "CFBundleShortVersionString": "2.2.1"}, stream)
    codesign = _file(
        tmp_path / "codesign",
        b"#!/bin/sh\ncase \"$1\" in (-dvvv) printf '%s\\n' 'Identifier=com.aionui.app' 'TeamIdentifier=52JQX2HUSC' 'Authority=Developer ID Application: AionUi Inc. (52JQX2HUSC)' 'Notarization Ticket=stapled' 'CDHash=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';; esac\n",
        executable=True,
    )
    bridge = _file(
        tmp_path / "bridge",
        b"#!/bin/sh\ncase \"$1\" in\n  --version) echo 0.1.0a15;;\n  ticket-lifecycle|seat-lifecycle|team-lifecycle) test \"$2\" = --help && echo \"usage: pursers-wait-bridge $1\";;\n  *) exit 9;;\nesac\n",
        executable=True,
    )
    values = {
        "candidate_checkout": str(checkout), "commit": commit, "candidate_zip": str(package),
        "sandbox_root": str(tmp_path / "handoff"), "board": "sandbox-home-acceptance",
        "central": "work", "origin": "http://127.0.0.1:25808", "helper_port": 0,
        "page_path": "/api/extensions/pursers/assets/webui/index.html",
        "observer_runner": str(observer),
        "observer_sha256": hashlib.sha256(b"runner").hexdigest(),
        "observer_backend": str(backend), "observer_backend_sha256": hashlib.sha256(b"observer").hexdigest(),
        "observer_harness": str(harness), "observer_harness_sha256": hashlib.sha256(b"harness").hexdigest(),
        "observer_install_dir": str(tmp_path / "reviewer-owned-observer"),
        "helper": str(_file(tmp_path / "helper.cjs")), "helper_sha256": hashlib.sha256(b"x").hexdigest(),
        "node": str(_file(tmp_path / "node", executable=True)),
        "bridge_bin": str(bridge),
        "aioncore_bin": str(core),
        "ego_browser": str(_file(tmp_path / "ego-browser", executable=True)),
        "host_bundle": str(host), "host_cdhash": "b" * 40, "codesign": str(codesign),
        "host_version": "2.2.1", "identity_mode": "webui",
        "aionpro_bootstrap_secret_file": None,
        "runtime_commit": RUNTIME_SHA, "core_version": "0.2.1",
        "task_space": "acceptance-test",
    }
    return type("Args", (), values)()


def test_prepare_writes_private_reproducible_handoff(tmp_path: Path, capsys) -> None:
    args = _args(tmp_path)
    result = handoff.prepare(args)
    root = Path(result["handoff"])
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("helper-token", "fresh_install.assertions.json", "handoff.json"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    for name in ("launch-aioncore.py", "start-aioncore.sh", "start-helper.sh", "reviewer-commands.sh", "cleanup.sh"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o700
        if name.endswith(".sh"):
            subprocess.run(["sh", "-n", str(root / name)], check=True)
    manifest = json.loads((root / "handoff.json").read_text())
    token = (root / "helper-token").read_text().strip()
    assert len(token) == 64
    assert token not in json.dumps(manifest)
    assert manifest["candidate"]["commit"] == args.commit
    assert manifest["central"] == "work"
    assert manifest["bridge_runtime"]["verified_commands"] == [
        "ticket-lifecycle", "seat-lifecycle", "team-lifecycle",
    ]
    helper_start = (root / "start-helper.sh").read_text()
    assert "--central \\\n  work" in helper_start
    assert 'ps -ww -p "$pid" -o command=' in (root / "cleanup.sh").read_text()
    assert manifest["signed_host"]["identity_mode"] == "webui"
    core_start = (root / "start-aioncore.sh").read_text()
    assert "unset AIONCORE_BOOTSTRAP_SECRET" in core_start
    assert core_start.splitlines()[-2:] == ["  --identity-mode \\", "  webui"]
    assert manifest["secrets"]["aionpro_bootstrap_secret"] == {
        "path": None,
        "required": False,
        "source": "not-used",
        "value_recorded": False,
    }
    commands = (root / "reviewer-commands.sh").read_text()
    assert "install-observer" in commands
    assert "doctor" in commands
    assert "capture" in commands
    assert "validate" in commands
    assert "I_UNDERSTAND_SANDBOX_ONLY" in commands
    assert "test_live_host.py" in commands
    assert all(name in commands for name in handoff.OBSERVATIONS)
    assert "http://127.0.0.1:25808/api/extensions/pursers/assets/webui/index.html" in commands
    assert (root / "extensions" / "pursers-home" / "webui" / "candidate.json").is_file()
    assert not Path(args.observer_install_dir).exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("board", ["pursers", "production-home", "sandbox-"])
def test_refuses_non_sandbox_board(tmp_path: Path, board: str) -> None:
    args = _args(tmp_path)
    args.board = board
    with pytest.raises(handoff.HandoffError, match="board"):
        handoff.prepare(args)


@pytest.mark.parametrize("central", ["", "work central", "/work", "a" * 81])
def test_refuses_unsafe_central(tmp_path: Path, central: str) -> None:
    args = _args(tmp_path)
    args.central = central
    with pytest.raises(handoff.HandoffError, match="central"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_generated_helper_starts_with_authenticated_board_and_central(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    args = _args(tmp_path)
    args.node = str(Path(node).resolve())
    helper = Path(handoff.__file__).parent / "aionui-extension" / "host" / "helper.cjs"
    args.helper = str(helper)
    args.helper_sha256 = hashlib.sha256(helper.read_bytes()).hexdigest()
    root = Path(handoff.prepare(args)["handoff"])
    process = subprocess.Popen(
        [str(root / "start-helper.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, process.stderr.read() if process.poll() is not None and process.stderr else "helper timed out"
        started = json.loads(process.stdout.readline())
        request = urllib.request.Request(
            f"http://127.0.0.1:{started['port']}/pursers/helper/status",
            headers={
                "Origin": args.origin,
                "x-pursers-home-token": (root / "helper-token").read_text().strip(),
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            status_payload = json.load(response)
        assert status_payload["ok"] is True
        assert status_payload["board"] == args.board
        assert status_payload["central"] == args.central
    finally:
        if process.poll() is None:
            subprocess.run([str(root / "cleanup.sh")], check=True, timeout=5)
        process.wait(timeout=5)


def test_documented_offline_wheelhouse_runs_lifecycle_and_handoff(tmp_path: Path) -> None:
    python = shutil.which("python3.12")
    if python is None:
        pytest.skip("python3.12 is required")
    root = Path(__file__).resolve().parents[2]
    ambient_wheels = tmp_path / "ambient-wheels"
    ambient_wheels.mkdir()
    with zipfile.ZipFile(
        ambient_wheels / "pursers_client-0.1.0a22-py3-none-any.whl", "w"
    ) as archive:
        archive.writestr(
            "pursers_client-0.1.0a22.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: pursers-client\nVersion: 0.1.0a22\n",
        )
        archive.writestr(
            "pursers_client-0.1.0a22.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: acceptance-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("pursers_client-0.1.0a22.dist-info/RECORD", "")
    contaminated_environment = {
        **os.environ,
        "PIP_FIND_LINKS": str(ambient_wheels),
        "UV_FIND_LINKS": str(ambient_wheels),
    }
    wheelhouse = tmp_path / "wheelhouse"
    built = subprocess.run(
        [
            python,
            str(root / "tools" / "build_home_runtime_wheelhouse.py"),
            "--python",
            python,
            "--output",
            str(wheelhouse),
            "--allow-dirty",
        ],
        check=False,
        capture_output=True,
        cwd=root,
        env=contaminated_environment,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    checksums = (wheelhouse / "SHA256SUMS").read_text()
    assert stat.S_IMODE(wheelhouse.stat().st_mode) == 0o700
    assert stat.S_IMODE((wheelhouse / "wheelhouse.json").stat().st_mode) == 0o600
    assert "mcp-2.1.1-py3-none-any.whl" in checksums
    assert "pursers_client-0.1.0a22-py3-none-any.whl" in checksums
    assert "pursers_wait_bridge-0.1.0a15-py3-none-any.whl" in checksums
    subprocess.run(
        ["shasum", "-a", "256", "-c", "SHA256SUMS"],
        check=True,
        capture_output=True,
        cwd=wheelhouse,
        text=True,
    )

    runtime = tmp_path / "runtime"
    subprocess.run(
        [python, "-m", "venv", str(runtime)],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_python = runtime / "bin" / "python"
    absent = subprocess.run(
        [
            runtime_python,
            "-I",
            "-c",
            "import importlib.util; assert importlib.util.find_spec('mcp') is None",
        ],
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )
    assert absent.returncode == 0, absent.stderr
    subprocess.run(
        [
            runtime_python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            str(wheelhouse / "pursers_client-0.1.0a22-py3-none-any.whl"),
            str(wheelhouse / "pursers_wait_bridge-0.1.0a15-py3-none-any.whl"),
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=contaminated_environment,
        text=True,
    )
    subprocess.run(
        [
            runtime_python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            str(wheelhouse / "pursers_client-0.1.0a22-py3-none-any.whl"),
            str(wheelhouse / "pursers_wait_bridge-0.1.0a15-py3-none-any.whl"),
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=contaminated_environment,
        text=True,
    )
    subprocess.run(
        [runtime_python, "-m", "pip", "check"],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=contaminated_environment,
        text=True,
    )
    bridge = runtime / "bin" / "pursers-wait-bridge"
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONHOME", None)
    clean_environment.pop("PYTHONPATH", None)
    for command in ("ticket-lifecycle", "seat-lifecycle", "team-lifecycle"):
        helped = subprocess.run(
            [bridge, command, "--help"],
            check=True,
            capture_output=True,
            cwd=tmp_path,
            env=clean_environment,
            text=True,
        )
        assert f"pursers-wait-bridge {command}" in helped.stdout

    args = _args(tmp_path)
    args.bridge_bin = str(bridge)
    handoff_root = Path(handoff.prepare(args)["handoff"])
    manifest = json.loads((handoff_root / "handoff.json").read_text())
    assert manifest["bridge_runtime"]["binary"] == str(bridge.resolve())
    assert manifest["bridge_runtime"]["version"] == "0.1.0a15"
    assert manifest["bridge_runtime"]["verified_commands"] == [
        "ticket-lifecycle", "seat-lifecycle", "team-lifecycle",
    ]


def test_refuses_legacy_bridge_before_output(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.bridge_bin = str(_file(
        tmp_path / "legacy-bridge",
        b"#!/bin/sh\ncase \"$1\" in --version) echo 0.1.0a15;; *) echo legacy-server;; esac\n",
        executable=True,
    ))
    with pytest.raises(handoff.HandoffError, match="ticket-lifecycle"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_refuses_non_loopback_origin(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.origin = "https://example.com:25808"
    with pytest.raises(handoff.HandoffError, match="loopback"):
        handoff.prepare(args)


@pytest.mark.parametrize("identity_mode", ["", "desktop", "WEBUI"])
def test_refuses_unsupported_identity_mode(tmp_path: Path, identity_mode: str) -> None:
    args = _args(tmp_path)
    args.identity_mode = identity_mode
    with pytest.raises(handoff.HandoffError, match="identity mode"):
        handoff.prepare(args)


def test_refuses_aionpro_without_explicit_secret_before_output(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.identity_mode = "aionpro"
    monkeypatch.setenv("AIONCORE_BOOTSTRAP_SECRET", "ambient-secret-must-not-count")
    with pytest.raises(handoff.HandoffError, match="requires --aionpro-bootstrap-secret-file"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_refuses_non_private_aionpro_secret_before_output(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.identity_mode = "aionpro"
    secret = _file(tmp_path / "aionpro-secret", b"sandbox-only-secret")
    secret.chmod(0o640)
    args.aionpro_bootstrap_secret_file = str(secret)
    with pytest.raises(handoff.HandoffError, match="exactly 0600"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_refuses_symlinked_aionpro_secret_before_output(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.identity_mode = "aionpro"
    secret = _file(tmp_path / "aionpro-secret", b"sandbox-only-secret")
    link = tmp_path / "aionpro-secret-link"
    link.symlink_to(secret)
    args.aionpro_bootstrap_secret_file = str(link)
    with pytest.raises(handoff.HandoffError, match="symlink"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_webui_refuses_aionpro_secret_file(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.aionpro_bootstrap_secret_file = str(
        _file(tmp_path / "aionpro-secret", b"sandbox-only-secret")
    )
    with pytest.raises(handoff.HandoffError, match="valid only with aionpro"):
        handoff.prepare(args)
    assert not Path(args.sandbox_root).exists()


def test_aionpro_runtime_uses_explicit_secret_not_ambient(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.identity_mode = "aionpro"
    secret_value = "sandbox-only-test-secret-42"
    secret = _file(tmp_path / "aionpro-secret", (secret_value + "\n").encode())
    args.aionpro_bootstrap_secret_file = str(secret)
    _file(
        Path(args.aioncore_bin),
        (
            "#!/bin/sh\n"
            f"test \"$AIONCORE_BOOTSTRAP_SECRET\" = {secret_value!r} || exit 9\n"
            "printf '%s\\n' aionpro-ready\n"
        ).encode(),
        executable=True,
    )
    root = Path(handoff.prepare(args)["handoff"])
    manifest = json.loads((root / "handoff.json").read_text())
    assert manifest["signed_host"]["identity_mode"] == "aionpro"
    start_text = (root / "start-aioncore.sh").read_text()
    launcher_text = (root / "launch-aioncore.py").read_text()
    assert start_text.splitlines()[-2:] == [
        "  --identity-mode \\",
        "  aionpro",
    ]
    result = subprocess.run(
        [str(root / "start-aioncore.sh")],
        capture_output=True,
        check=False,
        env={**os.environ, "AIONCORE_BOOTSTRAP_SECRET": "ambient-secret-must-not-count"},
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "aionpro-ready\n"
    exposed = start_text + launcher_text + json.dumps(manifest) + result.stdout + result.stderr
    assert secret_value not in exposed
    assert "ambient-secret-must-not-count" not in exposed
    assert manifest["secrets"]["aionpro_bootstrap_secret"] == {
        "path": str(secret),
        "required": True,
        "source": "explicit-private-file",
        "value_recorded": False,
    }


def test_aionpro_runtime_rechecks_secret_permissions(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.identity_mode = "aionpro"
    secret_value = "sandbox-only-test-secret-42"
    secret = _file(tmp_path / "aionpro-secret", secret_value.encode())
    args.aionpro_bootstrap_secret_file = str(secret)
    root = Path(handoff.prepare(args)["handoff"])
    secret.chmod(0o640)
    result = subprocess.run(
        [str(root / "start-aioncore.sh")], capture_output=True, check=False, text=True
    )
    assert result.returncode == 2
    assert "mode must be exactly 0600" in result.stderr
    assert secret_value not in result.stderr


def test_refuses_zip_bound_to_different_commit(tmp_path: Path) -> None:
    args = _args(tmp_path)
    with zipfile.ZipFile(args.candidate_zip, "w") as archive:
        archive.writestr("webui/candidate.json", json.dumps({"candidate_commit": SHA}))
    with pytest.raises(handoff.HandoffError, match="ZIP commit"):
        handoff.prepare(args)


def test_refuses_nested_candidate_identity(tmp_path: Path) -> None:
    args = _args(tmp_path)
    with zipfile.ZipFile(args.candidate_zip, "w") as archive:
        archive.writestr(
            "prefix/webui/candidate.json",
            json.dumps({"candidate_commit": args.commit}),
        )
    with pytest.raises(handoff.HandoffError, match="root webui/candidate.json"):
        handoff.prepare(args)


def test_refuses_sandbox_inside_candidate(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.sandbox_root = str(Path(args.candidate_checkout) / "private")
    with pytest.raises(handoff.HandoffError, match="sandbox root"):
        handoff.prepare(args)


def test_refuses_unapproved_observer(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.observer_sha256 = "0" * 64
    with pytest.raises(handoff.HandoffError, match="observer runner SHA-256"):
        handoff.prepare(args)


def test_refuses_aioncore_outside_signed_bundle(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.aioncore_bin = str(_file(tmp_path / "outside-core", executable=True))
    with pytest.raises(handoff.HandoffError, match="signed host bundle"):
        handoff.prepare(args)


@pytest.mark.parametrize(
    "field",
    [
        "candidate_zip",
        "observer_runner",
        "observer_backend",
        "observer_harness",
        "helper",
        "node",
        "bridge_bin",
        "aioncore_bin",
        "ego_browser",
        "codesign",
    ],
)
def test_refuses_symlinked_protected_input(tmp_path: Path, field: str) -> None:
    """A supplied symlink must be refused even when it points at the approved file.

    The earlier implementation resolved the path before calling lstat, so the
    symlink was dereferenced and accepted with a matching content hash. The
    check must see the path as supplied.
    """

    args = _args(tmp_path)
    target = Path(getattr(args, field))
    assert target.is_file()
    link = tmp_path / f"link-{field}"
    link.symlink_to(target)
    setattr(args, field, str(link))
    with pytest.raises(handoff.HandoffError, match="symlink"):
        handoff.prepare(args)


def test_refuses_symlinked_input_reached_through_relative_parts(tmp_path: Path) -> None:
    """Normalising away '..' must not turn a supplied symlink into its target."""

    args = _args(tmp_path)
    target = Path(args.helper)
    link = tmp_path / "link-helper.cjs"
    link.symlink_to(target)
    nested = tmp_path / "nested"
    nested.mkdir()
    args.helper = str(nested / ".." / link.name)
    with pytest.raises(handoff.HandoffError, match="symlink"):
        handoff.prepare(args)


def test_accepts_the_same_inputs_when_supplied_as_real_paths(tmp_path: Path) -> None:
    """The control for the symlink cases: identical inputs, real paths, still prepares."""

    args = _args(tmp_path)
    root = Path(handoff.prepare(args)["handoff"])
    manifest = json.loads((root / "handoff.json").read_text())
    assert manifest["candidate"]["commit"] == args.commit
