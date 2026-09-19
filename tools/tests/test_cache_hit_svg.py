from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cache_hit_svgs_are_generated_and_current() -> None:
    result = subprocess.run(
        [sys.executable, "tools/media/cache_hit_svg.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cache_hit_svgs_honour_reduced_motion_and_fixed_themes() -> None:
    for theme in ("light", "dark"):
        svg = (ROOT / "docs" / "media" / f"cache-hit-{theme}.svg").read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in svg
        assert "prefers-color-scheme" not in svg
        assert "<script" not in svg


def test_cache_hit_shares_match_the_evidence_page() -> None:
    evidence = (ROOT / "docs" / "evidence" / "cache-efficiency.md").read_text(encoding="utf-8")
    published = set(re.findall(r"(\d+\.\d)%", evidence))
    svg = (ROOT / "docs" / "media" / "cache-hit-light.svg").read_text(encoding="utf-8")
    shown = set(re.findall(r">(\d+\.\d)%<", svg))
    assert shown and shown <= published
