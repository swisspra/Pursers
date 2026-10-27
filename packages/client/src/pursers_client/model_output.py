"""Lossless duplicate removal for model-facing ticket projections."""

from __future__ import annotations

from typing import Any


def compact_ticket_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep canonical evidence; retain histories and aliases that differ."""
    def compact(ticket: dict[str, Any]) -> dict[str, Any]:
        result = dict(ticket)
        for latest, history in (
            ("latest_submission", "submission_history"),
            ("latest_verdict", "review_history"),
        ):
            entry = ticket.get(latest)
            if isinstance(entry, dict) and ticket.get(history) == [entry]:
                result.pop(history, None)
        verdict = ticket.get("latest_verdict")
        if isinstance(verdict, dict):
            for alias, field in (
                ("review_notes", "review_notes"),
                ("fix_instructions", "fix_instructions"),
                ("review_verdict", "verdict"),
            ):
                if alias in ticket and field in verdict and ticket[alias] == verdict[field]:
                    result.pop(alias, None)
        return result

    result = dict(payload)
    if isinstance(payload.get("ticket"), dict):
        result["ticket"] = compact(payload["ticket"])
    if isinstance(payload.get("tickets"), list):
        result["tickets"] = [
            compact(ticket) if isinstance(ticket, dict) else ticket
            for ticket in payload["tickets"]
        ]
    return result
