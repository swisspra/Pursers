"""Model defaults page summaries; exact details and retained history stay intact."""
import asyncio
import copy
import json

from mcp import Client
import test_response_bounds as bounds


def test_direct_mcp_summary_pages_recover_all_ticket_details_and_history():
    async def scenario():
        fixture = bounds.ResponseBoundsTests(methodName="runTest")
        await fixture.asyncSetUp()
        try:
            original_id = await fixture.create_fat_ticket()
            def seed(document):
                original = document["tickets"][original_id]
                for i in range(24):
                    row = copy.deepcopy(original)
                    row["ticket_id"] = f"TK-page-{i:03d}"
                    document["tickets"][row["ticket_id"]] = row
            fixture.service.mutate("pursers", seed)
            before = copy.deepcopy(fixture.service.load("pursers")["tickets"])
            async with Client(fixture.mcp, mode="2026-07-28", cache=None) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                assert tools["ticket_list"].input_schema["properties"]["view"]["default"] == "summary"
                cursor = None
                ids = []
                while True:
                    args = {"board_id": "pursers"}
                    if cursor is not None:
                        args["cursor"] = cursor
                    result = await client.call_tool("ticket_list", args)
                    assert not result.is_error
                    page = result.structured_content
                    assert page["view"] == "summary"
                    assert len(page["tickets"]) <= 10
                    assert len(json.dumps(page).encode()) < 32768
                    assert all("description" not in row and "submission_history" not in row for row in page["tickets"])
                    assert all(row["counts"]["submissions"] == 20 for row in page["tickets"])
                    assert page["response_max_bytes"] == 32768
                    assert page["read_guidance"]["detail_tool"] == "ticket_get"
                    assert page["read_guidance"]["history_tool"] == "ticket_history_list"
                    assert page["read_guidance"]["page_complete"] is (not page["has_more"])
                    ids.extend(t["ticket_id"] for t in page["tickets"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert len(ids) == len(set(ids)) == 25
                assert set(ids) == set(before)
                # A smaller byte page changes boundaries, never stored content.
                cursor = None
                byte_page_ids = []
                while True:
                    result = await client.call_tool("ticket_list", {
                        "board_id": "pursers", "limit": 500, "max_bytes": 8192,
                        **({"cursor": cursor} if cursor else {})})
                    page = result.structured_content
                    assert len(json.dumps(page).encode()) <= 8192
                    byte_page_ids.extend(t["ticket_id"] for t in page["tickets"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert byte_page_ids == ids
                for ticket_id in ids:
                    result = await client.call_tool("ticket_get", {
                        "board_id": "pursers", "ticket_id": ticket_id, "view": "full"})
                    detail = result.structured_content["ticket"]
                    assert detail["description"] == before[ticket_id]["description"] == "d" * 5000
                    assert detail["submission_history"] == before[ticket_id]["submission_history"]
                cursor = None
                entries = []
                while True:
                    result = await client.call_tool("ticket_history_list", {
                        "board_id": "pursers", "ticket_id": original_id, "history": "submissions",
                        "limit": 3, **({"cursor": cursor} if cursor else {})})
                    page = result.structured_content
                    assert page["retention_complete"] is True
                    entries.extend(row["value"] for row in page["entries"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert entries == before[original_id]["submission_history"]
                snapshot = (await client.call_tool("board_snapshot", {"board_id": "pursers"})).structured_content
                assert snapshot["bounds"]["limit_per_collection"] == 10
                assert snapshot["bounds"]["max_bytes"] == 16384
                assert snapshot["truncated"] is True
                assert snapshot["omitted_counts"]["tickets"] > 0
            assert fixture.service.load("pursers")["tickets"] == before
        finally:
            await fixture.asyncTearDown()
    asyncio.run(scenario())


def test_explicit_full_detail_recovers_retained_overflow_even_when_one_entry_exceeds_page():
    async def scenario():
        fixture = bounds.ResponseBoundsTests(methodName="runTest")
        await fixture.asyncSetUp()
        try:
            ticket_id = await fixture.create_fat_ticket()
            older = {"annotation_id": "AN-old", "text": "literal old evidence " * 12000}
            latest = {"annotation_id": "AN-latest", "text": "latest evidence"}
            def seed(document):
                document["tickets"][ticket_id].update(annotations=[latest], annotations_omitted_count=1)
            fixture.service.mutate("pursers", seed)
            fixture.service.persist_history_overflow("pursers", ticket_id, "annotations", [older])
            before = copy.deepcopy(fixture.service.load("pursers")["tickets"][ticket_id])
            page = (await fixture.protocol_call("ticket_history_list", ticket_id=ticket_id,
                    history="annotations", limit=1)).structured_content
            assert page["entries"][0]["oversized_for_history_page"] is True
            full = (await fixture.protocol_call("ticket_get", ticket_id=ticket_id,
                    view="full")).structured_content["ticket"]
            assert full["annotations"] == [older, latest]
            assert full["annotations_omitted_count"] == 0
            assert full["annotation_count"] == 2
            assert fixture.service.load("pursers")["tickets"][ticket_id] == before
        finally:
            await fixture.asyncTearDown()
    asyncio.run(scenario())
