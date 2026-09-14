from __future__ import annotations

import subprocess
from pathlib import Path


def test_node_webui_app_contract() -> None:
    test_file = Path(__file__).with_name("webui_app.test.cjs")
    completed = subprocess.run(
        ["node", "--test", str(test_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "pass 9" in completed.stdout


def test_join_markup_requires_validate_then_connect() -> None:
    markup = Path(__file__).parents[1] / "webui" / "index.html"
    text = markup.read_text()
    assert 'id="validate-door"' in text
    assert 'id="door-confirmation"' in text
    assert 'id="connect-door"' in text
    assert text.index('id="validate-door"') < text.index('id="connect-door"')
