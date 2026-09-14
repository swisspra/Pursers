"""Prefer this checkout's packages over globally installed Pursers builds."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
for package in ("personal", "central", "client"):
    source = str(REPOSITORY / "packages" / package / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
