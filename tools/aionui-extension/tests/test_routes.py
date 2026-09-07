from __future__ import annotations

import subprocess
from pathlib import Path


def test_node_route_contract() -> None:
    test_file = Path(__file__).with_name("routes.test.cjs")
    completed = subprocess.run(
        ["node", "--test", str(test_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "pass 3" in completed.stdout
