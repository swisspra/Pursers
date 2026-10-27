from __future__ import annotations

import asyncio
import runpy
from pathlib import Path

import pytest

API = runpy.run_path(str(Path(__file__).parents[1] / "stranded_approvals.py"))


def ticket(i, **extra):
    return {"ticket_id": f"TK-{i:03d}", "status": "closed", **extra}


def page(rows, total, cursor=None, **extra):
    return {"tickets": rows, "count": len(rows), "total_matching": total,
            "next_cursor": cursor, "has_more": cursor is not None, **extra}


class Client:
    def __init__(self, rows, full_pages=None, detail=None, summary_pages=None):
        self.rows = rows
        self.full_pages = full_pages
        self.summary_pages = summary_pages
        self.detail = detail or {}
        self.calls = []
        self.details = []

    async def ticket_list(self, **kw):
        self.calls.append(kw)
        if kw.get("view") == "full":
            if self.full_pages is not None:
                return self.full_pages[kw.get("cursor")]
            selected = [r for r in self.rows if r["ticket_id"] in kw["ticket_ids"]]
            return page(selected, len(selected))
        if kw.get("status") == "closed" and self.summary_pages is not None:
            return self.summary_pages[kw.get("cursor")]
        selected = [r for r in self.rows if kw.get("status") in (None, r["status"])]
        return page(selected[:kw["limit"]], len(selected))

    async def board_status(self, **kw):
        raise AssertionError("cursor traversal must not split by assignee")

    async def ticket_get(self, ticket_id, **kw):
        self.details.append((ticket_id, kw))
        return {"ticket": self.detail[ticket_id]}


def fetch(client):
    return asyncio.run(API["fetch_all_tickets"](client))


def test_cursor_recovers_byte_limited_full_batch():
    rows = [ticket(i, review_verdict="approve", submission_history=[]) for i in range(25)]
    client = Client(rows, {None: page(rows[:22], 25, "next"), "next": page(rows[22:], 25)})
    assert fetch(client) == rows
    full = [c for c in client.calls if c.get("view") == "full"]
    assert len(full) == 2
    assert {k: v for k, v in full[1].items() if k != "cursor"} == full[0]


def test_summary_cursor_not_assignee_split():
    rows = [ticket(i, review_verdict="approve", submission_history=[]) for i in range(4)]
    client = Client(rows, summary_pages={None: page(rows[:2], 4, "next"), "next": page(rows[2:], 4)})
    assert fetch(client) == rows


@pytest.mark.parametrize("marker", [
    {"oversized_for_list": True},
    {"omitted_sections": ["submission_history", "review_history"]},
    {"omitted_sections": ["description"]},
])
def test_count_complete_stub_is_hydrated(marker):
    full = ticket(0, review_verdict="approve", submission_history=[{"notes": "legacy"}])
    stub = ticket(0, **marker)
    client = Client([full], {None: page([stub], 1)}, {"TK-000": full})
    assert fetch(client) == [full]
    assert client.details == [("TK-000", {"view": "full"})]


@pytest.mark.parametrize("detail", [
    ticket(1),
    ticket(0, oversized_for_list=True),
    ticket(0, omitted_sections=["submission_history"]),
    ticket(0, review_verdict="approve"),
    ticket(0, submission_history="invalid"),
    ticket(0, review_verdict="approve", submission_history=None),
    ticket(0, omitted_sections="submission_history"),
    ticket(0, review_history=[{"verdict": "approve"}]),
])
def test_hydration_bad_identity_or_incomplete_inputs_fail_closed(detail):
    stub = ticket(0, oversized_for_list=True)
    with pytest.raises(API["AuditError"]):
        fetch(Client([stub], {None: page([stub], 1)}, {"TK-000": detail}))


def test_unreviewed_detail_does_not_require_verdict():
    full = ticket(0, status="open")
    client = Client([full], {None: page([ticket(0, status="open", oversized_for_list=True)], 1)}, {"TK-000": full})
    assert fetch(client) == [full]
    assert len(client.details) == 1


