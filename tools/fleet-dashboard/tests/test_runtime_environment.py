from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import runtime_environment


def _load_dashboard():
    path = MODULE_DIR / "fleet_dashboard.py"
    spec = importlib.util.spec_from_file_location("hermetic_dashboard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_state_override_avoids_home_and_worker_creation_until_write(
    tmp_path: Path, monkeypatch
) -> None:
    missing_home = tmp_path / "missing-home"
    state_root = tmp_path / "state"
    monkeypatch.setenv("HOME", str(missing_home))
    monkeypatch.setenv("PURSERS_STATE_DIR", str(state_root))
    dashboard = _load_dashboard()

    manager = dashboard.WorkerManager(platform="darwin")

    assert manager.root == state_root / "workers"
    assert not manager.root.exists()
    assert not missing_home.exists()


def test_process_provider_reports_sandbox_denial_without_raising() -> None:
    def denied(_command, **_kwargs):
        raise PermissionError("sandbox denied process inspection")

    result = runtime_environment._default_process_list_provider(
        ["ps", "-axo", "pid="], runner=denied
    )

    assert result == runtime_environment.ProcessInspection(
        False,
        message=runtime_environment.PROCESS_INSPECTION_UNAVAILABLE,
    )


def test_worker_status_preserves_pid_when_process_inspection_is_unavailable(
    tmp_path: Path,
) -> None:
    dashboard = _load_dashboard()
    root = tmp_path / "workers"
    root.mkdir()
    pid_path = root / "worker-one.pid"
    pid_path.write_text(json.dumps({"pid": 43210}), encoding="utf-8")
    pid_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def unavailable(
        _command: object, *, runner: object = None
    ) -> runtime_environment.ProcessInspection:
        del runner
        return runtime_environment.ProcessInspection(
            False,
            message=runtime_environment.PROCESS_INSPECTION_UNAVAILABLE,
        )

    manager = dashboard.WorkerManager(
        root,
        platform="darwin",
        process_provider=unavailable,
    )

    assert manager.status("worker-one") == {
        "running": False,
        "pid": 43210,
        "adopted": False,
        "process_inspection": "process inspection unavailable",
    }
    assert pid_path.exists()
