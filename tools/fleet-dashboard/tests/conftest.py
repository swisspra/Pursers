from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_dashboard_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep state and process discovery deterministic in every dashboard test."""
    monkeypatch.setenv("PURSERS_STATE_DIR", str(tmp_path / "pursers-state"))

    import runtime_environment

    def no_processes(
        _command: object, *, runner: object = None
    ) -> runtime_environment.ProcessInspection:
        del runner
        return runtime_environment.ProcessInspection(True, stdout="")

    monkeypatch.setattr(
        runtime_environment, "PROCESS_LIST_PROVIDER", no_processes
    )
