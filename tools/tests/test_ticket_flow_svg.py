from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ticket_flow_svgs_are_generated_and_current() -> None:
    result = subprocess.run(
        [sys.executable, "tools/media/ticket_flow_svg.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ticket_flow_svgs_honour_reduced_motion_and_fixed_themes() -> None:
    for theme in ("light", "dark"):
        svg = (ROOT / "docs" / "media" / f"ticket-flow-{theme}.svg").read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in svg
        assert "prefers-color-scheme" not in svg
        assert "<script" not in svg
