"""Projects are registry entries; readable boards are not project membership."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from test_fleet_dashboard import dashboard, registry


@pytest.mark.parametrize('discovery', ['_boards', '_summary_boards'])
def test_fleet_projects_preserve_shared_board_names_and_exclude_non_members(discovery, monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def board_state_get(self, *, key):
            return registry({
                'api': {'board_id': 'shared', 'status': 'active', 'work_dir': '/private/api'},
                'web': {'board_id': 'shared', 'status': 'active', 'work_dir': '/private/web'},
                'retired': {'board_id': 'old', 'status': 'inactive'},
                'disabled': {'board_id': 'disabled', 'status': 'active', 'fleet': False},
            })

        async def board_list(self):
            return {'boards': [{'board_id': 'shared'}, {'board_id': 'test-probe'}]}

    config = dashboard.Config(url='http://localhost/mcp', token='test', home_board='home',
                              agent_name='test', stale_seconds=300, cache_seconds=5)
    fetcher = dashboard.FleetFetcher(config)
    monkeypatch.setattr(fetcher, '_client', lambda _board: Client())
    try:
        asyncio.run(getattr(fetcher, discovery)())
        result = fetcher._aggregate_rows([], phase='complete')
        assert result.get('registered_projects') == [
            {'name': 'api', 'board_id': 'shared'}, {'name': 'web', 'board_id': 'shared'},
        ]
    finally:
        fetcher.close()


def render_projects(fleet):
    root = Path(__file__).parents[1] / 'ui'
    program = f"""
eval({json.dumps((root/'view-registry.js').read_text())});
eval({json.dumps((root/'views/projects.js').read_text())});
const fleetData = {json.dumps(fleet)};
const html = FleetViewModules.render('projects', {{
 esc: x => String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
 pageHead:()=>'',warmTruthStrip:()=>'',
 warmBoards:()=>Object.entries(fleetData).flatMap(([central,data])=>(data.boards||[]).map(board=>({{central,board}}))),
 numberCount:Number,boardHref:(central,board)=>`#/${{central}}/${{board}}`,
 centralLabels:Object.keys(fleetData),fleetData
}});
console.log(JSON.stringify(html));
"""
    return json.loads(subprocess.run(['node', '-e', program], check=True, capture_output=True, text=True).stdout)


def test_project_cards_follow_registry_and_do_not_double_count_shared_board():
    html = render_projects({'default': {
        'registered_projects': [{'name': 'api', 'board_id': 'shared'}, {'name': 'web', 'board_id': 'shared'},
                                {'name': 'pending', 'board_id': 'not-yet-readable'}],
        'boards': [{'board_id': 'shared', 'label': 'misleading-board-label', 'status': 'ready',
                    'counts': {'claimed': 2, 'submitted': 1}},
                   {'board_id': 'test-probe', 'label': 'test-probe', 'status': 'ready', 'counts': {'claimed': 99}}],
    }})
    assert '3 projects' in html
    assert '<h3>api</h3>' in html and '<h3>web</h3>' in html
    assert 'test-probe' not in html and 'misleading-board-label' not in html
    assert 'data-project-remove="web"' in html
    assert 'href="#/default/shared">Open project</a>' in html
    assert 'In progress</dt><dd>2</dd>' in html
    assert 'In progress</dt><dd>4</dd>' not in html
    assert 'Shared board totals' in html
    assert '<h3>pending</h3>' in html
    assert 'Summary pending' in html


def test_projects_do_not_fall_back_to_boards_when_registry_unavailable():
    html = render_projects({'default': {'boards': [{'board_id': 'probe', 'label': 'probe'}]}})
    assert 'data-projects-card' not in html
    assert 'Project registry unavailable' in html
    assert 'Connect your first project' not in html


def test_empty_registry_does_not_promote_readable_boards_to_projects():
    html = render_projects({'default': {'registered_projects': [], 'boards': [{'board_id': 'probe', 'label': 'probe'}]}})
    assert 'data-projects-card' not in html
    assert 'Connect your first project' in html
