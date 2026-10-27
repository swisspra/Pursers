"""Projects follow registry membership while board summaries remain board-scoped."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from test_fleet_dashboard import dashboard, registry


@pytest.mark.parametrize("discovery", ["_boards", "_summary_boards"])
def test_fetch_preserves_multiple_registered_projects_on_one_board(monkeypatch, discovery):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def board_state_get(self, *, key):
            assert key == "project_registry"
            return registry(
                {
                    "api": {"board_id": "shared", "status": "active"},
                    "web": {"board_id": "shared", "status": "active"},
                }
            )

        async def board_list(self):
            return {"boards": []}

    config = dashboard.Config(
        url="http://localhost/mcp",
        token="test",
        home_board="home",
        agent_name="test",
        stale_seconds=300,
        cache_seconds=5,
    )
    fetcher = dashboard.FleetFetcher(config)
    monkeypatch.setattr(fetcher, "_client", lambda _board: Client())
    try:
        discover = getattr(fetcher, discovery)
        if discovery == "_boards":
            boards = asyncio.run(discover(include_seat_definitions=False))
        else:
            boards = asyncio.run(discover())
        result = fetcher._aggregate_rows([], phase="complete")
        assert boards == [("home", "home"), ("api", "shared")]
        assert result["registered_projects"] == [
            {"name": "api", "board_id": "shared"},
            {"name": "web", "board_id": "shared"},
        ]
    finally:
        fetcher.close()


def render_projects(fleet):
    fleet = {
        central: {
            "generated_at": "2026-10-27T00:00:00Z",
            "refresh": {"complete": True},
            **data,
        }
        for central, data in fleet.items()
    }
    root = Path(__file__).parents[1] / "ui"
    program = f"""
eval({json.dumps((root / 'view-registry.js').read_text())});
eval({json.dumps((root / 'views/projects.js').read_text())});
const fleetData = {json.dumps(fleet)};
const html = FleetViewModules.render('projects', {{
 esc: x => String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
 pageHead:()=>'',warmTruthStrip:()=>' ',warmBoards:()=>[],
 numberCount:Number,boardHref:(central,board)=>`#/${{central}}/${{board}}`,
 centralLabels:Object.keys(fleetData),fleetData
}});
console.log(JSON.stringify(html));
"""
    return json.loads(
        subprocess.run(
            ["node", "-e", program], check=True, capture_output=True, text=True
        ).stdout
    )


def test_projects_render_registry_names_and_count_shared_board_once():
    html = render_projects(
        {
            "default": {
                "registered_projects": [
                    {"name": "api", "board_id": "shared"},
                    {"name": "web", "board_id": "shared"},
                ],
                "boards": [
                    {
                        "board_id": "shared",
                        "label": "misleading-board-label",
                        "status": "ready",
                        "counts": {"claimed": 2, "submitted": 1},
                    },
                    {
                        "board_id": "probe",
                        "label": "probe",
                        "status": "ready",
                        "counts": {"claimed": 99},
                    },
                ],
            }
        }
    )
    assert "2 projects" in html
    assert "<h3>api</h3>" in html and "<h3>web</h3>" in html
    assert "probe" not in html and "misleading-board-label" not in html
    assert 'data-project-remove="web"' in html
    assert 'href="#/default/shared">Open project</a>' in html
    assert "In progress</dt><dd>2</dd>" in html
    assert "In progress</dt><dd>4</dd>" not in html
    assert "Shared board totals" in html


def test_projects_show_registered_entry_when_no_board_summary_is_readable():
    html = render_projects(
        {
            "default": {
                "registered_projects": [
                    {"name": "api", "board_id": "private"},
                    {"name": "worker", "board_id": "readable"},
                ],
                "boards": [
                    {
                        "board_id": "readable",
                        "label": "wrong-board-label",
                        "status": "ready",
                        "counts": {"claimed": 3},
                    }
                ],
            }
        }
    )
    assert "<h3>api</h3>" in html and "<h3>worker</h3>" in html
    assert "Summary pending" in html
    assert "0 tickets in the Fleet summary" not in html
    assert "3 tickets in the Fleet summary" in html
    summary = html.split('<section class="projects-summary"', 1)[1].split("</section>", 1)[0]
    assert "<dd>—</dd>" in summary
    assert "<dd>0</dd>" not in summary


def test_projects_distinguish_unavailable_registry_from_empty_registry():
    unavailable = render_projects({"default": {"boards": []}})
    empty = render_projects(
        {"default": {"registered_projects": [], "boards": []}}
    )
    assert "Project registry unavailable" in unavailable
    assert "Connect your first project" not in unavailable
    assert "Connect your first project" in empty
