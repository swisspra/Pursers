from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import tomllib

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from pursers_central import pursers_central_runtime as runtime


def test_console_script_resolves_via_importlib_metadata() -> None:
    project = tomllib.loads(
        (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    entry_point = importlib.metadata.EntryPoint(
        name="pursers-central",
        value=project["scripts"]["pursers-central"],
        group="console_scripts",
    )

    assert entry_point.load() is runtime.main


@pytest.mark.parametrize(
    "module",
    ("pursers_central", "pursers_central.pursers_central_runtime"),
)
def test_module_exits_nonzero_for_unwritable_data_dir(
    module: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "read-only"
    data_dir.mkdir()
    data_dir.chmod(0o500)
    environment = os.environ.copy()
    python_path = [
        str(PACKAGE_ROOT / "src"),
        str(REPOSITORY_ROOT / "packages" / "client" / "src"),
    ]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                module,
                "--data-dir",
                str(data_dir),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        data_dir.chmod(0o700)

    assert result.returncode != 0
    assert "cannot use data directory" in result.stderr
    assert "Permission denied" in result.stderr
    assert "Traceback" not in result.stderr


def test_runtime_starts_production_app_with_banner_and_log_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Lock:
        exited_with: tuple[object, object, object] | None = None

        def __exit__(self, *args: object) -> None:
            self.exited_with = args

    app = object()
    mcp = object()
    service = object()
    lock = Lock()

    with (
        patch.object(
            sys,
            "argv",
            [
                "pursers-central",
                "--host",
                "127.0.0.2",
                "--port",
                "9012",
                "--data-dir",
                str(tmp_path / "central-data"),
                "--log-level",
                "debug",
            ],
        ),
        patch.object(runtime, "_acquire_data_lock", return_value=lock),
        patch.object(runtime.central, "build_server", return_value=(mcp, service)),
        patch.object(
            runtime, "create_streamable_http_app", return_value=app
        ) as create,
        patch.object(runtime.uvicorn, "run") as run,
    ):
        runtime.main()

    assert "bind=http://127.0.0.2:9012/mcp" in capsys.readouterr().err
    create.assert_called_once_with(
        mcp,
        service,
        host="127.0.0.2",
        allowed_hosts=(),
    )
    run.assert_called_once_with(
        app,
        host="127.0.0.2",
        port=9012,
        log_level="debug",
        server_header=False,
        access_log=False,
    )
    assert lock.exited_with == (None, None, None)


def test_runtime_enables_tls_and_extra_hosts_only_when_explicitly_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Lock:
        def __exit__(self, *_args: object) -> None:
            pass

    app = object()
    mcp = object()
    service = object()
    cert = Path("/PATH/TO/cert.pem")
    key = Path("/PATH/TO/key.pem")

    with (
        patch.object(
            sys,
            "argv",
            [
                "pursers-central",
                "--data-dir",
                str(tmp_path / "central-data"),
                "--ssl-certfile",
                str(cert),
                "--ssl-keyfile",
                str(key),
                "--allowed-host",
                "central.example",
            ],
        ),
        patch.object(runtime, "_acquire_data_lock", return_value=Lock()),
        patch.object(runtime.central, "build_server", return_value=(mcp, service)),
        patch.object(
            runtime, "create_streamable_http_app", return_value=app
        ) as create,
        patch.object(runtime.uvicorn, "run") as run,
    ):
        runtime.main()

    assert "bind=https://127.0.0.1:8766/mcp" in capsys.readouterr().err
    create.assert_called_once_with(
        mcp,
        service,
        host="127.0.0.1",
        allowed_hosts=("central.example",),
    )
    run.assert_called_once_with(
        app,
        host="127.0.0.1",
        port=8766,
        log_level="info",
        server_header=False,
        access_log=False,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
    )


def test_runtime_rejects_incomplete_tls_configuration(tmp_path: Path) -> None:
    with (
        patch.object(
            sys,
            "argv",
            [
                "pursers-central",
                "--data-dir",
                str(tmp_path / "central-data"),
                "--tls-certfile",
                "/PATH/TO/cert.pem",
            ],
        ),
        pytest.raises(SystemExit) as error,
    ):
        runtime.main()

    assert error.value.code == 2


def test_legacy_allowed_hosts_environment_is_supported() -> None:
    with patch.dict(
        os.environ,
        {"CENTRAL_ALLOWED_HOSTS": "central.example, proxy.example"},
        clear=True,
    ):
        assert runtime._env_hosts(
            "ONBOARD_CENTRAL_ALLOWED_HOSTS", "CENTRAL_ALLOWED_HOSTS"
        ) == ("central.example", "proxy.example")
