"""Plan/apply service for native and ACP managed seat runners."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ACP_DIR = Path(__file__).resolve().parents[1] / "acp-seat"
if str(ACP_DIR) not in sys.path:
    sys.path.insert(0, str(ACP_DIR))

from runner_catalog import (
    CatalogError,
    canonical_json,
    load_cached_catalog,
    persist_selection_lock,
    refresh_catalog,
)
from runner_installer import (
    InstallError,
    install_binary,
    installation_preview,
    resolved_command,
)
from runner_preset import PresetError, dumps_preset, normalize_preset

PLAN_TTL_S = 600
MAX_PLANS = 50


class RunnerSetupError(RuntimeError):
    """A runner plan or activation violated the setup contract."""


def compatibility_matrix() -> list[dict[str, Any]]:
    return [
        {
            "runner": "native-codex",
            "platforms": ["darwin", "linux", "windows"],
            "account": "native account/profile reference",
            "headless": True,
            "session_options": "native host settings",
            "status": "supported",
        },
        {
            "runner": "native-goose",
            "platforms": ["darwin", "linux", "windows"],
            "account": "native account reference",
            "headless": True,
            "session_options": "native host settings",
            "status": "supported",
        },
        {
            "runner": "registry-acp",
            "platforms": ["darwin"],
            "account": "dedicated provider account reference",
            "headless": True,
            "session_options": "negotiated ACP configOptions",
            "status": "provider-auth-needs-isolated-egress",
            "limitation": (
                "production ACP sandbox denies network and operator HOME; an account "
                "resolver must prove a narrow provider-auth boundary before activation"
            ),
        },
        {
            "runner": "zed-acp-client",
            "platforms": ["darwin", "linux", "windows"],
            "account": "Zed-managed",
            "headless": False,
            "session_options": "client-managed",
            "status": "optional-gui-not-managed",
        },
    ]


class RunnerSetupManager:
    """Keep catalog reads side-effect free and mutations behind expiring plans."""

    def __init__(
        self,
        state_root: Path,
        *,
        account_probe: Callable[[str], Mapping[str, Any]] | None = None,
        seat_probe: Callable[[str], Mapping[str, Any]] | None = None,
        executor: Callable[[Path], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = state_root.expanduser().resolve()
        self.cache_path = self.root / "registry-cache.json"
        self.install_root = self.root / "install-cache"
        self.preset_root = self.root / "presets"
        self.lock_root = self.root / "locks"
        self.template_root = self.root / "templates"
        self.account_probe = account_probe or self._account_needs_human
        self.seat_probe = seat_probe or (lambda _name: {})
        self.executor = executor
        self.clock = clock
        self._plans: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def catalog(self, *, target: str) -> dict[str, Any]:
        view = load_cached_catalog(self.cache_path)
        return {
            "schema": "pursers_acp_catalog_view_v1",
            "registry_revision": view.registry_revision,
            "fetched_at_epoch": view.fetched_at_epoch,
            "stale": view.stale,
            "agents": view.list_agents(target),
            "compatibility": compatibility_matrix(),
        }

    def refresh(self, *, target: str) -> dict[str, Any]:
        view = refresh_catalog(self.cache_path)
        return {
            "schema": "pursers_acp_catalog_view_v1",
            "registry_revision": view.registry_revision,
            "fetched_at_epoch": view.fetched_at_epoch,
            "stale": view.stale,
            "agents": view.list_agents(target),
            "compatibility": compatibility_matrix(),
        }

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping) or set(request) != {"preset", "runtime"}:
            raise ValueError("request must contain only preset and runtime")
        preset = normalize_preset(request["preset"])
        runtime = self._runtime(request["runtime"], preset)
        runner = preset["runner"]
        resolved: dict[str, Any] | None = None
        install: dict[str, Any] | None = None
        account: dict[str, Any] | None = None
        template: dict[str, Any] | None = None
        executor_template: dict[str, Any] | None = None
        if runner["kind"] == "acp":
            pin = runner["catalog_pin"]
            view = load_cached_catalog(self.cache_path)
            if view.registry_revision != pin["registry_revision"]:
                raise RunnerSetupError("catalog_pin_stale")
            resolved = view.resolve(
                pin["agent_id"],
                pin["agent_version"],
                pin["platform"],
                pin["distribution_kind"],
            )
            install = installation_preview(resolved, self.install_root)
            account = self._account_status(runner["account_ref"])
            template = self._template(preset, runtime, resolved, install)
            executor_template = self._executor_template(preset, runtime)
        seat_state = dict(self.seat_probe(preset["seat"]["agent_name"]) or {})
        activation = self._activation_state(preset, account, install, seat_state)
        basis = {
            "preset": preset,
            "runtime": runtime,
            "resolved": resolved,
            "install": install,
            "account": account,
            "template": template,
            "executor_template": executor_template,
            "seat_state": seat_state,
            "activation": activation,
        }
        digest = hashlib.sha256(canonical_json(basis)).hexdigest()
        plan_id = uuid.uuid4().hex
        with self._lock:
            while len(self._plans) >= MAX_PLANS:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan_id] = {
                "digest": digest,
                "expires_at": self.clock() + PLAN_TTL_S,
                "basis": basis,
            }
        return {
            "schema": "pursers_runner_setup_plan_v1",
            "plan_id": plan_id,
            "digest": digest,
            "expires_in_s": PLAN_TTL_S,
            **basis,
        }

    def apply(self, *, plan_id: str, digest: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not isinstance(digest, str):
            raise TypeError("plan_id and digest are required")
        with self._lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None:
            raise KeyError(plan_id)
        if plan["expires_at"] <= self.clock():
            raise RunnerSetupError("runner_setup_plan_expired")
        if not hmac.compare_digest(plan["digest"], digest):
            raise RunnerSetupError("runner_setup_plan_digest_mismatch")
        basis = plan["basis"]
        preset = basis["preset"]
        agent_name = preset["seat"]["agent_name"]
        if dict(self.seat_probe(agent_name) or {}) != basis["seat_state"]:
            raise RunnerSetupError("seat_state_changed_replan")
        account = basis["account"]
        if (
            account is not None
            and dict(self.account_probe(preset["runner"]["account_ref"]) or {})
            != account
        ):
            raise RunnerSetupError("account_state_changed_replan")
        if basis["activation"]["state"] == "needs_human":
            return {
                "ok": False,
                "state": "needs_human",
                "decision": basis["activation"]["decision"],
                "changed": [],
            }
        self._ensure_root()
        changed: list[str] = []
        install_result = None
        resolved = basis["resolved"]
        if resolved is not None and resolved["distribution"]["kind"] == "binary":
            install_result = install_binary(resolved, self.install_root)
            changed.append(install_result["install_root"])
        if resolved is not None:
            lock_path = self.lock_root / f"{agent_name}.json"
            if persist_selection_lock(lock_path, resolved):
                changed.append(str(lock_path))
        preset_path = self.preset_root / f"{agent_name}.json"
        if self._replace_private(preset_path, dumps_preset(preset).encode("utf-8")):
            changed.append(str(preset_path))
        template_path = None
        executor_template_path = None
        if basis["template"] is not None:
            template = dict(basis["template"])
            if install_result is not None:
                template["acp"]["command"] = install_result["command"]
            elif resolved is not None:
                template["acp"]["command"] = resolved_command(resolved, None)
            template_path = self.template_root / f"{agent_name}.json"
            if self._replace_private(template_path, canonical_json(template) + b"\n"):
                changed.append(str(template_path))
        if basis["executor_template"] is not None:
            executor_template_path = (
                self.template_root / f"{agent_name}.executor-template.json"
            )
            if self._replace_private(
                executor_template_path,
                canonical_json(basis["executor_template"]) + b"\n",
            ):
                changed.append(str(executor_template_path))
        activation = dict(basis["activation"])
        if activation["state"] == "ready" and executor_template_path is not None:
            if self.executor is None:
                activation = {
                    "state": "ready_for_existing_executor",
                    "template": str(executor_template_path),
                    "requires": "operator-approved executor policy template and signed start",
                }
            else:
                activation = dict(self.executor(executor_template_path))
        return {
            "ok": True,
            "state": activation["state"],
            "activation": activation,
            "changed": changed,
            "backup_paths": [
                str(path.with_name(path.name + ".previous"))
                for path in (preset_path, template_path, executor_template_path)
                if path is not None
                and path.with_name(path.name + ".previous").is_file()
            ],
            "preset": str(preset_path),
            "template": str(template_path) if template_path else None,
            "executor_template": (
                str(executor_template_path) if executor_template_path else None
            ),
            "install": install_result,
            "rollback": "stop the managed seat and restore the prior preset/template backup",
        }

    def _runtime(self, raw: Any, preset: Mapping[str, Any]) -> dict[str, Any]:
        if preset["runner"]["kind"] == "native":
            if raw != {}:
                raise ValueError(
                    "native runner setup does not accept ACP runtime references"
                )
            return {}
        allowed = {
            "central_url",
            "token_file",
            "expected_agent_id",
            "expected_principal_id",
            "repository",
            "work_root",
            "seat_root",
            "credential_ref",
            "base_ref",
            "policy_file",
            "git_user_name",
            "git_user_email",
        }
        required = allowed - {
            "base_ref",
            "policy_file",
            "git_user_name",
            "git_user_email",
        }
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or not set(raw) <= allowed
        ):
            raise ValueError(
                "runtime references are incomplete or contain unexpected fields"
            )
        result: dict[str, Any] = {}
        for field in required:
            value = raw[field]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 4096
                or "\x00" in value
            ):
                raise ValueError(f"runtime.{field} is invalid")
            result[field] = value
        result["base_ref"] = raw.get("base_ref", "origin/main")
        result["policy_file"] = raw.get("policy_file")
        result["git_user_name"] = raw.get("git_user_name", "Pursers ACP Seat")
        result["git_user_email"] = raw.get("git_user_email", "acp-seat@pursers.invalid")
        if preset["runner"]["kind"] == "acp":
            self._private_reference(result["token_file"], "token_file")
            if result["policy_file"] is not None:
                self._private_reference(result["policy_file"], "policy_file")
            if not Path(result["repository"]).expanduser().resolve().is_dir():
                raise ValueError("runtime.repository is unavailable")
        return result

    @staticmethod
    def _private_reference(raw: str, field: str) -> None:
        path = Path(raw).expanduser().resolve()
        try:
            info = path.stat()
        except OSError as exc:
            raise ValueError(f"runtime.{field} is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError(f"runtime.{field} must reference a mode-0600 regular file")

    def _template(
        self,
        preset: Mapping[str, Any],
        runtime: Mapping[str, Any],
        resolved: Mapping[str, Any],
        install: Mapping[str, Any],
    ) -> dict[str, Any]:
        command = (
            resolved_command(
                resolved,
                Path(install["install_root"])
                if install.get("ready") and install.get("install_root")
                else None,
            )
            if resolved["distribution"]["kind"] != "binary" or install.get("ready")
            else list(resolved["launch"]["argv"])
        )
        return {
            "seat": {
                "central_url": runtime["central_url"],
                "board_id": preset["seat"]["board_id"],
                "agent_name": preset["seat"]["agent_name"],
                "expected_agent_id": runtime["expected_agent_id"],
                "expected_principal_id": runtime["expected_principal_id"],
                "git_user_name": runtime["git_user_name"],
                "git_user_email": runtime["git_user_email"],
                "token_file": runtime["token_file"],
            },
            "acp": {
                "command": command,
                "repository": runtime["repository"],
                "base_ref": runtime["base_ref"],
                "work_root": runtime["work_root"],
                "policy_file": runtime["policy_file"],
                "session_options": dict(preset["session_options"]),
            },
            "lease_interval_s": 300,
            "wait_timeout_s": 180,
        }

    def _account_status(self, account_ref: str) -> dict[str, Any]:
        raw = dict(self.account_probe(account_ref) or {})
        allowed = {"status", "provider", "network", "auth_scope", "reason"}
        if (
            not raw
            or not set(raw) <= allowed
            or raw.get("status") not in {"ready", "needs_human", "unavailable"}
        ):
            raise RunnerSetupError("account_probe_invalid")
        return raw

    def _executor_template(
        self, preset: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> dict[str, Any]:
        agent_name = preset["seat"]["agent_name"]
        runtime_config = self.template_root / f"{agent_name}.json"
        return {
            "role": "acp_worker",
            "principal_id": runtime["expected_principal_id"],
            "credential_ref": runtime["credential_ref"],
            "repository_root": runtime["repository"],
            "seat_root": runtime["seat_root"],
            "command": [
                sys.executable,
                str(ACP_DIR / "pursers_acp_seat.py"),
                "--config",
                str(runtime_config),
            ],
            "boards": "registry",
            "capabilities": {
                "can_work": True,
                "can_review": False,
                "tier_max": 2,
                "max_parallel": 1,
            },
        }

    @staticmethod
    def _account_needs_human(account_ref: str) -> Mapping[str, Any]:
        return {
            "status": "needs_human",
            "reason": (
                f"account reference {account_ref!r} has no verified narrow auth/egress "
                "adapter; choose a dedicated provider boundary without operator HOME access"
            ),
        }

    @staticmethod
    def _activation_state(
        preset: Mapping[str, Any],
        account: Mapping[str, Any] | None,
        install: Mapping[str, Any] | None,
        seat_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        if preset["runner"]["kind"] == "native":
            return {"state": "native_preserved", "automatic_migration": False}
        if preset["seat"]["role"] != "worker":
            return {
                "state": "needs_human",
                "decision": (
                    "the current ACP seat runtime claims work tickets only; keep the "
                    "independent reviewer on its native runner until an ACP review adapter is reviewed"
                ),
            }
        if account is None or account.get("status") != "ready":
            return {
                "state": "needs_human",
                "decision": account.get("reason")
                if account
                else "account reference unavailable",
            }
        if install and install.get("blocked_reason"):
            return {"state": "needs_human", "decision": install["blocked_reason"]}
        if (
            install
            and install.get("action") == "use_pinned_package_launcher"
            and not install.get("ready")
        ):
            return {"state": "needs_human", "decision": "package_launcher_unavailable"}
        if seat_state.get("active_lease") or seat_state.get("running"):
            return {
                "state": "deferred",
                "reason": "managed seat is running or holds an active lease; drain before activation",
            }
        return {"state": "ready"}

    def _ensure_root(self) -> None:
        for path in (self.root, self.preset_root, self.lock_root, self.template_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    @staticmethod
    def _replace_private(path: Path, payload: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            previous = path.read_bytes()
            if previous == payload:
                return False
            backup = path.with_name(path.name + ".previous")
            backup_tmp = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
            backup_descriptor = os.open(
                backup_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(backup_descriptor, "wb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(backup_tmp, backup)
            finally:
                backup_tmp.unlink(missing_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True


__all__ = [
    "CatalogError",
    "InstallError",
    "PresetError",
    "RunnerSetupError",
    "RunnerSetupManager",
    "compatibility_matrix",
]
