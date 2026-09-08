from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_home_has_guided_connection_team_and_recovery_surfaces() -> None:
    html = read("webui/index.html")
    for element_id in (
        "helper-form",
        "helper-url",
        "helper-token",
        "connect-helper",
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
    assert "helperTokenInput.value = '';" in script
    assert "x-pursers-home-token" in script
    assert "credentials = 'omit'" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script


def test_helper_transport_is_loopback_only_and_team_fallback_stays_disabled() -> None:
    script = read("webui/app.js")
    assert "loopbackHelperOrigin" in script
    assert "url.protocol !== 'http:'" in script
    assert "state.helper.baseUrl" in script
    assert "runtime_context_missing" in script
    assert "confirmStart.disabled = true" in script
    assert "Use native Team" in script
    assert "fetch(`${state.helper.baseUrl}${path}`" in script
    assert "updateJourney();\nshowGlobal('Connect the local helper first'" in script


def test_live_apply_uses_the_exact_confirmed_plan_snapshot() -> None:
    script = read("webui/app.js")
    assert "state.plannedSpec.seats.map" in script
    assert "const liveSpec = buildTeamSpec(true)" not in script
    assert "confirm: 'apply-live', dry_run: false" in script


def test_same_origin_mcp_import_forwards_aionui_csrf_cookie() -> None:
    script = read("webui/app.js")
    assert "document.cookie" in script
    assert "aionui-csrf-token=" in script
    assert "headers['x-csrf-token']" in script
    assert "credentials: 'same-origin'" in script
    assert "fetch('/api/mcp/servers/import'" in script


def test_appending_a_teammate_invalidates_the_confirmed_plan() -> None:
    script = read("webui/app.js")
    add_seat = script.split("function addSeat", 1)[1].split(
        "function buildTeamSpec", 1
    )[0]
    assert "if (match) return;" in add_seat
    assert "  resetPlan();\n  seatList.append(fragment);" in add_seat


def test_assets_are_packaged_and_do_not_load_remote_dependencies() -> None:
    html = read("webui/index.html")
    css = read("webui/style.css")
    urls = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert set(urls).issubset({
        "#main-content", "#home", "#connection", "#team", "#progress", "#advanced",
        "./style.css", "./app.js", "data:", "data:,",
    })
    assert "http://" not in html.replace("http://127.0.0.1:43121", "")
    assert "https://" not in html
    assert "@import" not in css
    assert "url(" not in css


def test_controls_have_touch_sized_targets_and_responsive_layout() -> None:
    css = read("webui/style.css")
    assert "min-height: 44px" in css
    assert re.search(r"\.remove-seat\s*\{[^}]*min-height:\s*44px", css, re.S)
    assert re.search(r"\.row-actions \.button\s*\{[^}]*min-height:\s*44px", css, re.S)
    assert re.search(r"\.confirm-row\s*\{[^}]*min-height:\s*44px", css, re.S)
    assert re.search(r"\.confirm-control\s*\{[^}]*width:\s*44px", css, re.S)
    assert 'class="confirm-control"' in read("webui/index.html")
    assert "@media (max-width: 680px)" in css
    assert "prefers-reduced-motion: reduce" in css
