from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE = (
    "- one ticket at a time",
    "- never review your own work",
    "- workers never call ticket_review",
    "- reviewers never work-claim/submit/write code/push",
    "- stay in ticket scope",
    "- report faithfully",
    "- never push main / never force-push",
)


def test_contexts_contain_seat_governance_verbatim() -> None:
    for name in ("worker.md", "reviewer.md"):
        text = (ROOT / "contexts" / name).read_text(encoding="utf-8")
        assert "relentless loop" in text
        for rule in GOVERNANCE:
            assert rule in text
