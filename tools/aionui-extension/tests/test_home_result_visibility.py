from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_home_exposes_accessible_read_only_result_controls() -> None:
    html = read("webui/index.html")
    script = read("webui/app.js")

    for element_id in (
        "results-title",
        "results-pill",
        "result-state",
        "refresh-results",
        "results-message",
        "results-empty",
        "submission-list",
    ):
        assert f'id="{element_id}"' in html
    assert 'role="status" aria-live="polite"' in html
    assert "GET /pursers/results" not in html
    assert "api(`/pursers/results${query}`)" in script
    assert "innerHTML" not in script
    assert "textContent = item.submission.summary" in script
    assert not re.search(r"api\([^)]*pursers/results[^)]*,\s*\{", script)


def test_result_states_and_recovery_copy_are_explicit() -> None:
    html = read("webui/index.html")
    script = read("webui/app.js")

    for state in ("missing", "pending", "approved", "rejected", "failed"):
        assert f'value="{state}"' in html
        assert f"{state}:" in script
    assert "backend_unavailable" in script
    assert "Start the local Fleet dashboard and retry." in script
    assert "No matching results" in script
    assert "data-helper-field=\"central\"" in html
    assert "expectedCentral" in read("webui/routes.js")


def test_result_runtime_files_are_in_the_deterministic_allowlist() -> None:
    build = read("build.py")
    routes = read("webui/routes.js")

    assert '"result_visibility/adapter.cjs"' in build
    assert '"result_visibility/FEATURE_CONTRACT.md"' in build
    assert "'/pursers/results'" in routes
    assert "request.method === 'GET'" in routes
