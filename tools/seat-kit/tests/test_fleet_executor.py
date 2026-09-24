from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import secrets
import subprocess
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import jsonschema
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_PATH = Path(__file__).parents[1] / "fleet_executor.py"
SPEC = importlib.util.spec_from_file_location("fleet_executor", MODULE_PATH)
assert SPEC and SPEC.loader
executor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = executor
SPEC.loader.exec_module(executor)


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc).timestamp()
FINGERPRINT = "a" * 64


class FakeAdapter:
    def __init__(self) -> None:
        self.observations: dict[str, executor.ServiceObservation] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_readiness = False
        self.fail_stop = False

    def inspect(self, seat_id: str, template: executor.SeatTemplate) -> executor.ServiceObservation:
        self.calls.append(("inspect", seat_id))
        return self.observations.get(
            seat_id, executor.ServiceObservation(False, False, False, True)
        )

    def instantiate(self, seat_id: str, template: executor.SeatTemplate) -> None:
        self.calls.append(("instantiate", seat_id))
        self.observations[seat_id] = executor.ServiceObservation(True, False, False, True)

    def start(self, seat_id: str, template: executor.SeatTemplate) -> None:
        self.calls.append(("start", seat_id))
        self.observations[seat_id] = executor.ServiceObservation(
            True,
            True,
            not self.fail_readiness,
            True,
            f"fake:{seat_id}:123" if not self.fail_readiness else None,
        )

    def drain(self, seat_id: str, template: executor.SeatTemplate) -> None:
        self.calls.append(("drain", seat_id))

    def stop(self, seat_id: str, template: executor.SeatTemplate) -> None:
        self.calls.append(("stop", seat_id))
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.observations[seat_id] = executor.ServiceObservation(True, False, False, True)


class FakeLeases:
    def __init__(self, observation: executor.LeaseObservation | None = None) -> None:
        self.observation = observation or executor.LeaseObservation(True)

    def observe(self, board_id: str, seat_id: str) -> executor.LeaseObservation:
        return self.observation


class FakeReadiness:
    def __init__(
        self, observation: executor.RegistryReadinessObservation | None = None
    ) -> None:
        self.observation = observation or executor.RegistryReadinessObservation(
            True, True, ("pursers",)
        )

    def observe(
        self, board_id: str, seat_id: str, template: executor.SeatTemplate
    ) -> executor.RegistryReadinessObservation:
        return self.observation


class FakePublisher:
    def __init__(self) -> None:
        self.receipts: list[dict[str, Any]] = []

    def publish(self, receipt: dict[str, Any]) -> None:
        if any(
            prior["operation_id"] == receipt["operation_id"]
            and prior["request_digest_sha256"] == receipt["request_digest_sha256"]
            for prior in self.receipts
        ):
            return
        self.receipts.append(dict(receipt))


def template_record(
    repository: Path, seat_root: Path, *, principal: str = "PR-worker-a"
) -> dict[str, Any]:
    return {
        "role": "worker",
        "principal_id": principal,
        "credential_ref": "credential.worker-a",
        "repository_root": str(repository),
        "seat_root": str(seat_root),
        "command": [sys.executable, "-c", "raise SystemExit(0)"],
        "boards": "registry",
        "capabilities": {
            "can_work": True,
            "can_review": False,
            "tier_max": 2,
            "max_parallel": 1,
        },
    }


