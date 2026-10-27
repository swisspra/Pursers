from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_board_butler import NOW, args, butler


def approved(ticket_id, target):
    return {"ticket_id": ticket_id, "status": "closed", "review_verdict": "approve",
            "target_url": target, "submission_history": [{"notes": "branch_and_commit: worker/fixture @ " + "a" * 40}],
            "updated_at": (NOW - butler.timedelta(days=2)).isoformat()}


def registry(root):
    return {"schema_version": 1, "projects": {
        name: {"board_id": "shared", "status": "active", "work_dir": str(root / name),
               "fleet_clone_dir": str(root / (name + "-clone")), "fleet": True,
               "repository_url": "https://example.invalid/" + name}
        for name in ("alpha", "beta")}}


@pytest.mark.parametrize("target, expected", [("alpha/", "alpha-clone"), ("https://example.invalid/beta", "beta-clone")])
def test_ticket_binding_uses_registered_project_clone(tmp_path, monkeypatch, target, expected):
    reg = registry(tmp_path)
    calls = []
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: (calls.append(repo) or ("main", "b" * 40)))
    binding = butler._approval_repository_binding(reg, "shared", approved("TK-a", target))
    assert binding.repo == tmp_path / expected
    assert binding.main_sha == "b" * 40
    assert binding.error_class is None
    assert calls == [tmp_path / expected]


@pytest.mark.parametrize("case", ["ambiguous", "unknown", "wrong_board", "no_target", "unavailable"])
def test_binding_fails_unverifiable_not_shared_repo(tmp_path, monkeypatch, case):
    reg = registry(tmp_path)
    row = approved("TK-a", "alpha/")
    if case == "ambiguous":
        reg["projects"]["duplicate"] = dict(reg["projects"]["alpha"])
        row["target_url"] = "https://example.invalid/alpha"
    elif case == "unknown":
        row["target_url"] = "unregistered/"
    elif case == "wrong_board":
        reg["projects"]["alpha"]["board_id"] = "other"
    elif case == "no_target":
        row.pop("target_url")
        row["project"] = "shared"
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: None if case == "unavailable" else ("main", "b" * 40))
    binding = butler._approval_repository_binding(reg, "shared", row)
    assert binding.error_class
    assert binding.main_sha is None


def test_explicit_project_name_can_bind_without_target(tmp_path, monkeypatch):
    row = approved("TK-a", "alpha/")
    row.pop("target_url")
    row["project"] = "alpha"
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: ("main", "b" * 40))
    assert butler._approval_repository_binding(registry(tmp_path), "shared", row).repo == tmp_path / "alpha-clone"


def test_repo_identity_invalidates_classification_cache(tmp_path):
    calls = []
    sha = "b" * 40
    def classify(row, **kw):
        calls.append(kw)
        return SimpleNamespace(state="LANDED_ANCESTOR")
    first_binding = butler.ApprovalRepositoryBinding(tmp_path / "alpha", sha, "alpha-repo")
    context = butler.ObservationContext("shared", {"TK-a": approved("TK-a", "alpha/")}, (), NOW,
                                        approval_repositories={"TK-a": first_binding})
    cache = butler.ApprovalClassificationCache()
    first = cache.scan(context, main_sha=sha, budget=1, classify=classify)
    hit = cache.scan(context, main_sha=sha, budget=1, classify=classify)
    moved = cache.scan(replace(context, approval_repositories={"TK-a": butler.ApprovalRepositoryBinding(tmp_path / "beta", sha, "beta-repo")}),
                       main_sha=sha, budget=1, classify=classify)
    assert (first.classified, hit.cache_hits, moved.classified, moved.cache_hits) == (1, 1, 1, 0)
    assert [c["repo"] for c in calls] == [tmp_path / "alpha", tmp_path / "beta"]
    assert all(c["main_ref"] == sha for c in calls)


