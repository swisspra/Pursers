from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import pytest

from . import pair_aionui, runner


class FakeDriver:
    def __init__(
        self,
        *,
        info: dict[str, object] | None = None,
        code: str = "ABCD-23",
        result: dict[str, object] | None = None,
    ) -> None:
        self.info = info or {"desktop_authenticated": True, "has_active_code": True}
        self.code = code
        self.result = result or {"paired": True, "code_consumed": True}
        self.calls: list[str] = []

    def get_pair_info(self, runtime: pair_aionui.Runtime) -> dict[str, object]:
        self.calls.append("pair-info")
        return self.info

    def read_pairing_code(self, timeout_s: float) -> str:
        self.calls.append("read-code")
        return self.code

    def begin_browser_pairing(
        self, runtime: pair_aionui.Runtime, code: str, timeout_s: float
    ) -> object:
        self.calls.append(f"begin:{code}")
        return object()

    def approve_native_dialog(self, timeout_s: float) -> None:
        self.calls.append("approve")

    def finish_browser_pairing(self, handle: object, timeout_s: float) -> dict[str, object]:
        self.calls.append("finish")
        return self.result

    def cancel_browser_pairing(self, handle: object) -> None:
        self.calls.append("cancel")


def _runtime(tmp_path: Path) -> pair_aionui.Runtime:
    executable = tmp_path / "ego-browser"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    return pair_aionui.Runtime(
        origin="http://127.0.0.1:25808",
        core_port=25808,
        data_dir=tmp_path,
        observer_dir=tmp_path,
        ego_browser=executable,
        task_space="acceptance-space",
    )


def test_pair_runs_gate_in_order_and_printable_proof_has_no_code(tmp_path: Path) -> None:
    driver = FakeDriver()
    proof = pair_aionui.pair(_runtime(tmp_path), driver)

    assert driver.calls == [
        "pair-info",
        "read-code",
        "begin:ABCD-23",
        "approve",
        "finish",
    ]
    assert proof["paired"] is True
    assert proof["code_consumed"] is True
    assert proof["pairing_mode"] == "automated_preauthorized"
    assert proof["core_port"] == 25808
    assert len(proof["tool_sha256"]) == 64
    assert "ABCD-23" not in json.dumps(proof)


@pytest.mark.parametrize(
    ("info", "message"),
    [
        ({"desktop_authenticated": False, "has_active_code": True}, "must be authenticated"),
        ({"desktop_authenticated": True, "has_active_code": False}, "no active pairing code"),
    ],
)
def test_pair_rejects_missing_desktop_precondition(
    tmp_path: Path, info: dict[str, object], message: str
) -> None:
    with pytest.raises(pair_aionui.PairingError, match=message):
        pair_aionui.pair(_runtime(tmp_path), FakeDriver(info=info))


def test_pair_requires_consumed_code_result(tmp_path: Path) -> None:
    with pytest.raises(pair_aionui.PairingError, match="consumed-code"):
        pair_aionui.pair(
            _runtime(tmp_path),
            FakeDriver(result={"paired": True, "code_consumed": False}),
        )


def test_pair_cancels_browser_when_native_confirmation_fails(tmp_path: Path) -> None:
    class FailingApprovalDriver(FakeDriver):
        def approve_native_dialog(self, timeout_s: float) -> None:
            self.calls.append("approve")
            raise pair_aionui.PairingError("dialog mismatch")

    driver = FailingApprovalDriver()
    with pytest.raises(pair_aionui.PairingError, match="dialog mismatch"):
        pair_aionui.pair(_runtime(tmp_path), driver)
    assert driver.calls[-1] == "cancel"


