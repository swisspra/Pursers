import json
import subprocess
from pathlib import Path


def test_projects_exposes_human_promotion_route_and_guided_fields():
    root = Path(__file__).parents[1] / 'ui'
    program = f"""
eval({json.dumps((root/'view-registry.js').read_text())});
eval({json.dumps((root/'views/projects.js').read_text())});
const html = FleetViewModules.render('projects', {{
 esc: x => String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
 pageHead:()=>'',warmTruthStrip:()=>'',warmBoards:()=>[],numberCount:Number,boardHref:()=>'',centralLabels:['default']
}});
console.log(JSON.stringify(html));
"""
    html = json.loads(subprocess.run(['node', '-e', program], check=True, capture_output=True, text=True).stdout)
    assert 'id="project-delivery-form"' in html
    assert 'name="integration_branch"' in html
    assert 'name="base_branch"' in html
    assert 'name="production_branch"' not in html
    assert 'Your team owns the final merge' in html
    assert 'Preview delivery changes' in html
    assert 'name="mode"' in html
    assert 'value="batch_pr"' in html
    assert 'name="release_trigger"' in html
    assert 'Reset repository overrides to inherit' in html
    assert 'Unsupported runtime choices remain drafts' in html
    assert 'password' not in html
