from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_dashboard_evidence_test", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


class Cache:
    def labels(self) -> list[str]:
        return ["default"]


def _trace(
    tmp_path: Path, *, initial_output: bytes = b"", max_bytes: int = 65_536
) -> tuple[Any, Path]:
    output = tmp_path / "fleet-evidence.jsonl"
    if initial_output:
        output.write_bytes(initial_output)
        output.chmod(0o600)
    config = tmp_path / "trace.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "output_path": str(output),
                "max_bytes": max_bytes,
                "runtime_id": "fleet-runtime-test",
                "board_id": "sandbox-board",
                "surface": "fleet",
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return dashboard.EvidenceTrace.from_config(config, MODULE_PATH), output


def _headers(action: bytes | None = None) -> dict[str, str]:
    result = {
        "X-Pursers-Observation-Id": "fleet.attention-state",
        "X-Pursers-Run-Id": "run-1",
        "X-Pursers-Action-Id": "save-attention",
        "X-Pursers-Entity-Id": "TK-123",
    }
    if action is not None:
        result["X-Pursers-Action-SHA256"] = hashlib.sha256(action).hexdigest()
        result["Content-Type"] = "application/json"
    return result


def _call(
    base_url: str,
    method: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], Any]:
    request = urllib.request.Request(
        base_url + "/api/attention",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read()), response.headers


def test_real_attention_handler_emits_actual_sanitized_evidence(tmp_path: Path) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    secret = "must-not-appear-in-evidence"
    action = json.dumps(
        {"TK-123": {"state": "ack", "note": secret}}, separators=(",", ":")
    ).encode()
    headers = _headers(action)
    try:
        before_status, before, before_headers = _call(
            base_url, "GET", headers=_headers()
        )
        status, result, response_headers = _call(
            base_url, "POST", body=action, headers=headers
        )
        after_status, after, after_headers = _call(
            base_url, "GET", headers=_headers()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert before_status == status == after_status == 200
    assert before["items"] == {}
    assert result["items"] == {"TK-123": {"state": "ack", "note": secret}}
    assert after["items"] == result["items"]
    assert json.loads((state_dir / "attention-state.json").read_text()) == result["items"]
    assert result["_evidence"]["outcome"] == "succeeded"
    assert result["_evidence"]["effect"] == "attention_state_changed"
    assert result["_evidence"]["changed"] is True
    assert result["_evidence"]["result_sha256"] == hashlib.sha256(
        dashboard._json_bytes({"items": result["items"]})
    ).hexdigest()
    assert before["_evidence"]["effect"] == "attention_state_unchanged"
    assert after["_evidence"]["effect"] == "attention_state_unchanged"
    for returned in (before_headers, response_headers, after_headers):
        assert returned["X-Pursers-Observation-Id"] == "fleet.attention-state"
        assert returned["X-Pursers-Run-Id"] == "run-1"
        assert returned["X-Pursers-Action-Id"] == "save-attention"
        assert returned["X-Pursers-Entity-Id"] == "TK-123"

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        key: value
        for key, value in result["_evidence"].items()
        if key != "log_emitted"
    }
    assert record["action_sha256"] == hashlib.sha256(action).hexdigest()
    assert record["pid"] == os.getpid()
    assert len(record["candidate_commit"]) == 40
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert secret not in lines[0]


def test_trace_rejects_bad_correlation_digest_replay_and_forged_success(
    tmp_path: Path,
) -> None:
    trace, output = _trace(tmp_path)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    good = b'{"TK-123":{"state":"ack"}}'
    try:
        missing = _headers(good)
        missing.pop("X-Pursers-Entity-Id")
        assert "_evidence" not in _call(base_url, "POST", body=good, headers=missing)[1]

        malformed = _headers(good)
        malformed["X-Pursers-Run-Id"] = "bad id with spaces"
        assert "_evidence" not in _call(base_url, "POST", body=good, headers=malformed)[1]

        wrong_digest = _headers(good)
        wrong_digest["X-Pursers-Action-SHA256"] = "0" * 64
        assert "_evidence" not in _call(
            base_url, "POST", body=good, headers=wrong_digest
        )[1]

        first = _call(base_url, "POST", body=good, headers=_headers(good))[1]
        replay = _call(base_url, "POST", body=good, headers=_headers(good))[1]
        assert first["_evidence"]["log_emitted"] is True
        assert "_evidence" not in replay

        forged = {f"item-{index}": index for index in range(500)}
        forged["outcome"] = "succeeded"
        forged_body = json.dumps(forged, separators=(",", ":")).encode()
        forged_headers = _headers(forged_body)
        forged_headers["X-Pursers-Action-Id"] = "forged-success"
        failed_status, failed, _ = _call(
            base_url, "POST", body=forged_body, headers=forged_headers
        )

        unrelated = urllib.request.Request(
            base_url + "/api/centrals", headers=_headers(), method="GET"
        )
        with urllib.request.urlopen(unrelated) as response:
            unrelated_body = json.loads(response.read())
            assert response.headers.get("X-Pursers-Run-Id") is None
        assert "_evidence" not in unrelated_body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert failed_status == 400
    assert failed["_evidence"]["outcome"] == "failed"
    assert failed["_evidence"]["effect"] == "attention_state_unchanged"
    assert failed["_evidence"]["changed"] is False
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["action_id"] for record in records] == [
        "save-attention",
        "forged-success",
    ]


def test_trace_requires_private_config_and_real_git_entrypoint(tmp_path: Path) -> None:
    config = tmp_path / "trace.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "output_path": str(tmp_path / "trace.jsonl"),
                "max_bytes": 65_536,
                "runtime_id": "fleet-runtime-test",
                "board_id": "sandbox-board",
                "surface": "fleet",
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o644)
    with pytest.raises(dashboard.EvidenceTraceConfigError, match="0600"):
        dashboard.EvidenceTrace.from_config(config, MODULE_PATH)

    config.chmod(0o600)
    unrelated = tmp_path / "not-fleet.py"
    unrelated.write_text("print('not Fleet')\n", encoding="utf-8")
    with pytest.raises(
        dashboard.EvidenceTraceConfigError, match="checkout identity"
    ):
        dashboard.EvidenceTrace.from_config(config, unrelated)


def test_full_trace_file_is_fail_passive_for_real_action(tmp_path: Path) -> None:
    initial = b"{}\n" * 1_300
    trace, output = _trace(tmp_path, initial_output=initial, max_bytes=4_096)
    state_dir = tmp_path / "state"
    seats = dashboard.SeatConfigManager(
        state_dir / "seats.json", state_dir=state_dir
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(), worker_manager=SimpleNamespace(), seat_manager=seats, evidence_trace=trace
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    action = b'{"TK-123":{"state":"ack"}}'
    try:
        status, result, headers = _call(
            f"http://127.0.0.1:{server.server_port}",
            "POST",
            body=action,
            headers=_headers(action),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert status == 200
    assert result == {"items": {"TK-123": {"state": "ack"}}}
    assert headers.get("X-Pursers-Run-Id") is None
    assert output.read_bytes() == initial
    assert json.loads((state_dir / "attention-state.json").read_text()) == result["items"]