def test_load_runtime_uses_handoff_origin_data_dir_and_observer(tmp_path: Path) -> None:
    handoff_root = tmp_path / "handoff"
    data_dir = handoff_root / "aioncore-data"
    observer_dir = tmp_path / "observer"
    data_dir.mkdir(parents=True)
    observer_dir.mkdir()
    executable = tmp_path / "ego-browser"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    (observer_dir / "observer.json").write_text(
        json.dumps(
            {
                "backend": {
                    "kind": "ego-browser",
                    "command": str(executable),
                    "task_space": 17,
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = handoff_root / "handoff.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "origin": "http://127.0.0.1:32123",
                "approved_observer": {"install_dir": str(observer_dir)},
            }
        ),
        encoding="utf-8",
    )

    runtime = pair_aionui.load_runtime(manifest)

    assert runtime.core_port == 32123
    assert runtime.data_dir == data_dir
    assert runtime.observer_dir == observer_dir
    assert runtime.ego_browser == executable
    assert runtime.task_space == 17


def test_ego_script_reuses_task_space_and_webui_login(tmp_path: Path) -> None:
    script = pair_aionui._ego_pair_script(_runtime(tmp_path), "ABCD-23", 5)
    assert "useOrCreateTaskSpace" in script
    assert "openOrReuseTab" in script
    assert "http://127.0.0.1:25808/#/login" in script
    assert "requestSubmit" in script
    assert "already_paired: true" in script
    assert "playwright" not in script.lower()


def test_pairing_proof_schema_is_exact() -> None:
    proof = {
        "paired": True,
        "pairing_mode": "automated_preauthorized",
        "tool_version": "1.0.0",
        "tool_sha256": "a" * 64,
        "core_port": 25808,
        "timestamp": "2026-09-13T12:00:00Z",
        "code_consumed": True,
    }
    assert pair_aionui.validate_pairing_proof(proof, expected_port=25808) == proof
    with pytest.raises(ValueError, match="fields"):
        pair_aionui.validate_pairing_proof({**proof, "code": "ABCD-23"})
    with pytest.raises(ValueError, match="does not match"):
        pair_aionui.validate_pairing_proof(proof, expected_port=25809)
    with pytest.raises(ValueError, match="does not match this tool"):
        pair_aionui.validate_pairing_proof(proof, expected_tool_sha256="b" * 64)


def test_capture_attaches_valid_pairing_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observer = tmp_path / "observer"
    observer.mkdir()
    monkeypatch.setattr(runner, "_observer_command", lambda _path: tmp_path / "observer.py")
    payload = {
        "screenshot_base64": base64.b64encode(b"png").decode(),
        "observation_id": "pairing-observation",
        "target": {"base_url": "http://127.0.0.1:25808", "board_id": "board"},
        "surface_id": "aionui",
        "host_product": "AionUi",
        "host_version": "1.0.0",
        "host_build": "build",
        "host_identity_source": "signed-aionui-webui-listener",
        "candidate_commit": "0" * 40,
        "captured_at": "2026-09-13T12:00:01Z",
        "page_url": "http://127.0.0.1:25808/page",
        "snapshot": {"role": "document"},
        "attestation": "a" * 64,
        "attestation_nonce": "b" * 64,
    }
    monkeypatch.setattr(runner, "_run_observer", lambda _argv: payload)
    proof = {
        "paired": True,
        "pairing_mode": "automated_preauthorized",
        "tool_version": "1.0.0",
        "tool_sha256": pair_aionui.tool_sha256(),
        "core_port": 25808,
        "timestamp": "2026-09-13T12:00:00Z",
        "code_consumed": True,
    }
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    assertions_path = tmp_path / "assertions.json"
    assertions_path.write_text(json.dumps([{"kind": "text", "value": "Home"}]), encoding="utf-8")
    evidence = tmp_path / "evidence"
    args = argparse.Namespace(
        observer=str(observer),
        evidence=str(evidence),
        observation="pairing-observation",
        surface="aionui",
        target="http://127.0.0.1:25808",
        board="board",
        commit="0" * 40,
        page="http://127.0.0.1:25808/page",
        assertions=str(assertions_path),
        attestation_nonce=None,
        pairing_proof=str(proof_path),
    )

    assert runner.capture(args) == 0
    capsys.readouterr()
    receipt = json.loads(
        (evidence / "observations" / "pairing-observation.json").read_text(encoding="utf-8")
    )
    assert receipt["pairing"] == proof


def test_accessibility_code_parser_requires_one_exact_six_character_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pair_aionui.MacOSPairingDriver,
        "_osascript",
        staticmethod(lambda _script, _timeout: "Pairing code, ABCD-23, 59 seconds"),
    )
    assert pair_aionui.MacOSPairingDriver().read_pairing_code(1) == "ABCD-23"

    monkeypatch.setattr(
        pair_aionui.MacOSPairingDriver,
        "_osascript",
        staticmethod(lambda _script, _timeout: "ABCD-23, EFGH-45"),
    )
    with pytest.raises(pair_aionui.PairingError, match="exactly one"):
        pair_aionui.MacOSPairingDriver().read_pairing_code(1)
