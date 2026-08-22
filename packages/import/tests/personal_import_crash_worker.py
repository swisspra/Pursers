#!/usr/bin/env python3
"""Test-only process-death worker for durable Personal import recovery."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from personal_import import (
    retry_import,
    review_import,
    rollback_import,
    start_import,
    status_import,
)


def main() -> None:
    operation = sys.argv[1]
    config = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    kill_at = sys.argv[3] if len(sys.argv) > 3 else None

    def hard_exit(name: str) -> None:
        if name == kill_at:
            os._exit(97)

    run = Path(config["run"])
    if operation == "start":
        state = start_import(
            Path(config["source"]),
            Path(config["destination"]),
            run,
            board_id=config["board_id"],
            owner_principal_id=config["owner_principal_id"],
            owner_agent_name=config["owner_agent_name"],
            stable_install_root=Path(config["stable"]),
            confirm_central_stopped=True,
            checkpoint=hard_exit,
        )
    elif operation == "retry":
        state = retry_import(
            run,
            confirm_central_stopped=True,
            checkpoint=hard_exit,
        )
    elif operation == "review":
        state = review_import(
            run,
            bindings_path=(Path(config["bindings"]) if config.get("bindings") else None),
            decisions_path=(Path(config["decisions"]) if config.get("decisions") else None),
            confirm_central_stopped=True,
            checkpoint=hard_exit,
        )
    elif operation == "rollback":
        state = rollback_import(
            run,
            confirm_central_stopped=True,
            checkpoint=hard_exit,
        )
    elif operation == "status":
        state = status_import(run, confirm_central_stopped=True)
    else:
        raise ValueError("unsupported test operation")
    print(json.dumps({"phase": state["phase"]}, sort_keys=True))


if __name__ == "__main__":
    main()
