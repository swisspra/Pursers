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


def test_server_info_uses_runtime_version() -> None:
    assert wait_server.mcp.version == wait_server.VERSION


def test_runtime_version_falls_back_to_source_metadata(monkeypatch) -> None:
    def missing(_distribution: str) -> str:
        raise wait_server.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(wait_server.importlib.metadata, "version", missing)

    assert wait_server._runtime_version() == wait_server._source_version()
