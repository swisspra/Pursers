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
    assert "pass 11" in completed.stdout


def test_node_helper_contract() -> None:
    test_file = Path(__file__).with_name("host_helper.test.cjs")
    completed = subprocess.run(
        ["node", "--test", str(test_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "pass 6" in completed.stdout
