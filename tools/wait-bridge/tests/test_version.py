from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages/client/src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import pursers_wait_server as wait_server  # noqa: E402


def test_cli_version_matches_installed_distribution(monkeypatch, capsys) -> None:
    expected = "9.8.7"
    monkeypatch.setattr(
        wait_server.importlib.metadata,
        "version",
        lambda distribution: expected
        if distribution == "pursers-wait-bridge"
        else None,
    )
    monkeypatch.setattr(wait_server, "VERSION", wait_server._runtime_version())
    monkeypatch.setattr(sys, "argv", ["pursers-wait-bridge", "--version"])

    wait_server.main()

    assert capsys.readouterr().out.strip() == expected


def test_cli_help_does_not_configure_authenticated_runtime(
    monkeypatch, capsys, tmp_path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN", raising=False)
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN_FILE", raising=False)
    monkeypatch.setenv("PURSERS_BRIDGE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(sys, "argv", ["pursers-wait-bridge", "--help"])

    def unexpected_runtime_call(*_args, **_kwargs) -> None:
        raise AssertionError("help must not configure or start the runtime")

    monkeypatch.setattr(wait_server, "_configure_runtime", unexpected_runtime_call)
    monkeypatch.setattr(wait_server.mcp, "run", unexpected_runtime_call)

    wait_server.main()

    captured = capsys.readouterr()
    assert "usage: pursers-wait-bridge" in captured.out
    assert "ticket-lifecycle" in captured.out
    assert "FATAL" not in captured.err
    assert not state_dir.exists()


def test_server_info_uses_runtime_version() -> None:
    assert wait_server.mcp.version == wait_server.VERSION


def test_runtime_version_falls_back_to_source_metadata(monkeypatch) -> None:
    def missing(_distribution: str) -> str:
        raise wait_server.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(wait_server.importlib.metadata, "version", missing)

    assert wait_server._runtime_version() == wait_server._source_version()
