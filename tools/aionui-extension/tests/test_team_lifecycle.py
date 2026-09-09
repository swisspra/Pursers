from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_home_exposes_accessible_standalone_group_lifecycle() -> None:
    html = read("webui/index.html")
    script = read("webui/app.js")
    for element_id in (
        "groups", "group-form", "group-name", "group-members", "save-group",
        "group-message", "group-list", "groups-empty", "remove-group-dialog",
    ):
        assert f'id="{element_id}"' in html
    assert 'aria-live="polite"' in html
    assert "Groups do not create, start, stop, or dispatch seats." in html
    assert "expected_revision: state.groupRevision" in script
    assert "member.present ? member.lifecycle_status : 'missing from board'" in script
    assert "Only the group is removed" in html


def test_group_ui_uses_authenticated_helper_routes_without_secret_storage() -> None:
    script = read("webui/app.js")
    for route in (
        "/pursers/groups", "/pursers/groups/${operation}", "/pursers/groups/remove",
    ):
        assert route in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script
