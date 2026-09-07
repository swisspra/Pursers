"""Deterministic import paths for the central suite.

``tools/ci_manifest.py`` runs every suite from a plain interpreter, and CI
installs the checkout packages; locally the checkout client and central
sources must be importable before any test module loads, independent of
collection order.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
CENTRAL_SRC = PACKAGE_ROOT / "src" / "pursers_central"

for _entry in (str(CLIENT_SRC), str(CENTRAL_SRC)):
    if _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)
