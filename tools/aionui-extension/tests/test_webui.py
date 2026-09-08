from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_home_has_guided_connection_team_and_recovery_surfaces() -> None:
    html = read("webui/index.html")
    for element_id in (
        "connection-form",
        "recover-seat",
        "team-form",
        "plan-card",
        "confirm-start",
        "roster-list",
        "stop-dialog",
    ):
        assert f'id="{element_id}"' in html
    assert "one worker and one independent reviewer" not in html.lower()
    assert "Resume" in html
    assert "no Team resume operation" in html
    assert "board_snapshot" in html


def test_door_field_and_client_keep_secret_out_of_persistent_ui() -> None:
    html = read("webui/index.html")
    script = read("webui/app.js")
    door_tag = re.search(r'<input id="door"[^>]+>', html)
    assert door_tag
    assert 'type="password"' in door_tag.group(0)
    assert 'autocomplete="new-password"' in door_tag.group(0)
    assert "doorInput.value = '';" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script


def test_live_apply_uses_the_exact_confirmed_plan_snapshot() -> None:
    script = read("webui/app.js")
    assert "state.plannedSpec.seats.map" in script
    assert "const liveSpec = buildTeamSpec(true)" not in script
    assert "confirm: 'apply-live', dry_run: false" in script


def test_assets_are_packaged_and_do_not_load_remote_dependencies() -> None:
    html = read("webui/index.html")
    css = read("webui/style.css")
    urls = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert set(urls).issubset({
        "#main-content", "#home", "#connection", "#team", "#progress", "#advanced",
        "/pursers/assets/style.css", "/pursers/assets/app.js", "data:", "data:,",
    })
    assert "http://" not in html
    assert "https://" not in html
    assert "@import" not in css
    assert "url(" not in css


def test_controls_have_touch_sized_targets_and_responsive_layout() -> None:
    css = read("webui/style.css")
    assert "min-height: 44px" in css
    assert "@media (max-width: 680px)" in css
    assert "prefers-reduced-motion: reduce" in css
