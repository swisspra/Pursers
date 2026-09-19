from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "zed" / "e2e_isolated.py"
SPEC = importlib.util.spec_from_file_location("zed_e2e_isolated", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
zed_e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = zed_e2e
SPEC.loader.exec_module(zed_e2e)


def test_write_settings_uses_extension_configuration_and_local_client(tmp_path: Path) -> None:
    token_file = tmp_path / "central" / "admin.jwt"
    token_file.parent.mkdir()
    token_file.write_text("not-a-real-token\n", encoding="utf-8")
    client_dir = tmp_path / "checkout" / "packages" / "client"
    client_dir.mkdir(parents=True)
    path = tmp_path / "zed-data" / "config" / "settings.json"

    result = zed_e2e.write_settings(
        path,
        central_url="http://127.0.0.1:43127/mcp",
        token_file=token_file,
        board_id="zed-e2e-test",
        package_spec=client_dir,
        timeout=999,
        uvx_path=Path("/opt/test/uvx"),
    )

    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert result["context_server_timeout"] == 600
    server = result["context_servers"]["pursers"]
    assert server["enabled"] is True
    assert server["settings"] == {
        "central_url": "http://127.0.0.1:43127/mcp",
        "token_file": str(token_file.resolve()),
        "board_id": "zed-e2e-test",
        "package_spec": str(client_dir.resolve()),
        "uvx_path": "/opt/test/uvx",
    }
    assert "not-a-real-token" not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600


def test_log_matcher_accepts_split_chunks_and_keeps_literal_evidence() -> None:
    client = "/checkout/packages/client"
    matcher = zed_e2e.LogMatcher(("pursers-mcp", client))
    matcher.feed(
        "INFO starting context server pursers\n"
        f"DEBUG argv: uvx --from {client} pursers-mcp\n"
        'TRACE outgoing message: {"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        'TRACE outgoing message: {"jsonrpc":"2.0","id":2,"method":"prom'
    )
    matcher.feed(
        'pts/list"}\nTRACE recv: {"prompts":['
        '{"name":"board"},{"name":"create"},{"name":"watch"},'
        '{"name":"evidence"},{"name":"answer"}]}\n'
    )

    assert matcher.complete
    assert matcher.missing() == []
    assert any("starting context server pursers" in line for line in matcher.lines)
    assert any('"name":"answer"' in line for line in matcher.lines)


def test_log_matcher_reports_missing_and_redacts_credentials() -> None:
    matcher = zed_e2e.LogMatcher(("pursers-mcp", "/checkout/packages/client"))
    matcher.feed("INFO Authorization: Bearer secret.value.signature\n")
    matcher.finish()

    assert not matcher.complete
    assert "context server start" in matcher.missing()
    assert all("secret.value.signature" not in line for line in matcher.lines)
    assert zed_e2e.redact("Bearer secret.value.signature") == "Bearer <redacted>"


def test_missing_dependency_message_names_b1_and_b2(tmp_path: Path) -> None:
    client = tmp_path / "packages" / "client"
    client.mkdir(parents=True)
    (client / "pyproject.toml").write_text(
        '[project]\nname = "pursers-client"\nversion = "0"\n', encoding="utf-8"
    )

    try:
        zed_e2e._require_inputs(tmp_path / "integrations" / "zed" / "pursers-mcp", client)
    except zed_e2e.HarnessError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing B1/B2 inputs should fail")

    assert message.startswith("B1/B2 not present:")
    assert "B1 extension.toml" in message
    assert "B2 pursers-mcp entry point" in message


def test_stdio_command_uses_contract_argv_and_no_secret_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(zed_e2e.shutil, "which", lambda name: "/usr/local/bin/uvx")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "must-not-pass")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN_FILE", "/wrong/token")
    client = tmp_path / "packages" / "client"
    token_file = tmp_path / "admin.jwt"

    argv, env = zed_e2e._stdio_command(
        client, "http://127.0.0.1:43127/mcp", token_file, "zed-e2e-test"
    )

    assert argv == [
        "/usr/local/bin/uvx",
        "--from",
        str(client.resolve()),
        "pursers-mcp",
        "--central-url",
        "http://127.0.0.1:43127/mcp",
        "--board",
        "zed-e2e-test",
        "--token-file",
        str(token_file.resolve()),
    ]
    assert "ONBOARD_CENTRAL_TOKEN" not in env
    assert "ONBOARD_CENTRAL_TOKEN_FILE" not in env


def test_central_command_falls_back_to_repository_package(monkeypatch) -> None:
    monkeypatch.delenv("PURSERS_CENTRAL_BIN", raising=False)
    monkeypatch.setattr(
        zed_e2e.shutil,
        "which",
        lambda name: "/usr/local/bin/uvx" if name == "uvx" else None,
    )

    assert zed_e2e._central_command() == [
        "/usr/local/bin/uvx",
        "--from",
        str(zed_e2e.REPOSITORY_ROOT / "packages" / "central"),
        "pursers-central",
    ]


def test_read_default_tools_reads_b2_literal_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "src" / "pursers_client" / "mcp_proxy.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'DEFAULT_TOOLS = frozenset({"board_status", "ticket_create"})\n',
        encoding="utf-8",
    )

    assert zed_e2e.read_default_tools(tmp_path) == {"board_status", "ticket_create"}


def test_seed_throwaway_board_uses_local_client_without_token_argument(
    monkeypatch, tmp_path: Path
) -> None:
    captured = {}
    monkeypatch.setattr(zed_e2e.shutil, "which", lambda name: "/usr/local/bin/uv")

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        Path(argv[-1]).write_text(
            json.dumps(["board_status", "ticket_create"]), encoding="utf-8"
        )

    monkeypatch.setattr(zed_e2e, "_run", fake_run)
    token_file = tmp_path / "admin.jwt"
    advertised = zed_e2e._seed_throwaway_board(
        tmp_path / "client",
        "http://127.0.0.1:43127/mcp",
        token_file,
        "zed-e2e-test",
        {"UV_CACHE_DIR": str(tmp_path / "uv-cache")},
        30,
    )

    assert captured["argv"][-4:-1] == [
        "http://127.0.0.1:43127/mcp",
        str(token_file.resolve()),
        "zed-e2e-test",
    ]
    assert advertised == {"board_status", "ticket_create"}
    assert all("token-value" not in item for item in captured["argv"])
    assert captured["kwargs"]["timeout"] == 30