@pytest.mark.parametrize("pages", [
    {None: page([ticket(0)], 2, "next"), "next": page([ticket(1)], 3)},
    {None: page([ticket(0)], 2, "next"), "next": page([ticket(0)], 2)},
    {None: page([ticket(0)], 2, "next"), "next": page([ticket(1)], 2, "next")},
    {None: page([ticket(0)], 2, "")},
    {None: page([ticket(0)], 2, 12)},
    {None: page([ticket(0)], 2)},
    {None: page([ticket(0), ticket(0)], 2)},
    {None: page([ticket(0), ticket(1)], 2, "extra")},
    {None: page([], 2, "next")},
    {None: {"tickets": [ticket(0), ticket(1)], "count": True, "total_matching": 2}},
])
def test_invalid_traversals_fail_closed(pages):
    with pytest.raises(API["AuditError"]):
        fetch(Client([ticket(0), ticket(1)], pages))


def test_page_bound_exhaustion_fail_closed(monkeypatch):
    monkeypatch.setitem(API["fetch_all_tickets"].__globals__, "TICKET_MAX_PAGES", 1)
    with pytest.raises(API["AuditError"], match="page limit"):
        fetch(Client([ticket(0), ticket(1)], {None: page([ticket(0)], 2, "next")}))


def test_hydration_failure_is_audit_error():
    stub = ticket(0, oversized_for_list=True)
    with pytest.raises(API["AuditError"], match="hydration failed"):
        fetch(Client([stub], {None: page([stub], 1)}, {}))


def test_full_input_status_change_fail_closed():
    with pytest.raises(API["AuditError"], match="status changed"):
        fetch(Client([ticket(0)], {None: page([ticket(0, status="open")], 1)}))


def test_legacy_incomplete_full_page_fail_closed():
    incomplete = {"tickets": [ticket(0)], "count": 1, "total_matching": 2}
    with pytest.raises(API["AuditError"]):
        fetch(Client([ticket(0), ticket(1)], {None: incomplete}))


@pytest.mark.parametrize("field", ["submission_history_omitted_count", "review_history_omitted_count"])
@pytest.mark.parametrize("value", [1, -1, True, "1", None, 1.5])
def test_retained_or_malformed_history_counter_fails_closed(field, value):
    row = ticket(0, review_verdict="approve", submission_history=[{"notes": "latest"}], **{field: value})
    with pytest.raises(API["AuditError"]):
        fetch(Client([row]))


def test_actual_central_retained_history_counters_fail_closed():
    from pursers_central.central import CentralBoard
    row = ticket(0, review_verdict="approve", review_history=[{"verdict": "reject"}, {"verdict": "approve"}],
                 submission_history=[{"notes": "old"}, {"notes": "latest"}])
    assert set(CentralBoard.bound_ticket_history(row, 1)) == {"review_history", "submission_history"}
    assert row["review_history_omitted_count"] == row["submission_history_omitted_count"] == 1
    with pytest.raises(API["AuditError"]):
        fetch(Client([row]))


def test_zero_history_counters_and_new_unreviewed_are_allowed():
    row = ticket(0, status="open", submission_history_omitted_count=0, review_history_omitted_count=0)
    assert fetch(Client([row])) == [row]


def test_ambiguous_closed_row_is_hydrated_once():
    row = {"ticket_id": "TK-000", "status": "closed"}
    detail = ticket(0, review_verdict="approve", submission_history=[{"notes": "latest"}])
    client = Client([row], detail={"TK-000": detail})
    assert fetch(client) == [detail]
    assert client.details == [("TK-000", {"view": "full"})]


def test_transport_hydration_preserves_only_safe_error_metadata():
    class RemoteProtocolError(Exception):
        pass
    class Broken(Client):
        async def ticket_get(self, ticket_id, **kw):
            raise RemoteProtocolError("Bearer fixture-secret https://user:password@example.invalid")
    with pytest.raises(API["AuditError"]) as caught:
        fetch(Broken([ticket(0, status="open", oversized_for_list=True)]))
    assert caught.value.original_error_class == "RemoteProtocolError"
    assert "fixture-secret" not in str(caught.value)
    assert "password" not in str(caught.value)


def test_hydration_registry_error_preserves_safe_code():
    from pursers_client.project_registry import RegistryRoutingError
    class Broken(Client):
        async def ticket_get(self, ticket_id, **kw):
            raise RegistryRoutingError("project_route_not_registered", "Bearer fixture-secret")
    with pytest.raises(API["AuditError"]) as caught:
        fetch(Broken([ticket(0, status="open", oversized_for_list=True)]))
    assert caught.value.original_error_class == "RegistryRoutingError"
    assert caught.value.original_error_code == "project_route_not_registered"
    assert "fixture-secret" not in str(caught.value)