def test_unavailable_binding_reports_unverifiable_without_classifier(tmp_path):
    binding = butler.ApprovalRepositoryBinding(None, None, "unavailable", "ApprovalRepositoryUnavailable")
    context = butler.ObservationContext("shared", {"TK-a": approved("TK-a", "alpha/")}, (), NOW,
                                        approval_repositories={"TK-a": binding})
    def classify(*a, **kw):
        pytest.fail("must not classify against shared fallback")
    result = butler.ApprovalClassificationCache().scan(context, main_sha="-", budget=1, classify=classify)
    assert result.complete
    assert any("UNVERIFIABLE" in r["evidence"] for r in result.findings)


def test_direct_observer_uses_ticket_binding(tmp_path, monkeypatch):
    row = approved("TK-a", "alpha/")
    context = butler.ObservationContext("shared", {"TK-a": row}, (), NOW,
        repo=tmp_path / "wrong-shared", main_ref="d" * 40,
        approval_repositories={"TK-a": butler.ApprovalRepositoryBinding(tmp_path / "alpha", "b" * 40, "alpha")})
    calls = []
    def classify(row, **kw):
        calls.append(kw)
        return SimpleNamespace(state="LANDED_ANCESTOR")
    monkeypatch.setitem(butler._stranded_approvals_api(), "classify_approval", classify)
    assert butler._observe_stranded_approvals(context) == []
    assert calls == [{"repo": tmp_path / "alpha", "main_ref": "b" * 40}]


def test_background_cycle_binds_two_projects_on_same_board(tmp_path, monkeypatch):
    backend = butler.CentralBackend(args(tmp_path), "opaque-test")
    reg = registry(tmp_path)
    rows = [approved("TK-a", "alpha/"), approved("TK-b", "beta/")]
    class Client:
        async def board_state_get(self, key):
            assert key == "project_registry"
            return {"state": {"value": butler.json.dumps(reg)}}
    backend.client = Client()
    @contextlib.asynccontextmanager
    async def client_for_board(board_id):
        assert board_id == "shared"
        yield backend.client
    backend._client_for_board = client_for_board
    async def fetch(client):
        return rows
    monkeypatch.setitem(butler._stranded_approvals_api(), "fetch_all_tickets", fetch)
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: ("main", ("b" if repo.name == "alpha-clone" else "c") * 40))
    calls = []
    def classify(row, **kw):
        calls.append((row["ticket_id"], kw["repo"], kw["main_ref"]))
        return SimpleNamespace(state="LANDED_ANCESTOR")
    monkeypatch.setitem(butler._stranded_approvals_api(), "classify_approval", classify)
    result = asyncio.run(backend._run_approval_scan_cycle(["shared"], NOW))
    assert result["shared"].complete
    assert calls == [("TK-a", tmp_path / "alpha-clone", "b" * 40), ("TK-b", tmp_path / "beta-clone", "c" * 40)]


@pytest.mark.parametrize("target", ["legacy/", "https://example.invalid/beta"])
def test_stored_registered_project_conflict_fails_closed(tmp_path, monkeypatch, target):
    reg = registry(tmp_path)
    reg["projects"]["beta"]["work_dir"] = str(tmp_path / "legacy")
    row = approved("TK-a", target)
    row["project"] = "alpha"
    calls = []
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: (calls.append(repo) or ("main", "b" * 40)))
    binding = butler._approval_repository_binding(reg, "shared", row)
    assert binding.error_class is not None
    assert binding.repo is None
    assert calls == []


def test_matching_stored_registered_project_allows_route(tmp_path, monkeypatch):
    row = approved("TK-a", "https://example.invalid/alpha")
    row["project"] = "alpha"
    monkeypatch.setattr(butler, "_local_main_revision", lambda repo: ("main", "b" * 40))
    assert butler._approval_repository_binding(registry(tmp_path), "shared", row).error_class is None


def isolated_git_objects(repo):
    import hashlib
    import zlib
    gitdir = repo / ".git"
    (gitdir / "objects").mkdir(parents=True)
    (gitdir / "refs/heads").mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    def store(kind, data):
        content = (kind + " " + str(len(data)) + "\0").encode() + data
        sha = hashlib.sha1(content).hexdigest()
        path = gitdir / "objects" / sha[:2] / sha[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(content))
        return sha, path
    tree, _ = store("tree", b"")
    identity = "author Test <test@example.invalid> 1 +0000\ncommitter Test <test@example.invalid> 1 +0000\n\n"
    approved_sha, path = store("commit", ("tree " + tree + "\n" + identity + "approved\n").encode())
    main_sha, _ = store("commit", ("tree " + tree + "\nparent " + approved_sha + "\n" + identity + "main\n").encode())
    (gitdir / "refs/heads/main").write_text(main_sha + "\n")
    return approved_sha, main_sha, path


