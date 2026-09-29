from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "project_onboarding.py"
SPEC = importlib.util.spec_from_file_location("project_onboarding", MODULE_PATH)
assert SPEC and SPEC.loader
onboarding = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = onboarding
SPEC.loader.exec_module(onboarding)


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class FakeRegistry:
    def __init__(self) -> None:
        self.document: dict[str, Any] = {"schema_version": 1, "projects": {}}
        self.boards: list[tuple[str, str]] = []
        self.audits: list[dict[str, Any]] = []

    def digest(self) -> str:
        encoded = json.dumps(self.document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def snapshot(self) -> tuple[Mapping[str, Any], str]:
        return copy.deepcopy(self.document), self.digest()

    async def ensure_board(
        self, board_id: str, domain: str, default_ticket_tier: int | None = None
    ) -> None:
        self.boards.append((board_id, domain))

    async def add_project(
        self,
        name: str,
        entry: Mapping[str, Any],
        *,
        expected_sha256: str,
    ) -> None:
        assert expected_sha256 == self.digest()
        assert name not in self.document["projects"]
        self.document["projects"][name] = copy.deepcopy(dict(entry))

    async def audit_project_onboarding(self, event: Mapping[str, Any]) -> None:
        self.audits.append(copy.deepcopy(dict(event)))


def make_remote(tmp_path: Path, name: str) -> Path:
    source = tmp_path / f"{name}-source"
    remote = tmp_path / f"{name}.git"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (source / "README.md").write_text(name, encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True)
    return remote


def policy(
    tmp_path: Path,
    repositories: Mapping[str, onboarding.RepositoryResolution],
    *,
    auto_onboard: bool = True,
    per_cycle_cap: int = 10,
    retry_limit: int = 2,
    retry_backoff_s: int = 10,
) -> onboarding.IntakeSourcePolicy:
    return onboarding.IntakeSourcePolicy(
        source_id="sonarqube",
        domain="work",
        projects_root=tmp_path / "projects",
        auto_onboard=auto_onboard,
        per_cycle_cap=per_cycle_cap,
        retry_limit=retry_limit,
        retry_backoff_s=retry_backoff_s,
        repositories=repositories,
    )


def item(name: str) -> onboarding.PendingProjectItem:
    return onboarding.PendingProjectItem(
        item_id=f"finding-{name}", source_id="sonarqube", project_hint=name
    )


def test_unknown_project_is_cloned_registered_and_subsequent_intake_routes(
    tmp_path: Path,
) -> None:
    remote = make_remote(tmp_path, "alpha")
    registry = FakeRegistry()
    selected = policy(
        tmp_path,
        {"alpha": onboarding.RepositoryResolution(str(remote), "main")},
    )
    onboarder = onboarding.ProjectOnboarder(
        registry, {"sonarqube": selected}, clock=lambda: NOW
    )

    first = asyncio.run(onboarder.run_cycle([item("alpha")]))[0]
    second = asyncio.run(onboarder.run_cycle([item("alpha")]))[0]

    assert first.status == "onboarded"
    assert second.status == "already_registered"
    assert first.board_id == second.board_id
    entry = registry.document["projects"]["alpha"]
    assert entry["board_id"] == first.board_id
    assert entry["domain"] == "work"
    assert entry["integration_ref"] == "main"
    checkout = Path(entry["work_dir"])
    assert (checkout / "README.md").read_text(encoding="utf-8") == "alpha"
    assert len(registry.boards) == 1
    assert [row["status"] for row in registry.audits] == [
        "onboarded",
        "already_registered",
    ]


def test_no_resolvable_repository_emits_bounded_finding(tmp_path: Path) -> None:
    registry = FakeRegistry()
    onboarder = onboarding.ProjectOnboarder(
        registry,
        {"sonarqube": policy(tmp_path, {})},
        clock=lambda: NOW,
    )

    result = asyncio.run(onboarder.run_cycle([item("missing")]))[0]

    assert result.status == "unresolved"
    assert result.finding == "project missing has no resolvable repository"
    assert registry.document["projects"] == {}


def test_auto_onboard_false_is_finding_only(tmp_path: Path) -> None:
    registry = FakeRegistry()
    selected = policy(
        tmp_path,
        {"alpha": onboarding.RepositoryResolution(str(tmp_path / "unused.git"))},
        auto_onboard=False,
    )
    onboarder = onboarding.ProjectOnboarder(
        registry, {"sonarqube": selected}, clock=lambda: NOW
    )

    result = asyncio.run(onboarder.run_cycle([item("alpha")]))[0]

    assert result.status == "disabled"
    assert result.finding == "project alpha requires onboarding; auto_onboard is disabled"
    assert registry.boards == []


def test_per_cycle_cap_defers_later_projects(tmp_path: Path) -> None:
    alpha = make_remote(tmp_path, "alpha")
    beta = make_remote(tmp_path, "beta")
    registry = FakeRegistry()
    selected = policy(
        tmp_path,
        {
            "alpha": onboarding.RepositoryResolution(str(alpha)),
            "beta": onboarding.RepositoryResolution(str(beta)),
        },
        per_cycle_cap=1,
    )
    onboarder = onboarding.ProjectOnboarder(
        registry, {"sonarqube": selected}, clock=lambda: NOW
    )

    results = asyncio.run(onboarder.run_cycle([item("alpha"), item("beta")]))

    assert [result.status for result in results] == ["onboarded", "cycle_cap"]
    assert set(registry.document["projects"]) == {"alpha"}


def test_access_failure_is_capped_and_other_projects_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_remote = make_remote(tmp_path, "good")
    private_remote = tmp_path / "TOKEN-CANARY-private.git"
    registry = FakeRegistry()
    selected = policy(
        tmp_path,
        {
            "private": onboarding.RepositoryResolution(str(private_remote)),
            "good": onboarding.RepositoryResolution(str(good_remote)),
        },
        retry_limit=2,
        retry_backoff_s=10,
    )
    lifecycle = onboarding._load_project_lifecycle()
    real_clone = lifecycle.clone_project_source

    def guarded_clone(plan: Mapping[str, Any]) -> None:
        source = plan["source"]
        if "TOKEN-CANARY" in str(source["repository_url"]):
            raise lifecycle.ProjectLifecycleCloneError("repository_access_denied")
        real_clone(plan)

    monkeypatch.setattr(lifecycle, "clone_project_source", guarded_clone)
    current = [NOW]
    onboarder = onboarding.ProjectOnboarder(
        registry, {"sonarqube": selected}, clock=lambda: current[0]
    )

    first = asyncio.run(onboarder.run_cycle([item("private"), item("good")]))
    during_backoff = asyncio.run(onboarder.run_cycle([item("private")]))[0]
    current[0] += timedelta(seconds=10)
    second_failure = asyncio.run(onboarder.run_cycle([item("private")]))[0]
    current[0] += timedelta(seconds=20)
    exhausted = asyncio.run(onboarder.run_cycle([item("private")]))[0]

    assert [result.status for result in first] == ["access_denied", "onboarded"]
    assert first[0].finding == "no access to [configured repository] for project private"
    assert during_backoff.status == "backoff"
    assert second_failure.status == "access_denied"
    assert exhausted.status == "retry_exhausted"
    assert set(registry.document["projects"]) == {"good"}
    first_failure_audit = next(
        row
        for row in registry.audits
        if row["project_hint"] == "private" and row["status"] == "access_denied"
    )
    assert first_failure_audit["retry_attempts"] == 1
    assert first_failure_audit["retry_at"] == (NOW + timedelta(seconds=10)).isoformat()
    public = json.dumps(
        [result.__dict__ for result in [*first, during_backoff, second_failure, exhausted]],
        sort_keys=True,
    )
    assert "TOKEN-CANARY" not in public
    assert str(tmp_path) not in public


def test_config_requires_absolute_root_and_rejects_credentials(tmp_path: Path) -> None:
    base = {
        "sources": {
            "sonarqube": {
                "domain": "work",
                "projects_root": str(tmp_path / "projects"),
                "auto_onboard": True,
                "per_cycle_cap": 2,
                "retry_limit": 3,
                "retry_backoff_s": 30,
                "repositories": {
                    "alpha": {
                        "repository_url": "https://example.invalid/alpha.git",
                        "integration_ref": "main",
                    }
                },
            }
        }
    }
    parsed = onboarding.parse_source_policies(base)
    assert parsed["sonarqube"].projects_root == tmp_path / "projects"
    assert parsed["sonarqube"].default_ticket_tier is None

    tiered = copy.deepcopy(base)
    tiered["sources"]["sonarqube"]["default_ticket_tier"] = 1
    assert onboarding.parse_source_policies(tiered)["sonarqube"].default_ticket_tier == 1
    tiered["sources"]["sonarqube"]["default_ticket_tier"] = 9
    with pytest.raises(ValueError):
        onboarding.parse_source_policies(tiered)

    aliases = copy.deepcopy(base)
    source = aliases["sources"]["sonarqube"]
    source["repository_map"] = {
        "alpha": source.pop("repositories")["alpha"]["repository_url"]
    }
    source["max_new_projects_per_cycle"] = source.pop("per_cycle_cap")
    source.pop("retry_limit")
    source.pop("retry_backoff_s")
    alias_policy = onboarding.parse_source_policies(aliases)["sonarqube"]
    assert alias_policy.repositories["alpha"].integration_ref == "main"
    assert alias_policy.retry_limit == 3
    assert alias_policy.retry_backoff_s == 60

    relative = copy.deepcopy(base)
    relative["sources"]["sonarqube"]["projects_root"] = "relative/projects"
    with pytest.raises(ValueError, match="must be absolute"):
        onboarding.parse_source_policies(relative)

    credential = copy.deepcopy(base)
    credential["sources"]["sonarqube"]["repositories"]["alpha"][
        "repository_url"
    ] = "https://user:secret@example.invalid/alpha.git"
    with pytest.raises(ValueError, match="credentials"):
        onboarding.parse_source_policies(credential)