@pytest.mark.parametrize("case, expected", [
    ("wrapped_route", ("RegistryRoutingError", "project_route_not_registered")),
    ("wrapped_transport", ("RemoteProtocolError", None)),
    ("invalid_class", ("ApprovalError", None)),
    ("nonstring_class", ("ApprovalError", None)),
    ("invalid_code", ("RegistryRoutingError", None)),
    ("nonstring_code", ("RegistryRoutingError", None)),
    ("transport_with_code", ("RemoteProtocolError", None)),
    ("ordinary_route", ("RegistryRoutingError", "project_route_not_registered")),
    ("ordinary_transport", ("RemoteProtocolError", None)),
    ("empty_originals", ("RegistryRoutingError", "project_route_not_registered")),
])
def test_v3_hydration_preserves_validated_original_metadata(case, expected):
    from pursers_client.project_registry import RegistryRoutingError
    class RemoteProtocolError(Exception): pass
    if case in {"ordinary_route", "empty_originals"}:
        error = RegistryRoutingError("project_route_not_registered", "Bearer fixture-secret")
        if case == "empty_originals":
            error.original_error_class = ""
            error.original_error_code = ""
    elif case == "ordinary_transport":
        error = RemoteProtocolError("Bearer fixture-secret")
    else:
        name, code = {
            "wrapped_route": ("RegistryRoutingError", "project_route_not_registered"),
            "wrapped_transport": ("RemoteProtocolError", None),
            "invalid_class": ("Bearer fixture-secret", "project_route_not_registered"),
            "nonstring_class": ({"private": "fixture-secret"}, "project_route_not_registered"),
            "invalid_code": ("RegistryRoutingError", "Bearer fixture-secret"),
            "nonstring_code": ("RegistryRoutingError", ["fixture-secret"]),
            "transport_with_code": ("RemoteProtocolError", "project_route_not_registered"),
        }[case]
        error = API["AuditError"]("Bearer fixture-secret", original_error_class=name, original_error_code=code)
    class Broken(Client):
        async def ticket_get(self, ticket_id, **kw): raise error
    with pytest.raises(API["AuditError"]) as caught:
        fetch(Broken([ticket(0, status="open", oversized_for_list=True)]))
    assert (caught.value.original_error_class, caught.value.original_error_code) == expected
    assert str(caught.value) == "full-input hydration failed"
    assert caught.value.__suppress_context__ is True


def test_real_central_unavailable_archive_body_fails_closed(tmp_path, monkeypatch):
    import pursers_central.central as central
    jwks = tmp_path / "fixture-jwks.json"
    jwks.write_text('{"keys":[]}')
    for key, value in {"CENTRAL_AUTH_MODE": "jwt", "CENTRAL_JWT_ISSUER": "https://issuer.example.invalid",
                       "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp", "CENTRAL_JWKS_PATH": str(jwks),
                       "CENTRAL_ADMISSION": "invite", "STORE_BACKEND": "sqlite"}.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server("localhost", 8765, tmp_path / "synthetic-central")
    principal = central.Principal("PR-archive", "archive", frozenset({"board:read", "board:write"}))
    monkeypatch.setattr(central, "current_principal", lambda: principal)
    archived = central.CentralBoard.archive_index_row("shared", ticket(0, review_verdict="approve"))
    assert not {"review_verdict", "submission_history", "omitted_sections"} & archived.keys()
    monkeypatch.setattr(service, "load_archive_index", lambda board: {"TK-000": archived})
    monkeypatch.setattr(service, "merged_archived_ticket", lambda board, ticket: None)
    details = []
    class RealTools:
        async def ticket_list(self, **kw):
            return (await mcp.call_tool("ticket_list", {"board_id": "shared", **kw})).structured_content
        async def ticket_get(self, ticket_id, **kw):
            details.append((ticket_id, kw))
            return (await mcp.call_tool("ticket_get", {"board_id": "shared", "ticket_id": ticket_id, **kw})).structured_content
    async def scan():
        await mcp.call_tool("board_join", {"board_id": "shared", "agent_name": "fixture-reader", "role": "worker",
                            "capabilities": {"can_work": False, "can_review": False, "tier_max": 2, "max_parallel": 1}})
        return await API["fetch_all_tickets"](RealTools())
    with pytest.raises(API["AuditError"]):
        asyncio.run(scan())
    assert details == [("TK-000", {"view": "full"})]
