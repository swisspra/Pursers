"""Bounded auto-onboarding for intake items that target unknown projects."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


MAX_FINDING_CHARS = 240
MAX_SOURCE_PROJECTS = 2_000
MAX_RETRY_BACKOFF_S = 7 * 86_400
PROJECT_HINT_RE = re.compile(r"^[^\x00-\x1f/\\]{1,120}$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _load_project_lifecycle() -> Any:
    name = "pursers_project_lifecycle"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parents[1] / "fleet-dashboard" / "project_lifecycle.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("project lifecycle module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class RepositoryResolution:
    repository_url: str
    integration_ref: str = "main"


@dataclass(frozen=True)
class IntakeSourcePolicy:
    source_id: str
    domain: str
    projects_root: Path
    auto_onboard: bool
    per_cycle_cap: int
    retry_limit: int
    retry_backoff_s: int
    repositories: Mapping[str, RepositoryResolution]


@dataclass(frozen=True)
class PendingProjectItem:
    item_id: str
    source_id: str
    project_hint: str


@dataclass(frozen=True)
class OnboardingResult:
    item_id: str
    source_id: str
    project_hint: str
    status: str
    board_id: str | None = None
    finding: str | None = None


@dataclass(frozen=True)
class RetryState:
    attempts: int
    retry_at: datetime | None


class ProjectRegistry(Protocol):
    async def snapshot(self) -> tuple[Mapping[str, Any], str]: ...

    async def ensure_board(self, board_id: str, domain: str) -> None: ...

    async def add_project(
        self,
        name: str,
        entry: Mapping[str, Any],
        *,
        expected_sha256: str,
    ) -> None: ...

    async def audit_project_onboarding(self, event: Mapping[str, Any]) -> None: ...


Resolver = Callable[
    [IntakeSourcePolicy, str],
    Awaitable[RepositoryResolution | None] | RepositoryResolution | None,
]


def _clean_string(value: Any, path: str, *, maximum: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{path} must be a bounded non-empty string")
    return value


def _bounded_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be an integer from {minimum} to {maximum}")
    return value


def parse_source_policies(document: Mapping[str, Any]) -> dict[str, IntakeSourcePolicy]:
    """Validate the operator-owned per-source onboarding configuration."""
    if not isinstance(document, Mapping) or set(document) != {"sources"}:
        raise ValueError("onboarding config must contain only sources")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, Mapping) or len(raw_sources) > 100:
        raise ValueError("onboarding config sources must be a bounded object")
    result: dict[str, IntakeSourcePolicy] = {}
    for source_id, raw in raw_sources.items():
        source = _clean_string(source_id, "source id", maximum=80)
        path = f"sources.{source}"
        required = {
            "domain",
            "projects_root",
            "auto_onboard",
        }
        optional = {
            "per_cycle_cap",
            "max_new_projects_per_cycle",
            "retry_limit",
            "retry_backoff_s",
            "repositories",
            "repository_map",
        }
        if (
            not isinstance(raw, Mapping)
            or not required <= set(raw)
            or not set(raw) <= required | optional
        ):
            raise ValueError(f"{path} has unsupported or missing keys")
        if ("repositories" in raw) == ("repository_map" in raw):
            raise ValueError(
                f"{path} must contain exactly one of repositories or repository_map"
            )
        if ("per_cycle_cap" in raw) == ("max_new_projects_per_cycle" in raw):
            raise ValueError(
                f"{path} must contain exactly one per-cycle cap"
            )
        domain = _clean_string(raw.get("domain"), f"{path}.domain")
        if domain not in {"personal", "work"}:
            raise ValueError(f"{path}.domain must be personal or work")
        projects_root_raw = _clean_string(
            raw.get("projects_root"), f"{path}.projects_root", maximum=2_048
        )
        projects_root = Path(projects_root_raw)
        if not projects_root.is_absolute():
            raise ValueError(f"{path}.projects_root must be absolute")
        if type(raw.get("auto_onboard")) is not bool:
            raise ValueError(f"{path}.auto_onboard must be boolean")
        repositories_raw = raw.get("repositories", raw.get("repository_map"))
        if not isinstance(repositories_raw, Mapping) or len(repositories_raw) > MAX_SOURCE_PROJECTS:
            raise ValueError(f"{path}.repositories must be a bounded object")
        repositories: dict[str, RepositoryResolution] = {}
        lifecycle = _load_project_lifecycle()
        for hint, resolution in repositories_raw.items():
            project_hint = _clean_string(hint, f"{path}.repositories project", maximum=120)
            if PROJECT_HINT_RE.fullmatch(project_hint) is None:
                raise ValueError(f"{path}.repositories project is unsafe")
            if isinstance(resolution, str):
                repository_url: Any = resolution
                integration_ref: Any = "main"
            elif isinstance(resolution, Mapping) and set(resolution) <= {
                "repository_url",
                "integration_ref",
            } and "repository_url" in resolution:
                repository_url = resolution.get("repository_url")
                integration_ref = resolution.get("integration_ref", "main")
            else:
                raise ValueError(f"{path}.repositories.{project_hint} is invalid")
            inspected = lifecycle.inspect_project_source(
                work_dir=str(projects_root / safe_project_name(project_hint)),
                git_mode="clone",
                repository_url=repository_url,
                integration_ref=integration_ref,
            )
            repositories[project_hint] = RepositoryResolution(
                repository_url=str(inspected["repository_url"]),
                integration_ref=str(inspected["integration_ref"]),
            )
        result[source] = IntakeSourcePolicy(
            source_id=source,
            domain=domain,
            projects_root=projects_root,
            auto_onboard=raw["auto_onboard"],
            per_cycle_cap=_bounded_int(
                raw.get("per_cycle_cap", raw.get("max_new_projects_per_cycle")),
                f"{path}.per_cycle_cap",
                1,
                100,
            ),
            retry_limit=_bounded_int(raw.get("retry_limit", 3), f"{path}.retry_limit", 1, 20),
            retry_backoff_s=_bounded_int(
                raw.get("retry_backoff_s", 60),
                f"{path}.retry_backoff_s",
                1,
                86_400,
            ),
            repositories=repositories,
        )
    return result


def safe_project_name(project_hint: str) -> str:
    if PROJECT_HINT_RE.fullmatch(project_hint) is None:
        raise ValueError("project_hint is unsafe")
    value = SAFE_NAME_RE.sub("-", project_hint.strip()).strip(".-_").lower()
    if not value:
        value = "project"
    suffix = hashlib.sha256(project_hint.encode("utf-8")).hexdigest()[:10]
    return f"{value[:60]}-{suffix}"


def explicit_map_resolver(
    policy: IntakeSourcePolicy, project_hint: str
) -> RepositoryResolution | None:
    return policy.repositories.get(project_hint)


class ProjectOnboarder:
    """Apply bounded onboarding attempts while leaving failed intake pending."""

    def __init__(
        self,
        registry: ProjectRegistry,
        policies: Mapping[str, IntakeSourcePolicy],
        *,
        resolver: Resolver = explicit_map_resolver,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        operation_lock: asyncio.Lock | None = None,
        retry_state: Mapping[tuple[str, str], RetryState] | None = None,
    ) -> None:
        self.registry = registry
        self.policies = dict(policies)
        self.resolver = resolver
        self.clock = clock
        self.operation_lock = operation_lock or asyncio.Lock()
        self.retry_state = dict(retry_state or {})

    async def _audit(self, result: OnboardingResult, now: datetime) -> None:
        writer = getattr(self.registry, "audit_project_onboarding", None)
        if not callable(writer):
            return
        event = {
            "schema_version": 1,
            "kind": "project_auto_onboarding",
            "item_id": result.item_id,
            "source_id": result.source_id,
            "project_hint": result.project_hint,
            "status": result.status,
            "board_id": result.board_id,
            "at": now.isoformat(),
        }
        retry = self.retry_state.get((result.source_id, result.project_hint))
        if retry is not None:
            event["retry_attempts"] = retry.attempts
            event["retry_at"] = (
                retry.retry_at.isoformat() if retry.retry_at is not None else None
            )
        await writer(event)

    async def _finish(self, result: OnboardingResult, now: datetime) -> OnboardingResult:
        await self._audit(result, now)
        return result

    async def run_cycle(
        self, items: Sequence[PendingProjectItem]
    ) -> list[OnboardingResult]:
        counts: dict[str, int] = {}
        results: list[OnboardingResult] = []
        for item in items:
            now = self.clock().astimezone(timezone.utc)
            results.append(await self._process(item, counts, now))
        return results

    async def _process(
        self,
        item: PendingProjectItem,
        counts: dict[str, int],
        now: datetime,
    ) -> OnboardingResult:
        if not item.item_id or PROJECT_HINT_RE.fullmatch(item.project_hint) is None:
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    "[invalid project hint]",
                    "invalid",
                    finding="intake project hint is invalid",
                ),
                now,
            )
        policy = self.policies.get(item.source_id)
        if policy is None:
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    item.project_hint,
                    "unconfigured_source",
                    finding=f"project {item.project_hint} has no onboarding source configuration"[:MAX_FINDING_CHARS],
                ),
                now,
            )
        registry, _digest = await self.registry.snapshot()
        projects = registry.get("projects") if isinstance(registry, Mapping) else None
        existing = projects.get(item.project_hint) if isinstance(projects, Mapping) else None
        if isinstance(existing, Mapping):
            board_id = existing.get("board_id")
            if existing.get("status", "active") == "active" and isinstance(board_id, str):
                self.retry_state.pop((item.source_id, item.project_hint), None)
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        "already_registered",
                        board_id=board_id,
                    ),
                    now,
                )
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    item.project_hint,
                    "registry_conflict",
                    finding=f"project {item.project_hint} is registered but not active"[:MAX_FINDING_CHARS],
                ),
                now,
            )
        if not policy.auto_onboard:
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    item.project_hint,
                    "disabled",
                    finding=f"project {item.project_hint} requires onboarding; auto_onboard is disabled"[:MAX_FINDING_CHARS],
                ),
                now,
            )
        if counts.get(item.source_id, 0) >= policy.per_cycle_cap:
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    item.project_hint,
                    "cycle_cap",
                    finding=f"project {item.project_hint} onboarding deferred by the per-cycle cap"[:MAX_FINDING_CHARS],
                ),
                now,
            )
        retry_key = (item.source_id, item.project_hint)
        retry = self.retry_state.get(retry_key)
        if retry is not None:
            if retry.attempts >= policy.retry_limit:
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        "retry_exhausted",
                        finding=f"no access to [configured repository] for project {item.project_hint}; retry limit reached"[:MAX_FINDING_CHARS],
                    ),
                    now,
                )
            if retry.retry_at is not None and now < retry.retry_at:
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        "backoff",
                        finding=f"project {item.project_hint} onboarding is waiting for retry backoff"[:MAX_FINDING_CHARS],
                    ),
                    now,
                )
        selected = self.resolver(policy, item.project_hint)
        resolution = await selected if isinstance(selected, Awaitable) else selected
        if resolution is None:
            return await self._finish(
                OnboardingResult(
                    item.item_id,
                    item.source_id,
                    item.project_hint,
                    "unresolved",
                    finding=f"project {item.project_hint} has no resolvable repository"[:MAX_FINDING_CHARS],
                ),
                now,
            )
        counts[item.source_id] = counts.get(item.source_id, 0) + 1
        board_id = safe_project_name(item.project_hint)
        work_dir = policy.projects_root / board_id
        lifecycle = _load_project_lifecycle()
        async with self.operation_lock:
            registry, digest = await self.registry.snapshot()
            projects = registry.get("projects") if isinstance(registry, Mapping) else None
            raced = projects.get(item.project_hint) if isinstance(projects, Mapping) else None
            if isinstance(raced, Mapping):
                raced_board = raced.get("board_id")
                status = "already_registered" if raced.get("status", "active") == "active" else "registry_conflict"
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        status,
                        board_id=raced_board if isinstance(raced_board, str) else None,
                        finding=None if status == "already_registered" else f"project {item.project_hint} registry settings conflict",
                    ),
                    now,
                )
            git_mode = "existing" if work_dir.exists() else "clone"
            try:
                source = await asyncio.to_thread(
                    lifecycle.inspect_project_source,
                    work_dir=str(work_dir),
                    git_mode=git_mode,
                    repository_url=resolution.repository_url,
                    integration_ref=resolution.integration_ref,
                )
                plan = lifecycle.build_add_plan(
                    request={
                        "name": item.project_hint,
                        "board_id": board_id,
                        "prepare_fleet_clone": False,
                    },
                    registry=registry,
                    registry_expected_sha256=digest,
                    source=source,
                    board_exists=False,
                    actor="board-butler",
                    central="registry",
                    created_at=now,
                )
                if plan.get("blocked"):
                    return await self._finish(
                        OnboardingResult(
                            item.item_id,
                            item.source_id,
                            item.project_hint,
                            "source_conflict",
                            finding=f"project {item.project_hint} onboarding conflicts with an existing folder or registry entry"[:MAX_FINDING_CHARS],
                        ),
                        now,
                    )
                if git_mode == "clone":
                    await asyncio.to_thread(lifecycle.clone_project_source, plan)
                await self.registry.ensure_board(board_id, policy.domain)
                entry = dict(plan["proposed_entry"])
                entry.update(
                    {
                        "status": "active",
                        "domain": policy.domain,
                        "integration_ref": resolution.integration_ref,
                    }
                )
                await self.registry.add_project(
                    item.project_hint, entry, expected_sha256=digest
                )
            except lifecycle.ProjectLifecycleCloneError as exc:
                attempts = (retry.attempts if retry is not None else 0) + 1
                self.retry_state[retry_key] = RetryState(
                    attempts=attempts,
                    retry_at=now + timedelta(
                        seconds=min(
                            policy.retry_backoff_s * (2 ** (attempts - 1)),
                            MAX_RETRY_BACKOFF_S,
                        )
                    ),
                )
                access = exc.reason_code == "repository_access_denied"
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        "access_denied" if access else "clone_failed",
                        finding=(
                            f"no access to [configured repository] for project {item.project_hint}"
                            if access
                            else f"project {item.project_hint} repository clone failed"
                        )[:MAX_FINDING_CHARS],
                    ),
                    now,
                )
            except Exception:
                return await self._finish(
                    OnboardingResult(
                        item.item_id,
                        item.source_id,
                        item.project_hint,
                        "onboarding_failed",
                        finding=f"project {item.project_hint} onboarding could not complete"[:MAX_FINDING_CHARS],
                    ),
                    now,
                )
        self.retry_state.pop(retry_key, None)
        return await self._finish(
            OnboardingResult(
                item.item_id,
                item.source_id,
                item.project_hint,
                "onboarded",
                board_id=board_id,
            ),
            now,
        )
