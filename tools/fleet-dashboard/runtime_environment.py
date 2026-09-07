"""Lazy, injectable access to dashboard state and process inspection."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


STATE_DIR_ENV = "PURSERS_STATE_DIR"
PROCESS_INSPECTION_UNAVAILABLE = "process inspection unavailable"


def pursers_state_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve local state only when a caller first needs it."""
    selected_env = os.environ if env is None else env
    configured = selected_env.get(STATE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path.home() / ".pursers").resolve()


def dashboard_state_dir(env: Mapping[str, str] | None = None) -> Path:
    return pursers_state_root(env) / "fleet-dashboard"


@dataclass(frozen=True)
class ProcessInspection:
    available: bool
    stdout: str = ""
    message: str = ""


def _default_process_list_provider(
    command: Sequence[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> ProcessInspection:
    try:
        completed = runner(
            list(command),
            check=False,
            text=True,
            capture_output=True,
        )
    except (PermissionError, FileNotFoundError):
        return ProcessInspection(False, message=PROCESS_INSPECTION_UNAVAILABLE)
    if completed.returncode != 0:
        return ProcessInspection(False, message=PROCESS_INSPECTION_UNAVAILABLE)
    return ProcessInspection(True, stdout=str(completed.stdout))


# Tests and sandbox-aware embeddings replace this provider without patching
# subprocess globally. Production keeps the guarded implementation above.
PROCESS_LIST_PROVIDER = _default_process_list_provider