@pytest.fixture
def runtime(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repositories" / "project"
    seat_root = tmp_path / "seats" / "worker-a"
    repository.mkdir(parents=True)
    seat_root.mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    record = template_record(repository, seat_root)
    template = executor.SeatTemplate.from_record("worker-standard", record)
    policy = executor.ExecutorPolicy(
        authorization_fingerprint_sha256=FINGERPRINT,
        templates={template.template_id: template},
        caller_keys={"butler-local": private.public_key()},
        credential_paths={"credential.worker-a": tmp_path / "worker-a.env"},
        repository_roots=(repository.parent.resolve(),),
        seat_roots=(seat_root.parent.resolve(),),
        board_caps={"pursers": 2},
        host_cap=3,
        signature_skew_s=90,
        mutation_cooldown_s=0,
        failure_backoff_s=0,
    )
    (tmp_path / "worker-a.env").write_text("", encoding="utf-8")
    adapter = FakeAdapter()
    leases = FakeLeases()
    readiness = FakeReadiness()
    publisher = FakePublisher()
    service = executor.FleetExecutor(
        policy,
        executor.ExecutorStore(tmp_path / "state" / "executor.sqlite3"),
        adapter,
        leases,
        readiness,
        publisher,
        clock=lambda: NOW,
    )
    return {
        "service": service,
        "private": private,
        "template": template,
        "adapter": adapter,
        "leases": leases,
        "readiness": readiness,
        "publisher": publisher,
        "tmp_path": tmp_path,
    }


def signed_request(
    runtime: dict[str, Any],
    action: str,
    operation_id: str,
    *,
    seat_id: str = "worker-a",
    generation: int = 1,
    nonce: str | None = None,
) -> dict[str, Any]:
    template = runtime["template"]
    signed_at = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
    request: dict[str, Any] = {
        "schema": executor.SCHEMA,
        "schema_version": 1,
        "message_type": "request",
        "operation_id": operation_id,
        "board_id": "pursers",
        "action": action,
        "seat_id": seat_id,
        "template_id": template.template_id,
        "template_digest_sha256": template.digest_sha256,
        "expected_seat_generation": generation,
        "authorization_fingerprint_sha256": FINGERPRINT,
        "deadline": datetime.fromtimestamp(NOW + 60, timezone.utc).isoformat(),
        "caller_auth": {
            "scheme": "local_ed25519_v1",
            "key_id": "butler-local",
            "nonce": nonce or f"nonce-{operation_id}",
            "signed_at": signed_at,
            "request_digest_sha256": "0" * 64,
            "signature_base64": "",
        },
    }
    digest = executor.request_digest(request)
    request["caller_auth"]["request_digest_sha256"] = digest
    # The digest excludes caller_auth, so updating its digest/signature is stable.
    message = b"\0".join(
        (
            executor.SIGNING_CONTEXT,
            digest.encode("ascii"),
            b"butler-local",
            request["caller_auth"]["nonce"].encode(),
            signed_at.encode(),
        )
    )
    signature = runtime["private"].sign(message)
    request["caller_auth"]["signature_base64"] = base64.b64encode(signature).decode()
    return request


def test_start_creates_ready_seat_and_publishes_bounded_receipt(runtime: dict[str, Any]) -> None:
    result = runtime["service"].handle(signed_request(runtime, "start", "op-start"))

    assert result["outcome"] == "succeeded"
    assert result["committed"] is True
    assert result["process_ref"] == "fake:worker-a:123"
    assert "credential" not in json.dumps(result)
    assert "repository" not in json.dumps(result)
    assert runtime["adapter"].calls == [
        ("inspect", "worker-a"),
        ("instantiate", "worker-a"),
        ("start", "worker-a"),
        ("inspect", "worker-a"),
    ]
    assert runtime["publisher"].receipts == [result]
    schema = json.loads(
        (
            Path(__file__).parents[3]
            / "docs/design/schemas/autonomous-butler-executor-v1.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(result)


def test_identical_operation_replays_without_second_mutation(runtime: dict[str, Any]) -> None:
    request = signed_request(runtime, "start", "op-replay")
    first = runtime["service"].handle(request)
    call_count = len(runtime["adapter"].calls)
    second = runtime["service"].handle(request)

    assert first["replayed"] is False
    assert second == {**first, "replayed": True}
    assert len(runtime["adapter"].calls) == call_count
    assert len(runtime["publisher"].receipts) == 1


def test_operation_payload_change_and_nonce_reuse_fail_closed(runtime: dict[str, Any]) -> None:
    runtime["service"].handle(signed_request(runtime, "start", "op-one", nonce="one-nonce"))
    changed = signed_request(runtime, "inspect", "op-one", nonce="other-nonce")
    with pytest.raises(executor.PolicyError, match="operation_id_payload_changed"):
        runtime["service"].handle(changed)

    reused = signed_request(runtime, "inspect", "op-two", nonce="one-nonce")
    with pytest.raises(executor.PolicyError, match="nonce_reuse"):
        runtime["service"].handle(reused)


def test_stop_requires_known_lease_state_and_refuses_live_lease(runtime: dict[str, Any]) -> None:
    runtime["service"].handle(signed_request(runtime, "start", "op-start"))
    runtime["leases"].observation = executor.LeaseObservation(False)
    unknown = runtime["service"].handle(signed_request(runtime, "stop", "op-stop-unknown"))
    assert unknown["outcome"] == "rejected"
    assert unknown["reason_code"] == "lease_state_unknown"
    assert ("stop", "worker-a") not in runtime["adapter"].calls

    runtime["leases"].observation = executor.LeaseObservation(True, live_work=True)
    live = runtime["service"].handle(signed_request(runtime, "stop", "op-stop-live"))
    assert live["outcome"] == "rejected"
    assert live["reason_code"] == "live_lease"
    assert ("stop", "worker-a") not in runtime["adapter"].calls


def test_drain_then_stop_without_live_lease_increments_generation(runtime: dict[str, Any]) -> None:
    runtime["service"].handle(signed_request(runtime, "start", "op-start"))
    drained = runtime["service"].handle(signed_request(runtime, "drain", "op-drain"))
    stopped = runtime["service"].handle(signed_request(runtime, "stop", "op-stop"))

    assert drained["outcome"] == stopped["outcome"] == "succeeded"
    assert runtime["service"].store.seat("worker-a")["generation"] == 2
    stale = runtime["service"].handle(signed_request(runtime, "start", "op-stale"))
    assert stale["reason_code"] == "seat_generation_mismatch"


def test_unknown_process_identity_blocks_inspect_and_stop(runtime: dict[str, Any]) -> None:
    runtime["service"].handle(signed_request(runtime, "start", "op-start"))
    runtime["adapter"].observations["worker-a"] = executor.ServiceObservation(
        True, True, True, False, "fake:worker-a:123"
    )
    inspected = runtime["service"].handle(signed_request(runtime, "inspect", "op-inspect"))
    stopped = runtime["service"].handle(signed_request(runtime, "stop", "op-stop"))
    assert inspected["reason_code"] == "process_identity_unknown"
    assert stopped["reason_code"] == "process_identity_unknown"


def test_start_rolls_back_failed_readiness(runtime: dict[str, Any]) -> None:
    runtime["adapter"].fail_readiness = True
    result = runtime["service"].handle(signed_request(runtime, "start", "op-fail"))

    assert result["outcome"] == "failed"
    assert result["reason_code"] == "service_operation_failed"
    assert ("stop", "worker-a") in runtime["adapter"].calls
    assert runtime["service"].store.seat("worker-a")["lifecycle"] == "stopped"


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            executor.RegistryReadinessObservation(
                True, False, reason_code="registry_membership_missing"
            ),
            "registry_membership_missing",
        ),
        (
            executor.RegistryReadinessObservation(
                False, reason_code="registry_readiness_unknown"
            ),
            "registry_readiness_unknown",
        ),
    ],
)
def test_start_requires_registry_membership_and_capability_readback_before_mutation(
    runtime: dict[str, Any],
    observation: executor.RegistryReadinessObservation,
    reason: str,
) -> None:
    runtime["readiness"].observation = observation

    result = runtime["service"].handle(signed_request(runtime, "start", f"op-{reason}"))

    assert result["outcome"] == "rejected"
    assert result["reason_code"] == reason
    assert ("instantiate", "worker-a") not in runtime["adapter"].calls