def test_real_same_path_git_object_loss_and_reappearance_revalidated(tmp_path, monkeypatch):
    repo = tmp_path / "alpha"
    approved_sha, main_sha, path = isolated_git_objects(repo)
    row = approved("TK-a", "alpha/")
    row["submission_history"] = [{"notes": "branch_and_commit: worker/fixture @ " + approved_sha}]
    reg = {"schema_version": 1, "projects": {"alpha": {"board_id": "shared", "status": "active", "work_dir": str(repo)}}}
    binding = butler._approval_repository_binding(reg, "shared", row)
    context = butler.ObservationContext("shared", {"TK-a": row}, (), NOW, approval_repositories={"TK-a": binding})
    clock = [0.0]
    monkeypatch.setattr(butler.time, "monotonic", lambda: clock[0])
    cache = butler.ApprovalClassificationCache()
    assert cache.scan(context, main_sha=main_sha, budget=1).findings == ()
    saved = path.read_bytes()
    path.unlink()
    clock[0] += 301
    missing = cache.scan(context, main_sha=main_sha, budget=1)
    assert missing.classified == 1 and missing.cache_hits == 0
    assert any("BRANCH_MISSING" in r["evidence"] for r in missing.findings)
    path.write_bytes(saved)
    clock[0] += 31
    restored = cache.scan(context, main_sha=main_sha, budget=1)
    assert restored.classified == 1 and restored.findings == ()


