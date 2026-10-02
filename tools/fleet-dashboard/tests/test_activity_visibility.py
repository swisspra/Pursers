from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fleet_activity_visibility", ROOT / "activity_visibility.py"
)
assert SPEC and SPEC.loader
activity_visibility = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = activity_visibility
SPEC.loader.exec_module(activity_visibility)


def record() -> dict:
    return {
        "schema_version": 1,
        "stage": "review",
        "state": "running",
        "attempt_id": 2,
        "actor_id": "AI-reviewer",
        "updated_at": "2030-01-02T12:00:00Z",
        "freshness": "fresh",
        "evidence_refs": [
            "board://private/ticket/TK-one#submission",
            "board://private/ticket/TK-one#review",
        ],
        "next_action": "Record an independent verdict.",
        "completion_boundary": "unknown",
        "estimate": {
            "low_percent": 40,
            "high_percent": 60,
            "confidence": "medium",
            "evidence": "Focused checks pass.",
            "assessed_at": "2030-01-02T11:55:00Z",
        },
    }


def test_activity_projection_allowlists_and_bounds_untrusted_fields() -> None:
    value = record()
    value["private"] = "must not cross the dashboard boundary"
    value["evidence_refs"] = [
        *value["evidence_refs"],
        *[f"board://private/ticket/TK-one#extra-{index}" for index in range(20)],
        "x" * 400,
        42,
    ]
    value["blocking_reason"] = "Needs an operator decision."

    projected = activity_visibility.project_activity(value)

    assert projected is not None
    assert "private" not in projected
    assert projected["blocking_reason"] == "Needs an operator decision."
    assert len(projected["evidence_refs"]) == activity_visibility.MAX_EVIDENCE_REFS
    assert all(len(item) <= activity_visibility.MAX_REF_CHARS for item in projected["evidence_refs"])
    assert projected["estimate"]["evidence"] == "Focused checks pass."


def test_activity_projection_rejects_unknown_schema_and_invalid_vocabulary() -> None:
    assert activity_visibility.project_activity(None) is None
    assert activity_visibility.project_activity({"schema_version": 2}) is None

    invalid_stage = record()
    invalid_stage["stage"] = "deployed"
    assert activity_visibility.project_activity(invalid_stage) is None

    invalid_state = record()
    invalid_state["state"] = "almost-done"
    assert activity_visibility.project_activity(invalid_state) is None


def test_activity_projection_preserves_unknowns_without_fake_estimates() -> None:
    value = record()
    value.update(
        stage="delivery",
        state="unknown",
        freshness="unknown",
        actor_id=None,
        attempt_id=None,
        completion_boundary="unknown",
    )
    value.pop("estimate")

    projected = activity_visibility.project_activity(value)

    assert projected is not None
    assert projected["state"] == "unknown"
    assert projected["actor_id"] is None
    assert projected["attempt_id"] is None
    assert "estimate" not in projected
