#!/usr/bin/env python3
"""Run the Personal MCP App observer scripts against a real Ego Lite browser.

This worker-owned dry-run is deterministic and synthetic. It proves that the
observer selects a host-embedded MCP App frame, executes a transition inside
that frame, hashes the actual resource URL, and fails closed on bad bindings.
It is not verifier-owned final acceptance evidence.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))

import beta_blocking_fixture_server as fixture_server  # noqa: E402
import browser_observer as observer  # noqa: E402

BOARD_ID = "sandbox-home-acceptance"
ARTIFACT = "packages/personal/src/pursers_personal/resources/dashboard.html"


class DryRunError(RuntimeError):
    """A dry-run assertion failed."""


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(REPOSITORY_ROOT), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise DryRunError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


@contextmanager
def _fixture(candidate_commit: str) -> Iterator[str]:
    server = fixture_server.ThreadingHTTPServer(
        ("127.0.0.1", 0), fixture_server.Handler
    )
    server.candidate_commit = candidate_commit  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(
    origin: str, ego_browser: Path, task_space: str, store: Path,
    candidate_commit: str, artifact_sha256: str,
) -> dict[str, Any]:
    return {
        "store": store,
        "repository_root": str(REPOSITORY_ROOT),
        "backend": {
            "kind": "ego-browser",
            "command": [str(ego_browser)],
            "task_space": task_space,
            "timeout_s": 120,
        },
        "surfaces": {
            "aionui": {},
            "fleet": {},
            "mcp-app": {
                "adapter": "pinned-signed-aionui-personal-mcp",
                "target": {"base_url": origin, "board_id": BOARD_ID},
                "candidate_manifest_url": f"{origin}/candidate.json",
                "artifact": ARTIFACT,
                "artifact_sha256": artifact_sha256,
                "candidate_commit": candidate_commit,
                "product": "Pursers Personal",
                "version": f"candidate-{candidate_commit[:12]}",
            },
        },
    }


def _assert_capture_binding(
    capture: dict[str, Any], candidate_commit: str, artifact_sha256: str,
) -> None:
    candidate = capture.get("candidate_status")
    manifest = candidate.get("payload") if isinstance(candidate, dict) else None
    if (
        not isinstance(candidate, dict)
        or candidate.get("http_status") != 200
        or not isinstance(manifest, dict)
        or manifest.get("candidate_commit") != candidate_commit
    ):
        raise DryRunError("candidate.json did not bind the browser capture")
    if capture.get("selected_board") != BOARD_ID:
        raise DryRunError("Personal MCP App sandbox board did not match")
    if capture.get("page_sha256") != artifact_sha256:
        raise DryRunError("served Personal artifact did not match exact dashboard.html bytes")


def _expect_failure(label: str, action: Callable[[], object]) -> str:
    try:
        action()
    except (DryRunError, observer.ObserverError):
        return label
    raise DryRunError(f"{label} did not fail closed")


def _cleanup_task_space(ego_browser: Path, task_space: str) -> None:
    script = (
        f"const task = await taskSpace({json.dumps(task_space)});\n"
        "await task.finish({keep: []});\n"
    )
    environment = {
        "PATH": os.pathsep.join([str(ego_browser.parent), os.defpath]),
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": os.environ.get("HOME") or str(HERE),
    }
    completed = subprocess.run(
        [str(ego_browser), "nodejs"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        cwd=HERE,
        env=environment,
    )
    if completed.returncode:
        diagnostic = (completed.stderr or completed.stdout).strip().splitlines()
        detail = diagnostic[-1][:240] if diagnostic else "no diagnostic"
        raise DryRunError(f"Ego Lite task-space cleanup failed: {detail}")


def run(ego_browser: Path, task_space: str) -> dict[str, Any]:
    candidate_commit = _git("rev-parse", "--verify", "HEAD^{commit}")
    if len(candidate_commit) != 40:
        raise DryRunError("candidate commit is not a full SHA")
    artifact_sha256 = hashlib.sha256(
        (REPOSITORY_ROOT / ARTIFACT).read_bytes()
    ).hexdigest()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pursers-mcp-app-dry-run-") as raw_store:
        with _fixture(candidate_commit) as origin:
            config = _config(
                origin, ego_browser, task_space, Path(raw_store),
                candidate_commit, artifact_sha256,
            )
            page_url = f"{origin}/mcp-host/one/"
            capture = observer._run_backend(config, page_url, "mcp-app")
            _assert_capture_binding(capture, candidate_commit, artifact_sha256)

            recipe = {
                "before": [{
                    "path": "/fleet_hidden",
                    "selector": "#view-fleet",
                    "property": "hidden",
                }],
                "actions": [{
                    "kind": "click",
                    "selector": "#tab-fleet",
                    "path": "/clicked",
                }],
                "after": [{
                    "path": "/fleet_hidden",
                    "selector": "#view-fleet",
                    "property": "hidden",
                }],
                "settle_milliseconds": 0,
            }
            transition = observer._run_transition_backend(
                config, page_url, recipe, "mcp-app"
            )
            _assert_capture_binding(transition, candidate_commit, artifact_sha256)
            if (
                transition["before"].get("/fleet_hidden") is not True
                or transition["action"].get("/clicked") != "clicked"
                or transition["after"].get("/fleet_hidden") is not False
            ):
                observed = {
                    "before": transition["before"],
                    "action": transition["action"],
                    "after": transition["after"],
                }
                raise DryRunError(
                    f"MCP App frame transition did not complete: {json.dumps(observed, sort_keys=True)}"
                )

            for mode in ("absent", "ambiguous", "wrong-board"):
                failures.append(_expect_failure(
                    mode,
                    lambda mode=mode: observer._run_backend(
                        config, f"{origin}/mcp-host/{mode}/", "mcp-app"
                    ),
                ))

            wrong_bytes = observer._run_backend(
                config, f"{origin}/mcp-host/wrong-bytes/", "mcp-app"
            )
            failures.append(_expect_failure(
                "wrong-bytes",
                lambda: _assert_capture_binding(
                    wrong_bytes, candidate_commit, artifact_sha256
                ),
            ))

            cross_origin = copy.deepcopy(config)
            cross_origin["surfaces"]["mcp-app"]["candidate_manifest_url"] = (
                f"http://localhost:{urlsplit(origin).port}/candidate.json"
            )
            failures.append(_expect_failure(
                "cross-origin-candidate",
                lambda: observer._candidate_manifest_url(
                    cross_origin, page_url, "mcp-app"
                ),
            ))

            result = {
                "ok": True,
                "label": "NOT final",
                "surface": "mcp-app",
                "browser_backend": "ego-browser",
                "host_mechanism": "PostMessageTransport fixture host",
                "page_url": page_url,
                "selected_board": capture["selected_board"],
                "page_sha256": capture["page_sha256"],
                "transition": {
                    "before": transition["before"],
                    "action": transition["action"],
                    "after": transition["after"],
                },
                "fail_closed": failures,
            }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ego-browser", required=True, type=Path)
    parser.add_argument("--task-space", required=True)
    args = parser.parse_args()
    ego_browser = args.ego_browser.resolve()
    if not ego_browser.is_file() or not os.access(ego_browser, os.X_OK):
        raise DryRunError("--ego-browser must name an executable Ego Lite CLI")
    if not args.task_space or len(args.task_space) > 128:
        raise DryRunError("--task-space must contain 1-128 characters")
    try:
        result = run(ego_browser, args.task_space)
    except Exception:
        try:
            _cleanup_task_space(ego_browser, args.task_space)
        except DryRunError:
            pass
        raise
    _cleanup_task_space(ego_browser, args.task_space)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DryRunError, observer.ObserverError) as exc:
        print(f"mcp_app_frame_dry_run: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