def test_start_rollback_failure_is_recorded_unhealthy(runtime: dict[str, Any]) -> None:
    runtime["adapter"].fail_readiness = True
    runtime["adapter"].fail_stop = True
    result = runtime["service"].handle(signed_request(runtime, "start", "op-fail"))

    assert result["outcome"] == "failed"
    assert runtime["service"].store.seat("worker-a")["lifecycle"] == "unhealthy"


def test_start_does_not_overwrite_unknown_existing_service(runtime: dict[str, Any]) -> None:
    runtime["adapter"].observations["worker-a"] = executor.ServiceObservation(
        True, False, False, False
    )
    result = runtime["service"].handle(signed_request(runtime, "start", "op-drift"))

    assert result["reason_code"] == "process_identity_unknown"
    assert ("instantiate", "worker-a") not in runtime["adapter"].calls


def test_caps_and_principal_independence_are_enforced(runtime: dict[str, Any]) -> None:
    runtime["service"].policy = executor.ExecutorPolicy(
        **{
            **runtime["service"].policy.__dict__,
            "board_caps": {"pursers": 1},
        }
    )
    runtime["service"].handle(signed_request(runtime, "start", "op-first"))
    capped = runtime["service"].handle(
        signed_request(runtime, "start", "op-second", seat_id="worker-b")
    )
    assert capped["reason_code"] == "board_concurrency_cap"

    runtime["service"].policy = executor.ExecutorPolicy(
        **{
            **runtime["service"].policy.__dict__,
            "board_caps": {"pursers": 2},
        }
    )
    duplicate = runtime["service"].handle(
        signed_request(runtime, "start", "op-duplicate", seat_id="worker-b")
    )
    assert duplicate["reason_code"] == "principal_not_independent"