def test_expired_cache_budget_preserves_queue_fairness(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(butler.time, "monotonic", lambda: clock[0])
    tickets = {"TK-" + str(i): approved("TK-" + str(i), "alpha/") for i in range(3)}
    bindings = {tid: butler.ApprovalRepositoryBinding(tmp_path, "b" * 40, "same") for tid in tickets}
    context = butler.ObservationContext("shared", tickets, (), NOW, approval_repositories=bindings)
    calls = []
    def classify(row, **kw):
        calls.append(row["ticket_id"])
        return SimpleNamespace(state="LANDED_ANCESTOR")
    cache = butler.ApprovalClassificationCache()
    assert cache.scan(context, main_sha="b" * 40, budget=3, classify=classify).complete
    clock[0] = 301
    step = cache.scan(context, main_sha="b" * 40, budget=1, classify=classify)
    assert not step.complete and step.pending == 2 and step.classified == 1
    cache.scan(context, main_sha="b" * 40, budget=1, classify=classify)
    assert cache.scan(context, main_sha="b" * 40, budget=1, classify=classify).complete
    assert calls == list(tickets) * 2


def test_binding_preserves_safe_routing_metadata_through_consumers(tmp_path):
    row = approved("TK-a", "unknown/")
    binding = butler._approval_repository_binding(registry(tmp_path), "shared", row)
    assert binding.error_class == "RegistryRoutingError"
    assert binding.error_code == "project_route_not_registered"
    context = butler.ObservationContext("shared", {"TK-a": row}, (), NOW, approval_repositories={"TK-a": binding})
    for findings in (butler.ApprovalClassificationCache().scan(context, main_sha="-", budget=1).findings,
                     butler._observe_stranded_approvals(context)):
        assert findings[0]["error_class"] == "RegistryRoutingError"
        assert findings[0]["routing_code"] == "project_route_not_registered"
        assert "code=project_route_not_registered" in findings[0]["evidence"]
        decorated = butler._decorate_observation_candidate(context, butler.APPROVED_NOT_LANDED_OBSERVATION_RULE, findings[0])
        assert decorated["error_class"] == "RegistryRoutingError"
        assert decorated["routing_code"] == "project_route_not_registered"


def test_background_harvest_preserves_transport_class_without_text(tmp_path, monkeypatch):
    class RemoteProtocolError(Exception): pass
    class Client:
        async def board_state_get(self, key):
            return {"state": {"value": butler.json.dumps(registry(tmp_path))}}
        async def ticket_list(self, **kw):
            rows = [{"ticket_id": "TK-a", "status": "closed", "oversized_for_list": True}] if kw.get("status") in (None, "closed") else []
            return {"tickets": rows, "count": len(rows), "total_matching": len(rows), "next_cursor": None, "has_more": False}
        async def ticket_get(self, ticket_id, **kw):
            raise RemoteProtocolError("Bearer fixture-secret")
    backend = butler.CentralBackend(args(tmp_path), "opaque-test")
    backend.client = Client()
    @contextlib.asynccontextmanager
    async def selected(board_id): yield backend.client
    backend._client_for_board = selected
    async def scan():
        task = asyncio.create_task(backend._run_approval_scan_cycle(["shared"], NOW))
        backend._approval_scan_task = task
        with pytest.raises(Exception): await task
        backend._harvest_approval_scan(["shared"])
    asyncio.run(scan())
    assert backend._approval_scan_failure == "RemoteProtocolError"
    finding = backend._approval_findings_for_board("shared", NOW)[0]
    assert finding["error_class"] == "RemoteProtocolError"
    assert "fixture-secret" not in str(finding)


def test_direct_registry_error_keeps_safe_class(tmp_path, monkeypatch):
    class RegistryReadFailure(Exception): pass
    backend = butler.CentralBackend(args(tmp_path), "opaque-test")
    class Client:
        async def board_state_get(self, key): raise RegistryReadFailure("Bearer fixture-secret")
    backend.client = Client()
    row = approved("TK-a", "alpha/")
    import pursers_client
    class FakeBoardClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def board_question_inbox(self, **kw): return {"questions": [], "total": 0}
    monkeypatch.setattr(pursers_client, "BoardClient", FakeBoardClient)
    async def read_approved(client): return [row]
    monkeypatch.setitem(butler._stranded_approvals_api(), "fetch_all_tickets", read_approved)
    monkeypatch.setattr(butler, "read_host_headroom", lambda repo: {})
    context = asyncio.run(backend._observation_context_for_board("shared", {"tickets": {}}, NOW))
    finding = butler._observe_stranded_approvals(context)[0]
    assert finding["error_class"] == "RegistryReadFailure"
    assert "fixture-secret" not in str(finding)


def test_safe_diagnostics_reject_malformed_routing_code(tmp_path):
    from pursers_client.project_registry import RegistryRoutingError
    error = RegistryRoutingError("Bearer fixture-secret", "credential narrative")
    assert butler._approval_error_metadata(error) == ("RegistryRoutingError", None)
    wrapped = butler._stranded_approvals_api()["AuditError"]("safe", original_error_class="RegistryRoutingError", original_error_code="project_route_not_registered")
    assert butler._approval_error_metadata(wrapped) == ("RegistryRoutingError", "project_route_not_registered")


@pytest.mark.parametrize("old_findings", [(), ({"level": "warn", "ticket_id": "TK-a", "message": "Prior positive", "evidence": "state=STRANDED"},)])
def test_failed_refresh_invalidates_complete_coverage_and_marks_prior_stale(tmp_path, old_findings):
    class RegistryReadFailure(Exception): pass
    class Client:
        async def board_state_get(self, key): raise RegistryReadFailure("private narrative")
    backend = butler.CentralBackend(args(tmp_path), "opaque-test")
    backend.client = Client()
    backend._approval_scan_last["shared"] = butler.ApprovalScanOutcome(old_findings, True, 0, 0, 1, 1, "b" * 40)
    async def scan():
        task = asyncio.create_task(backend._run_approval_scan_cycle(["shared"], NOW))
        backend._approval_scan_task = task
        with pytest.raises(RegistryReadFailure): await task
        backend._harvest_approval_scan(["shared"])
    asyncio.run(scan())
    assert not backend._approval_scan_last["shared"].complete
    findings = backend._approval_findings_for_board("shared", NOW)
    coverage = [f for f in findings if f.get("observer") == "approved_not_landed_coverage"]
    assert len(coverage) == 1 and "failed:RegistryReadFailure" in coverage[0]["evidence"]
    prior = [f for f in findings if f.get("ticket_id") == "TK-a"]
    assert len(prior) == len(old_findings)
    assert all(f["stale"] is True and "stale=true" in f["evidence"] for f in prior)
    # Failed state remains visible during a new nonblocking refresh.
    async def schedule():
        payload = backend._schedule_approval_scan(["shared"], NOW)
        assert payload["boards"]["shared"]["complete"] is False
        backend._approval_scan_task.cancel()
        try: await backend._approval_scan_task
        except asyncio.CancelledError: pass
    asyncio.run(schedule())


def test_legacy_no_binding_cache_passes_pinned_main_sha(tmp_path):
    row = approved("TK-a", "alpha/")
    context = butler.ObservationContext("shared", {"TK-a": row}, (), NOW, repo=tmp_path, main_ref="refs/heads/main")
    calls = []
    def classify(row, **kw):
        calls.append(kw)
        return SimpleNamespace(state="LANDED_ANCESTOR")
    result = butler.ApprovalClassificationCache().scan(context, main_sha="b" * 40, budget=1, classify=classify)
    assert result.complete
    assert calls == [{"repo": tmp_path, "main_ref": "b" * 40}]


@pytest.mark.parametrize("case, expected", [
    ("wrapped_route", ("RegistryRoutingError", "project_route_not_registered")),
    ("invalid_class", ("ApprovalError", None)),
    ("invalid_code", ("RegistryRoutingError", None)),
    ("ordinary_transport", ("RemoteProtocolError", None)),
    ("ordinary_route", ("RegistryRoutingError", "project_route_not_registered")),
])
def test_v3_wrapped_hydration_metadata_reaches_async_harvest(tmp_path, case, expected):
    from pursers_client.project_registry import RegistryRoutingError
    class RemoteProtocolError(Exception): pass
    api = butler._stranded_approvals_api()
    if case == "ordinary_transport":
        error = RemoteProtocolError("Bearer fixture-secret")
    elif case == "ordinary_route":
        error = RegistryRoutingError("project_route_not_registered", "Bearer fixture-secret")
    else:
        name = "Bearer fixture-secret" if case == "invalid_class" else "RegistryRoutingError"
        code = "Bearer fixture-secret" if case == "invalid_code" else "project_route_not_registered"
        error = api["AuditError"]("Bearer fixture-secret", original_error_class=name, original_error_code=code)
    assert butler._approval_error_metadata(error) == expected
    class Client:
        async def board_state_get(self, key):
            return {"state": {"value": butler.json.dumps(registry(tmp_path))}}
        async def ticket_list(self, **kw):
            rows = [{"ticket_id": "TK-a", "status": "closed", "oversized_for_list": True}] if kw.get("status") in (None, "closed") else []
            return {"tickets": rows, "count": len(rows), "total_matching": len(rows), "next_cursor": None, "has_more": False}
        async def ticket_get(self, ticket_id, **kw): raise error
    backend = butler.CentralBackend(args(tmp_path), "opaque-test")
    backend.client = Client()
    @contextlib.asynccontextmanager
    async def selected(board_id): yield backend.client
    backend._client_for_board = selected
    async def scan():
        task = asyncio.create_task(backend._run_approval_scan_cycle(["shared"], NOW))
        backend._approval_scan_task = task
        with pytest.raises(api["AuditError"]) as caught: await task
        backend._harvest_approval_scan(["shared"])
        return caught.value
    hydrated_error = asyncio.run(scan())
    assert (backend._approval_scan_failure, backend._approval_scan_failure_code) == expected
    finding = backend._approval_findings_for_board("shared", NOW)[0]
    assert (finding["error_class"], finding["routing_code"]) == expected
    assert "failed:" + expected[0] in finding["evidence"]
    assert "fixture-secret" not in str(finding)
    assert (hydrated_error.original_error_class, hydrated_error.original_error_code) == expected
