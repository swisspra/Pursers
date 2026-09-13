"""Synthetic fixture loader for TK-3951189e9012 beta-blocking rows.

The door fixtures are descriptors, not reusable credentials.  A caller supplies
the disposable loopback port and receives a short-lived, synthetic door.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "beta_blocking"
BOARD_ID = "sandbox-home-acceptance"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _segment(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def materialize_door(name: str, port: int) -> str:
    """Build a disposable synthetic door for the shipped adapter parser."""
    fixture = load_fixture(name)
    credential = fixture["credential"]
    token = ".".join(
        (
            _segment({"alg": credential["algorithm"], "kid": credential["key_id"]}),
            _segment({"exp": credential["expires_at"]}),
            credential["signature_marker"],
        )
    )
    envelope = {
        "u": f"http://127.0.0.1:{port}{fixture['central_path']}",
        "b": fixture["board_id"],
        "r": fixture["role"],
        "t": token,
    }
    return f"prs1.{_segment(envelope)}"