def test_template_must_be_registry_mode_and_paths_stay_inside_roots(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    record = template_record(repository, seat)
    record["boards"] = "pursers"
    with pytest.raises(executor.PolicyError, match="template_not_registry_mode"):
        executor.SeatTemplate.from_record("template", record)

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(executor.PolicyError, match="outside_approved_roots"):
        executor._inside(outside, (repository,), "repository_root")

    invalid_capabilities = template_record(repository, seat)
    invalid_capabilities["capabilities"]["can_review"] = True
    with pytest.raises(executor.PolicyError, match="template_capabilities_invalid"):
        executor.SeatTemplate.from_record("template", invalid_capabilities)


def test_file_lease_provider_fails_closed_on_stale_or_missing_state(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps(
            {
                "boards": {
                    "pursers": {
                        "seats": {
                            "worker-a": {
                                "stale_after": datetime.fromtimestamp(
                                    NOW + 1, timezone.utc
                                ).isoformat(),
                                "work": True,
                                "review": False,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    provider = executor.FileLeaseProvider(path, clock=lambda: NOW)
    assert provider.observe("pursers", "worker-a") == executor.LeaseObservation(
        True, live_work=True
    )
    provider = executor.FileLeaseProvider(path, clock=lambda: NOW + 2)
    assert provider.observe("pursers", "worker-a") == executor.LeaseObservation(False)
    assert provider.observe("pursers", "missing") == executor.LeaseObservation(False)


@pytest.mark.parametrize(
    "record",
    [
        {"stale_after": "2030-01-01T12:01:00+00:00", "review": False},
        {
            "stale_after": "2030-01-01T12:01:00+00:00",
            "work": 0,
            "review": False,
        },
        {
            "stale_after": "2030-01-01T12:01:00+00:00",
            "work": False,
            "review": False,
            "unexpected": False,
        },
    ],
)
def test_file_lease_provider_rejects_incomplete_or_mistyped_records(
    tmp_path: Path, record: dict[str, Any]
) -> None:
    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps({"boards": {"pursers": {"seats": {"worker-a": record}}}}),
        encoding="utf-8",
    )
    provider = executor.FileLeaseProvider(path, clock=lambda: NOW)
    assert provider.observe("pursers", "worker-a") == executor.LeaseObservation(False)


def test_file_lease_provider_rejects_untrusted_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps(
            {
                "boards": {
                    "pursers": {
                        "seats": {
                            "worker-a": {
                                "stale_after": "2030-01-01T12:01:00+00:00",
                                "work": False,
                                "review": False,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o666)
    provider = executor.FileLeaseProvider(path, clock=lambda: NOW)
    assert provider.observe("pursers", "worker-a") == executor.LeaseObservation(False)


def test_registry_readiness_checks_every_selected_active_board(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    seat_root = tmp_path / "seat"
    repository.mkdir()
    seat_root.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat_root)
    )
    seat = {
        "principal_id": template.principal_id,
        "role": template.role,
        "membership_role": "member",
        "lifecycle_status": "active",
        "capabilities": dict(template.capabilities),
    }
    snapshot = {
        "schema": "pursers_registry_readiness_v1",
        "stale_after": datetime.fromtimestamp(NOW + 60, timezone.utc).isoformat(),
        "selected_active_boards": ["pursers", "project-b"],
        "boards": {
            "pursers": {"seats": {"worker-a": seat}},
            "project-b": {"seats": {"worker-a": seat}},
        },
    }
    path = tmp_path / "registry-readiness.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    provider = executor.FileRegistryReadinessProvider(path, clock=lambda: NOW)

    ready = provider.observe("pursers", "worker-a", template)
    assert ready == executor.RegistryReadinessObservation(
        True, True, ("pursers", "project-b"), None
    )

    snapshot["boards"]["project-b"]["seats"] = {}
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    missing = provider.observe("pursers", "worker-a", template)
    assert missing.known and not missing.ready
    assert missing.reason_code == "registry_membership_missing"

    snapshot["boards"]["project-b"]["seats"]["worker-a"] = {
        **seat,
        "capabilities": {**seat["capabilities"], "can_work": False},
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    mismatched = provider.observe("pursers", "worker-a", template)
    assert mismatched.reason_code == "registry_capabilities_mismatch"


def test_registry_readiness_fails_closed_on_stale_or_untrusted_snapshot(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    seat_root = tmp_path / "seat"
    repository.mkdir()
    seat_root.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat_root)
    )
    path = tmp_path / "registry-readiness.json"
    path.write_text(
        json.dumps(
            {
                "schema": "pursers_registry_readiness_v1",
                "stale_after": datetime.fromtimestamp(NOW - 1, timezone.utc).isoformat(),
                "selected_active_boards": ["pursers"],
                "boards": {"pursers": {"seats": {}}},
            }
        ),
        encoding="utf-8",
    )
    provider = executor.FileRegistryReadinessProvider(path, clock=lambda: NOW)
    assert provider.observe("pursers", "worker-a", template).known is False
    path.chmod(0o666)
    assert provider.observe("pursers", "worker-a", template).known is False


def test_systemd_adapter_uses_argument_vector_and_detects_unit_drift(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "exe").symlink_to(Path(sys.executable).resolve())
    (proc / "cmdline").write_bytes(
        b"\0".join(item.encode() for item in (sys.executable, "-c", "raise SystemExit(0)"))
        + b"\0"
    )
    (proc / "stat").write_text(
        "123 (python worker) " + " ".join(["S", *("0" for _ in range(18)), "456"]),
        encoding="utf-8",
    )

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "show" in command:
            unit_name = command[3]
            control_group = f"/test.slice/{unit_name}"
            (proc / "cgroup").write_text(f"0::{control_group}\n", encoding="utf-8")
            unit_path = tmp_path / "systemd" / unit_name
            output = (
                "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=123\n"
                f"FragmentPath={unit_path}\nDropInPaths=\n"
                f"ExecStart={{ path={sys.executable} ; argv[]={sys.executable} -c "
                '"raise SystemExit(0)" ; ignore_errors=no ; start_time=[n/a] ; }}\n'
                f"ControlGroup={control_group}\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    adapter = executor.SystemdUserAdapter(
        tmp_path / "systemd",
        tmp_path / "drain",
        {"credential.worker-a": tmp_path / "worker-a.env"},
        runner=runner,
        proc_root=tmp_path / "proc",
    )
    (tmp_path / "worker-a.env").write_text("", encoding="utf-8")
    adapter.instantiate("worker-a", template)
    adapter.start("worker-a", template)
    observation = adapter.inspect("worker-a", template)

    assert observation.ready and observation.identity_verified
    assert calls[0] == ["systemctl", "--user", "daemon-reload"]
    unit_name = adapter._unit_name("worker-a")
    assert calls[1] == ["systemctl", "--user", "start", unit_name]
    unit = tmp_path / "systemd" / unit_name
    assert f'EnvironmentFile="{tmp_path / "worker-a.env"}"' in unit.read_text()
    unit.write_text(unit.read_text() + "# drift\n", encoding="utf-8")
    assert adapter.inspect("worker-a", template).identity_verified is False


def test_systemd_adapter_rejects_effective_dropin_execstart_drift(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    unit_dir = tmp_path / "systemd"
    unit_name = executor.SystemdUserAdapter._unit_name("worker-a")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "show" not in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        output = (
            "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4321\n"
            f"FragmentPath={unit_dir / unit_name}\n"
            f"DropInPaths={unit_dir / (unit_name + '.d') / 'override.conf'}\n"
            "ExecStart={ path=/usr/bin/false ; argv[]=/usr/bin/false ; "
            "ignore_errors=no ; start_time=[n/a] ; }\n"
            f"ControlGroup=/test.slice/{unit_name}\n"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    adapter = executor.SystemdUserAdapter(
        unit_dir,
        tmp_path / "drain",
        {"credential.worker-a": tmp_path / "worker-a.env"},
        runner=runner,
        proc_root=tmp_path / "proc",
    )
    (tmp_path / "worker-a.env").write_text("", encoding="utf-8")
    adapter.instantiate("worker-a", template)
    observation = adapter.inspect("worker-a", template)
    assert observation.running
    assert not observation.ready
    assert not observation.identity_verified
    assert observation.process_ref is None


def test_systemd_unit_names_do_not_alias_colon_and_dash_seat_ids() -> None:
    colon = executor.SystemdUserAdapter._unit_name("worker:a")
    dash = executor.SystemdUserAdapter._unit_name("worker-a")
    assert colon != dash
    assert len(colon) < 160


def test_receipt_queue_is_idempotent_and_rejects_digest_change(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    publisher = executor.JsonlReceiptPublisher(path)
    receipt = {"operation_id": "op-1", "request_digest_sha256": "a" * 64}
    publisher.publish(receipt)
    publisher.publish(receipt)
    assert path.read_text().count("\n") == 1
    with pytest.raises(RuntimeError, match="receipt_operation_digest_changed"):
        publisher.publish({**receipt, "request_digest_sha256": "b" * 64})


def test_unix_socket_is_owner_only_and_returns_typed_result(
    runtime: dict[str, Any],
) -> None:
    async def exercise() -> None:
        socket_root = Path.home() / ".cache" / "pursers-fx"
        socket_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        socket_path = socket_root / (
            f"fx-{os.getpid()}-{secrets.token_hex(4)}.sock"
        )
        server = executor.UnixSocketServer(socket_path, runtime["service"])
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            assert socket_path.exists()
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(
                executor.canonical_json(
                    signed_request(runtime, "inspect", "op-socket")
                )
                + b"\n"
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert result["schema"] == executor.SCHEMA
            assert result["outcome"] == "succeeded"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            socket_path.unlink(missing_ok=True)

    asyncio.run(exercise())


def test_config_loader_accepts_only_explicit_local_policy(tmp_path: Path) -> None:
    repository = tmp_path / "repositories" / "project"
    seat = tmp_path / "seats" / "worker-a"
    repository.mkdir(parents=True)
    seat.mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    document = {
        "schema": "pursers_fleet_executor_config_v1",
        "authorization_fingerprint_sha256": FINGERPRINT,
        "caller_keys": {"butler-local": base64.b64encode(public).decode()},
        "credential_paths": {"credential.worker-a": str(tmp_path / "worker-a.env")},
        "templates": {"worker-standard": template_record(repository, seat)},
        "repository_roots": [str(repository.parent)],
        "seat_roots": [str(seat.parent)],
        "board_caps": {"pursers": 2},
        "host_cap": 3,
    }
    path = tmp_path / "executor.json"
    (tmp_path / "worker-a.env").write_text("", encoding="utf-8")
    path.write_text(json.dumps(document), encoding="utf-8")
    policy = executor.load_policy(path)
    assert policy.host_cap == 3
    assert tuple(policy.templates) == ("worker-standard",)

    document["unexpected"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(executor.PolicyError, match="executor_config_fields_invalid"):
        executor.load_policy(path)


def _systemd_user_manager_available(
    *,
    runner: Any = subprocess.run,
    platform: str = sys.platform,
    runtime_dir: str | None = os.environ.get("XDG_RUNTIME_DIR"),
) -> bool:
    if platform != "linux" or not runtime_dir:
        return False
    probe = runner(
        ["systemctl", "--user", "is-system-running"],
        check=False,
        text=True,
        capture_output=True,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "running"


def _systemd_user_service_start_available(
    *,
    runner: Any = subprocess.run,
    platform: str = sys.platform,
    runtime_dir: str | None = os.environ.get("XDG_RUNTIME_DIR"),
    pid: int = os.getpid(),
) -> bool:
    if not _systemd_user_manager_available(
        runner=runner,
        platform=platform,
        runtime_dir=runtime_dir,
    ):
        return False
    try:
        probe = runner(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--wait",
                "--collect",
                f"--unit=pursers-ci-probe-{pid}",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return False
    return probe.returncode == 0


def _cleanup_disposable_systemd_user_service(
    adapter: Any,
    seat_id: str,
    template: executor.SeatTemplate,
    unit_dir: Path,
    *,
    started: bool,
    suppress_errors: bool,
    runner: Any = subprocess.run,
) -> None:
    cleanup_error: Exception | None = None
    if started:
        try:
            adapter.stop(seat_id, template)
        except Exception as exc:  # pragma: no branch - preserves the primary failure
            cleanup_error = exc
    try:
        (unit_dir / adapter._unit_name(seat_id)).unlink(missing_ok=True)
    except Exception as exc:
        cleanup_error = cleanup_error or exc
    try:
        reload_result = runner(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            text=True,
            capture_output=True,
        )
        if reload_result.returncode != 0:
            cleanup_error = cleanup_error or RuntimeError("systemd_daemon_reload_failed")
    except Exception as exc:
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None and not suppress_errors:
        raise cleanup_error


class _SystemdUserEnvironmentUnavailable(RuntimeError):
    """The exact disposable unit proved a host-specific user-manager gap."""


def _diagnose_disposable_systemd_start_failure(
    adapter: Any,
    seat_id: str,
    template: executor.SeatTemplate,
    unit_dir: Path,
    *,
    runner: Any = subprocess.run,
) -> str | None:
    """Return a bounded reason only for proven host/layout restrictions."""
    unit_name = adapter._unit_name(seat_id)
    unit_path = unit_dir / unit_name
    try:
        exact_unit = (
            unit_path.is_file()
            and not unit_path.is_symlink()
            and unit_path.read_text(encoding="utf-8") == adapter._unit(seat_id, template)
        )
    except (AttributeError, OSError, RuntimeError, UnicodeError):
        return None
    if not exact_unit:
        return None
    property_names = (
        "LoadState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainCode",
        "ExecMainStatus",
    )
    try:
        shown = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit_name,
                *(f"--property={name}" for name in property_names),
                "--no-pager",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        status = runner(
            [
                "systemctl",
                "--user",
                "status",
                unit_name,
                "--lines=0",
                "--no-pager",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    diagnostic_size = sum(
        len(value.encode("utf-8"))
        for value in (shown.stdout, shown.stderr, status.stdout, status.stderr)
    )
    if shown.returncode not in {0, 1} or diagnostic_size > 4096:
        return None
    try:
        properties = executor.SystemdUserAdapter._show_properties(shown.stdout)
    except ValueError:
        return None
    if set(properties) != set(property_names):
        return None
    signature = tuple(properties[name] for name in property_names)
    if status.returncode == 4 and signature == (
        "not-found",
        "inactive",
        "dead",
        "success",
        "0",
        "0",
    ):
        return "systemd_user_persistent_unit_not_visible"
    if status.returncode == 3 and signature in {
        ("loaded", "failed", "failed", "exit-code", "1", "200"),
        ("loaded", "failed", "failed", "exit-code", "1", "203"),
    }:
        return "systemd_user_service_path_unavailable"
    if status.returncode == 3 and signature == (
        "loaded",
        "failed",
        "failed",
        "resources",
        "0",
        "0",
    ):
        return "systemd_user_service_resources_unavailable"
    return None


def _exercise_disposable_systemd_user_service(
    adapter: Any,
    seat_id: str,
    template: executor.SeatTemplate,
    unit_dir: Path,
    *,
    runner: Any = subprocess.run,
) -> executor.ServiceObservation:
    started = False
    try:
        adapter.instantiate(seat_id, template)
        adapter.start(seat_id, template)
        started = True
        observation = adapter.inspect(seat_id, template)
        assert observation.identity_verified
    except BaseException as primary_error:
        unavailable_reason = None
        if isinstance(primary_error, RuntimeError) and str(primary_error) == "systemd_start_failed":
            unavailable_reason = _diagnose_disposable_systemd_start_failure(
                adapter,
                seat_id,
                template,
                unit_dir,
                runner=runner,
            )
        _cleanup_disposable_systemd_user_service(
            adapter,
            seat_id,
            template,
            unit_dir,
            started=started,
            suppress_errors=unavailable_reason is None,
            runner=runner,
        )
        if unavailable_reason is not None:
            raise _SystemdUserEnvironmentUnavailable(unavailable_reason) from primary_error
        raise
    _cleanup_disposable_systemd_user_service(
        adapter,
        seat_id,
        template,
        unit_dir,
        started=True,
        suppress_errors=False,
        runner=runner,
    )
    return observation


def test_degraded_systemd_user_manager_is_unavailable_before_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "degraded\n", "")

    assert not _systemd_user_manager_available(
        runner=runner,
        platform="linux",
        runtime_dir="/run/user/1000",
    )
    assert calls == [["systemctl", "--user", "is-system-running"]]


def test_running_systemd_user_manager_without_start_capability_is_unavailable() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "systemctl":
            return subprocess.CompletedProcess(command, 0, "running\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Failed to start transient service")

    assert not _systemd_user_service_start_available(
        runner=runner,
        platform="linux",
        runtime_dir="/run/user/1000",
        pid=123,
    )
    assert calls == [
        ["systemctl", "--user", "is-system-running"],
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--collect",
            "--unit=pursers-ci-probe-123",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
    ]


def test_running_systemd_user_manager_with_start_capability_is_available() -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        stdout = "running\n" if command[0] == "systemctl" else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    assert _systemd_user_service_start_available(
        runner=runner,
        platform="linux",
        runtime_dir="/run/user/1000",
        pid=123,
    )


def test_start_failure_cleanup_preserves_primary_error(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    calls: list[str] = []
    reloads: list[list[str]] = []

    class FailingAdapter:
        @staticmethod
        def _unit_name(seat_id: str) -> str:
            return f"{seat_id}.service"

        def instantiate(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("instantiate")
            (unit_dir / self._unit_name(seat_id)).write_text("unit", encoding="utf-8")

        def start(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("start")
            raise RuntimeError("systemd_start_failed")

        def inspect(
            self, seat_id: str, template: executor.SeatTemplate
        ) -> executor.ServiceObservation:
            raise AssertionError("inspect must not run after start failure")

        def stop(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("stop")
            raise RuntimeError("systemd_stop_failed")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        reloads.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    with pytest.raises(RuntimeError, match="systemd_start_failed"):
        _exercise_disposable_systemd_user_service(
            FailingAdapter(),
            "worker-a",
            template,
            unit_dir,
            runner=runner,
        )

    assert calls == ["instantiate", "start"]
    assert not (unit_dir / "worker-a.service").exists()
    assert reloads == [["systemctl", "--user", "daemon-reload"]]


def test_transient_probe_success_but_exact_persistent_unit_is_unavailable(
    tmp_path: Path,
) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    calls: list[list[str]] = []

    class UnavailableAdapter:
        @staticmethod
        def _unit_name(seat_id: str) -> str:
            return f"{seat_id}.service"

        @staticmethod
        def _unit(seat_id: str, template: executor.SeatTemplate) -> str:
            return "unit"

        def instantiate(self, seat_id: str, template: executor.SeatTemplate) -> None:
            (unit_dir / self._unit_name(seat_id)).write_text("unit", encoding="utf-8")

        def start(self, seat_id: str, template: executor.SeatTemplate) -> None:
            raise RuntimeError("systemd_start_failed")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["systemctl", "--user", "is-system-running"]:
            return subprocess.CompletedProcess(command, 0, "running\n", "")
        if command[0] == "systemd-run":
            return subprocess.CompletedProcess(command, 0, "", "")
        if "show" in command:
            output = (
                "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                "Result=success\nExecMainCode=0\nExecMainStatus=0\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if "status" in command:
            return subprocess.CompletedProcess(command, 4, "bounded status", "private stderr")
        return subprocess.CompletedProcess(command, 0, "", "")

    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    assert _systemd_user_service_start_available(
        runner=runner,
        platform="linux",
        runtime_dir="/run/user/1000",
        pid=123,
    )
    with pytest.raises(
        _SystemdUserEnvironmentUnavailable,
        match="^systemd_user_persistent_unit_not_visible$",
    ):
        _exercise_disposable_systemd_user_service(
            UnavailableAdapter(),
            "worker-a",
            template,
            unit_dir,
            runner=runner,
        )

    assert not (unit_dir / "worker-a.service").exists()
    assert calls[-1] == ["systemctl", "--user", "daemon-reload"]


def test_generated_unit_defect_remains_a_failure(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()

    class DefectiveAdapter:
        @staticmethod
        def _unit_name(seat_id: str) -> str:
            return f"{seat_id}.service"

        @staticmethod
        def _unit(seat_id: str, template: executor.SeatTemplate) -> str:
            return "unit"

        def instantiate(self, seat_id: str, template: executor.SeatTemplate) -> None:
            (unit_dir / self._unit_name(seat_id)).write_text("invalid", encoding="utf-8")

        def start(self, seat_id: str, template: executor.SeatTemplate) -> None:
            raise RuntimeError("systemd_start_failed")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "show" in command:
            output = (
                "LoadState=error\nActiveState=inactive\nSubState=dead\n"
                "Result=exit-code\nExecMainCode=0\nExecMainStatus=0\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if "status" in command:
            return subprocess.CompletedProcess(command, 3, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    with pytest.raises(RuntimeError, match="^systemd_start_failed$"):
        _exercise_disposable_systemd_user_service(
            DefectiveAdapter(),
            "worker-a",
            template,
            unit_dir,
            runner=runner,
        )
    assert not (unit_dir / "worker-a.service").exists()


def test_disposable_systemd_success_stops_removes_and_reloads(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    calls: list[str] = []

    class SuccessfulAdapter:
        @staticmethod
        def _unit_name(seat_id: str) -> str:
            return f"{seat_id}.service"

        def instantiate(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("instantiate")
            (unit_dir / self._unit_name(seat_id)).write_text("unit", encoding="utf-8")

        def start(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("start")

        def inspect(
            self, seat_id: str, template: executor.SeatTemplate
        ) -> executor.ServiceObservation:
            calls.append("inspect")
            return executor.ServiceObservation(True, True, True, True, "systemd:test")

        def stop(self, seat_id: str, template: executor.SeatTemplate) -> None:
            calls.append("stop")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append("reload")
        return subprocess.CompletedProcess(command, 0, "", "")

    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    template = executor.SeatTemplate.from_record(
        "worker-standard", template_record(repository, seat)
    )
    observation = _exercise_disposable_systemd_user_service(
        SuccessfulAdapter(),
        "worker-a",
        template,
        unit_dir,
        runner=runner,
    )
    assert observation.ready and observation.identity_verified
    assert calls == ["instantiate", "start", "inspect", "stop", "reload"]
    assert not (unit_dir / "worker-a.service").exists()


@pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("XDG_RUNTIME_DIR"),
    reason="real disposable systemd user manager is unavailable",
)
def test_real_disposable_systemd_user_service_when_available(tmp_path: Path) -> None:
    if not _systemd_user_service_start_available():
        pytest.skip("real disposable systemd user service start is unavailable")
    repository = tmp_path / "repository"
    seat = tmp_path / "seat"
    repository.mkdir()
    seat.mkdir()
    record = template_record(repository, seat)
    (tmp_path / "empty.env").write_text("", encoding="utf-8")
    template = executor.SeatTemplate.from_record("worker-standard", record)
    unit_dir = Path.home() / ".config/systemd/user"
    adapter = executor.SystemdUserAdapter(
        unit_dir,
        tmp_path / "drain",
        {"credential.worker-a": tmp_path / "empty.env"},
    )
    seat_id = f"disposable-{os.getpid()}"
    try:
        _exercise_disposable_systemd_user_service(adapter, seat_id, template, unit_dir)
    except _SystemdUserEnvironmentUnavailable as exc:
        pytest.skip(str(exc))
