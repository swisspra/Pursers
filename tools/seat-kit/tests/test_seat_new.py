from __future__ import annotations

import asyncio
import contextvars
import html
import importlib.util
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import base64
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
CENTRAL_SRC = ROOT.parents[1] / "packages" / "central" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(CENTRAL_SRC))
from pursers_client import REVIEWER_WAIT_KINDS, WORKER_WAIT_KINDS  # noqa: E402

SPEC = importlib.util.spec_from_file_location("seat_new", ROOT / "seat_new.py")
assert SPEC and SPEC.loader
seat_new = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seat_new)


def args(
    tmp_path: Path,
    *,
    role: str = "worker",
    repo: str | None = None,
    client: str = "codex",
):
    return seat_new.build_parser().parse_args(
        [
            "--role",
            role,
            "--name",
            f"{role}-a",
            "--dest",
            str(tmp_path / role),
            "--central-url",
            "https://central.example/mcp",
            "--token-file",
            str(tmp_path / "seat.jwt"),
            "--client",
            client,
            *(["--repo", repo] if repo else []),
        ]
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def synthetic_door(*, role: str = "worker") -> str:
    def segment(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    compact = (
        f"{segment({'alg': 'RS256', 'kid': 'seat-test'})}."
        f"{segment({'exp': 2_000_000_000})}.synthetic-signature"
    )
    return f"prs1.{segment({'u': 'http://127.0.0.1:8766/mcp', 'b': 'sandbox', 'r': role, 't': compact})}"


def load_generated(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def hermetic_interpreter_check(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep seat generation independent of the host interpreter's site-packages.

    generate() probes the selected interpreter for pursers_client/mcp/httpx. CI
    runners (and developer shells) may run pytest from an interpreter that only
    sees those modules via sys.path, so the probe is stubbed unless a test opts
    into the real check with @pytest.mark.real_interpreter_check.
    """
    if request.node.get_closest_marker("real_interpreter_check"):
        return
    monkeypatch.setattr(seat_new, "_validate_interpreter", lambda python: None)


@pytest.fixture(autouse=True)
def isolated_operator_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    marker_file = tmp_path / "leak-markers.txt"
    marker_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("PURSERS_LEAK_MARKERS_FILE", str(marker_file))
    return marker_file


class LocalSubscriptionAdapter:
    """Approved BoardClient.events contract over a real in-process Central."""

    def __init__(self, raw_client: Any, service: Any, agent_id: str) -> None:
        self.raw_client = raw_client
        self.service = service
        self.identity = SimpleNamespace(agent_id=agent_id)
        self.ready = asyncio.Event()
        self.events_calls: list[dict[str, Any]] = []
        self.ticket_list_calls = 0
        self.catchup_calls = 0

    async def ticket_list(self, **_arguments: Any) -> dict[str, Any]:
        self.ticket_list_calls += 1
        raise AssertionError("default wait must not call ticket_list")

    async def board_catchup(self, **_arguments: Any) -> dict[str, Any]:
        self.catchup_calls += 1
        raise AssertionError("default wait must delegate pure refetch to events()")

    async def events(
        self,
        from_cursor: int | None = None,
        *,
        only_mine: bool = True,
        kinds: frozenset[str] | None = None,
        resource_subscriptions: tuple[str, ...] | None = None,
        acknowledge: bool = True,
        touch: bool | None = None,
        cursor_callback: Any = None,
    ):
        selected = kinds or frozenset()
        cursor = int(from_cursor or 0)
        subscriptions = tuple(resource_subscriptions or ())
        self.events_calls.append(
            {
                "from_cursor": from_cursor,
                "only_mine": only_mine,
                "kinds": selected,
                "resource_subscriptions": subscriptions,
                "acknowledge": acknowledge,
                "touch": touch,
            }
        )
        assert acknowledge is False
        assert touch is False
        if cursor_callback is not None:
            cursor_callback(cursor)
        async with self.raw_client.listen(
            resource_subscriptions=list(subscriptions)
        ) as subscription:
            self.ready.set()
            async for _cue in subscription:
                page = self.service.journal.read_after("pursers", cursor, 100)
                cursor = int(page["next_cursor"])
                if cursor_callback is not None:
                    cursor_callback(cursor)
                for event in page["events"]:
                    if event.get("kind") not in selected:
                        continue
                    if event.get("actor") == self.identity.agent_id:
                        continue
                    if only_mine and self.identity.agent_id not in event.get(
                        "recipient_identities", []
                    ):
                        continue
                    yield event


def persisted_documents(service: Any) -> list[tuple[str, str, int]]:
    connection = sqlite3.connect(service.store.db_path)
    try:
        return connection.execute(
            "SELECT path, doc, version FROM documents ORDER BY path"
        ).fetchall()
    finally:
        connection.close()


def test_worker_folder_permissions_and_secret_safety(tmp_path: Path) -> None:
    secret = "SECRET_MUST_NOT_BE_COPIED"
    (tmp_path / "seat.jwt").write_text(secret, encoding="utf-8")
    (tmp_path / "ca.pem").write_text("synthetic CA", encoding="utf-8")

    dest = seat_new.generate(args(tmp_path))

    assert mode(dest) == 0o700
    assert mode(dest / "bin") == 0o755
    assert mode(dest / "bin" / "board.sh") == 0o755
    assert mode(dest / "bin" / "board.py") == 0o644
    assert (dest / "AGENTS.md").read_text() == (dest / ".goosehints").read_text()
    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            dest / "bin" / "board.sh",
            dest / "bin" / "board.py",
            dest / "AGENTS.md",
        )
    )
    assert secret not in generated
    assert "worker-a" in generated
    assert "ticket_review" in generated
    assert "never call ticket_review" in generated


def test_door_setup_writes_private_state_and_secret_free_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = seat_new.build_parser().parse_args(
        [
            "--role", "worker",
            "--name", "worker-door",
            "--dest", str(tmp_path / "worker"),
            "--door", synthetic_door(),
            "--client", "codex",
        ]
    )
    dest = seat_new.generate(parsed)
    state = dest / ".pursers" / "wait-bridge" / "doors.json"
    shell = (dest / "bin" / "board.sh").read_text(encoding="utf-8")
    assert state.is_file()
    assert mode(state) == 0o600
    assert "TOKEN" not in shell
    assert "SSL_CERT_FILE" not in shell
    assert "PURSERS_CA_FILE" not in shell
    assert "PURSERS_BRIDGE_STATE_DIR" in shell
    generated = load_generated(dest / "bin" / "board.py", "board_door_state")
    for name in tuple(os.environ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PURSERS_BRIDGE_STATE_DIR", str(state.parent))
    generated._load_stored_door_environment()
    assert os.environ["ONBOARD_BOARD_ID"] == "sandbox"
    assert os.environ["PURSERS_ROLE"] == "worker"


def test_door_is_mutually_exclusive_and_role_checked(tmp_path: Path) -> None:
    parsed = seat_new.build_parser().parse_args(
        [
            "--role", "worker", "--name", "worker-door",
            "--dest", str(tmp_path / "mixed"), "--door", synthetic_door(),
            "--central-url", "http://127.0.0.1:8766/mcp",
        ]
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        seat_new.generate(parsed)
    wrong_role = seat_new.build_parser().parse_args(
        [
            "--role", "reviewer", "--name", "reviewer-door",
            "--dest", str(tmp_path / "wrong"), "--door", synthetic_door(),
        ]
    )
    with pytest.raises(ValueError, match="role must match"):
        seat_new.generate(wrong_role)


def test_worker_and_reviewer_variants_have_only_their_commands(tmp_path: Path) -> None:
    worker = seat_new.generate(args(tmp_path, role="worker"))
    reviewer = seat_new.generate(args(tmp_path, role="reviewer"))

    worker_py = (worker / "bin" / "board.py").read_text(encoding="utf-8")
    reviewer_py = (reviewer / "bin" / "board.py").read_text(encoding="utf-8")
    assert "ROLE = 'worker'" in worker_py
    assert 'commands.add_parser("claim")' in worker_py
    assert "ROLE = 'reviewer'" in reviewer_py
    assert 'commands.add_parser("approve")' in reviewer_py
    assert 'commands.add_parser("review-claim")' in reviewer_py
    assert 'claimed = await target.ticket_review_claim(args.ticket_id)' in reviewer_py
    assert 'if not claimed.get("ok")' in reviewer_py
    assert "reviewers never work-claim/submit/write code/push" in (
        reviewer / "AGENTS.md"
    ).read_text()
    assert seat_new.HARD_VERIFY_CHECKLIST in (reviewer / "AGENTS.md").read_text()
    assert seat_new.HARD_VERIFY_CHECKLIST not in (worker / "AGENTS.md").read_text()
    assert 'commands.add_parser("verify")' in reviewer_py


def test_generator_writes_dispatch_capabilities_and_offer_guidance(tmp_path: Path) -> None:
    parsed = args(tmp_path, role="worker", client="codex")
    parsed.tier_max = 1
    parsed.skills = "python, docs,python"
    parsed.can_review = False
    parsed.model = "gpt-test"
    parsed.provider = "openai"
    dest = seat_new.generate(parsed)

    shell = (dest / "bin" / "board.sh").read_text(encoding="utf-8")
    guidance = (dest / "AGENTS.md").read_text(encoding="utf-8")
    generated_py = (dest / "bin" / "board.py").read_text(encoding="utf-8")
    assert "export PURSERS_TIER_MAX=1" in shell
    assert "export PURSERS_SKILLS=docs,python" in shell
    assert "export PURSERS_CAN_REVIEW=false" in shell
    assert "export PURSERS_CAN_WORK=true" in shell
    assert "export PURSERS_HOST=codex" in shell
    assert "export PURSERS_MODEL=gpt-test" in shell
    assert "export PURSERS_PROVIDER=openai" in shell
    assert str(tmp_path / "ca.pem") not in shell
    assert 'if [ -n "${PURSERS_CA_FILE:-}" ]' in shell
    assert 'export SSL_CERT_FILE="$PURSERS_CA_FILE"' in shell
    assert "A work broadcast is also claimable" in guidance
    assert "Never claim a ticket offered to another seat" in guidance
    assert "reason=held_ticket_update" in guidance
    assert "If every selected board is skipped, stop and report" in guidance
    assert "never re-arm blind" in guidance
    assert "this ticket was offered to another seat; wait for your own offer" in generated_py


def test_reviewer_checklist_matches_both_generated_hints_and_docs(tmp_path: Path) -> None:
    reviewer = seat_new.generate(args(tmp_path, role="reviewer"))
    checklist = seat_new.HARD_VERIFY_CHECKLIST

    assert checklist in (reviewer / "AGENTS.md").read_text(encoding="utf-8")
    assert checklist in (reviewer / ".goosehints").read_text(encoding="utf-8")
    for name in ("manual-en.html", "manual-th.html"):
        rendered_source = html.unescape(
            (ROOT.parents[1] / "docs-local" / name).read_text(encoding="utf-8")
        )
        assert checklist in rendered_source


def test_generated_directives_explain_holder_wakes_and_claimable_broadcasts(
    tmp_path: Path,
) -> None:
    worker = seat_new.generate(args(tmp_path / "worker", role="worker"))
    reviewer = seat_new.generate(args(tmp_path / "reviewer", role="reviewer"))

    for name in ("AGENTS.md", ".goosehints"):
        worker_text = (worker / name).read_text(encoding="utf-8")
        reviewer_text = (reviewer / name).read_text(encoding="utf-8")
        assert "reason=held_ticket_update" in worker_text
        assert "fix and resubmit a rejection" in worker_text
        assert "dispatch_state.state=broadcast" in worker_text
        assert "Never claim a ticket offered to another seat" in worker_text
        assert "A review broadcast is also claimable" in reviewer_text
        assert "Never claim a review offered to another reviewer" in reviewer_text


def test_approve_evidence_gate_refuses_before_any_board_call(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, role="reviewer"))
    generated = load_generated(dest / "bin" / "board.py", "board_approve_gate")
    loaded = False

    def load_client():
        nonlocal loaded
        loaded = True
        raise AssertionError("board client must not load")

    generated._load_client = load_client
    parsed = generated._parser().parse_args(
        ["approve", "TK-review", "looks good"]
    )

    with pytest.raises(ValueError, match="approve evidence missing"):
        asyncio.run(generated._execute(parsed))
    assert loaded is False


@pytest.mark.parametrize(
    "test_tail",
    [
        "1 failed, 1 passed in 0.10s",
        "Ran 2 tests in 0.01s\n\nFAILED (failures=1)",
        "0 passed in 0.01s",
        "no tests ran in 0.01s",
        "collected 0 items",
        "ERROR collecting test_sample.py",
        "test_sample.py::test_ok ERROR at setup",
        "1 error in 0.01s",
        "Ran 1 test in 0.01s\n\nFAILED (failures=1)",
        "!!!!!!!!!!!!!!!! Interrupted !!!!!!!!!!!!!!!!",
    ],
)
def test_approve_rejects_unsuccessful_test_tails_before_board_access(
    tmp_path: Path, test_tail: str
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_approve_bad_tail",
    )
    loaded = False

    def load_client():
        nonlocal loaded
        loaded = True
        raise AssertionError("board client must not load")

    generated._load_client = load_client
    notes = "\n".join(
        ["sha: " + "a" * 40, test_tail, "leak-scan: clean", "model: gpt-5"]
    )
    parsed = generated._parser().parse_args(["approve", "TK-review", notes])

    with pytest.raises(ValueError, match="approve evidence missing: pytest/unittest tail"):
        asyncio.run(generated._execute(parsed))
    assert loaded is False


def test_approve_gate_accepts_evidence_and_logs_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = seat_new.generate(args(tmp_path, role="reviewer"))
    generated = load_generated(dest / "bin" / "board.py", "board_approve_evidence")
    valid = "\n".join(
        [
            "sha: " + "a" * 40,
            "12 passed in 1.23s",
            "leak-scan: clean",
            "model: gpt-5",
        ]
    )

    assert generated._approve_notes(valid, False) == valid
    with pytest.raises(ValueError, match=generated.APPROVE_OVERRIDE_ENV):
        generated._approve_notes("insufficient", True)
    with pytest.raises(ValueError, match=generated.APPROVE_OVERRIDE_ENV):
        generated._approve_notes(valid, True)
    monkeypatch.setenv(generated.APPROVE_OVERRIDE_ENV, "1")
    forced = generated._approve_notes("insufficient", True)
    assert "force-approve-without-evidence: operator override" in forced
    assert generated.APPROVE_OVERRIDE_ENV + "=1" in forced
    assert "force-approve-without-evidence: operator override" in generated._approve_notes(
        valid, True
    )

    unittest_valid = "\n".join(
        [
            "sha: " + "b" * 40,
            "Ran 3 tests in 0.01s",
            "",
            "OK",
            "leak-scan: clean",
            "model: gpt-5",
        ]
    )
    assert generated._approve_notes(unittest_valid, False) == unittest_valid


def test_reject_requires_nonempty_fix_before_any_board_call(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, role="reviewer"))
    generated = load_generated(dest / "bin" / "board.py", "board_reject_gate")
    loaded = False

    def load_client():
        nonlocal loaded
        loaded = True
        raise AssertionError("board client must not load")

    generated._load_client = load_client
    parsed = generated._parser().parse_args(["reject", "TK-review", "bad", " "])

    with pytest.raises(ValueError, match="fix_instructions must be non-empty"):
        asyncio.run(generated._execute(parsed))
    assert loaded is False


@pytest.mark.parametrize(
    "platform", ["codex", "goose", "claude", "api", "vendor", "generic"]
)
def test_verify_detaches_sha_checks_scope_origin_leaks_and_runs_suite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str
) -> None:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    clone = tmp_path / "seat-clone"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(author)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seat Test"], cwd=author, check=True)
    subprocess.run(["git", "config", "user.email", "seat@example.test"], cwd=author, check=True)
    (author / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=author, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=author, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=author, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=author, check=True, capture_output=True)
    branch = f"{platform}/TK-review"
    subprocess.run(["git", "switch", "-c", branch], cwd=author, check=True, capture_output=True)
    (author / "change.txt").write_text("verified change\n", encoding="utf-8")
    (author / "test_sample.py").write_text(
        "import unittest\n\nclass Sample(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "change.txt", "test_sample.py"], cwd=author, check=True)
    subprocess.run(["git", "commit", "-m", "review target"], cwd=author, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=author, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=author, check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer")) / "bin" / "board.py",
        f"board_verify_{platform}",
    )
    ticket = {
        "ticket_id": "TK-review",
        "target_url": "sample/path",
        "required_fields": ["branch_and_commit", "test_output"],
        "tests": [
            "test-command: python3 -m unittest discover -s . -p test_sample.py",
            "test-command: PYTHONPATH=. pytest -q test_sample.py",
        ],
        "submission_history": [
            {
                "files_changed": ["change.txt", "test_sample.py"],
                "notes": f"branch_and_commit: {branch} @ {sha}",
            }
        ],
    }

    result = generated._verify_ticket(ticket, clone, run_suites=True)

    output = capsys.readouterr().out
    assert result["sha"] == sha
    assert result["files_changed_match"] is True
    assert result["origin_main_contains"] is False
    assert result["leak_scan"] == "clean"
    assert result["suites"][0]["returncode"] == 0
    assert result["suites"][1]["returncode"] == 0
    assert "files-changed-diff:" in output
    assert "remote-branches-containing-sha:" in output
    assert "Ran 1 test" in output
    assert "OK" in output
    assert "operator-markers-loaded: 0" in output
    assert "leak-scan: clean" in output
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == sha


def _review_verification_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Any, dict[str, Any], str]:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    clone = tmp_path / "seat-clone"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(author)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seat Test"], cwd=author, check=True)
    subprocess.run(["git", "config", "user.email", "seat@example.test"], cwd=author, check=True)
    (author / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=author, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=author, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=author, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=author, check=True, capture_output=True)
    branch = "codex/TK-review"
    subprocess.run(["git", "switch", "-c", branch], cwd=author, check=True, capture_output=True)
    (author / "change.txt").write_text("review target\n", encoding="utf-8")
    subprocess.run(["git", "add", "change.txt"], cwd=author, check=True)
    subprocess.run(["git", "commit", "-m", "review target"], cwd=author, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=author, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=author, check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer")) / "bin" / "board.py",
        "board_verify_remote_heads",
    )
    ticket = {
        "ticket_id": "TK-review",
        "target_url": "sample/path",
        "submission_history": [
            {
                "files_changed": ["change.txt"],
                "notes": f"branch_and_commit: {branch} @ {sha}",
            }
        ],
    }
    return author, clone, generated, ticket, sha


def test_routed_verify_uses_and_cleans_reviewer_owned_clone_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _author, routed, generated, ticket, sha = _review_verification_fixture(tmp_path)
    from pursers_client import (
        parse_project_registry,
        registry_project_work_dirs,
        registry_work_dirs,
        resolve_registry_target,
    )

    ticket["target_url"] = "https://example.test/acme/sample"
    registry = {
        "schema_version": 1,
        "projects": {
            "sample": {
                "board_id": "pursers",
                "work_dir": str(routed),
                "work_dir_owner": "fleet",
                "repository_url": ticket["target_url"],
                "status": "active",
            }
        },
    }
    subprocess.run(
        ["git", "switch", "main"], cwd=routed, check=True, capture_output=True,
    )
    (routed / "base.txt").write_text("local routed change\n", encoding="utf-8")
    (routed / "local-only.txt").write_text("keep me\n", encoding="utf-8")

    def git_output(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=routed, check=True,
            capture_output=True, text=True,
        ).stdout

    original_branch = git_output("branch", "--show-current")
    original_head = git_output("rev-parse", "HEAD")
    original_status = git_output("status", "--porcelain=v1")
    original_files = {
        name: (routed / name).read_text(encoding="utf-8")
        for name in ("base.txt", "local-only.txt")
    }
    seat_root = Path(generated.__file__).resolve().parents[1]
    verified_paths: list[Path] = []
    verify_ticket = generated._verify_ticket

    def track_verification(ticket_value, repo, *, run_suites=False):
        verified_paths.append(repo)
        assert repo != routed
        assert repo.is_relative_to(seat_root)
        return verify_ticket(ticket_value, repo, run_suites=run_suites)

    generated._verify_ticket = track_verification

    class ReviewClient:
        identity = SimpleNamespace(agent_id="AI-reviewer")

        def __init__(self, *_arguments, **_keywords) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_arguments) -> None:
            pass

        async def board_join(self, **kwargs):
            assert kwargs["allow_takeover"] is True
            return {"ok": True}

        async def board_state_get(self, *, key):
            return {"state": {"key": key, "value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id):
            assert ticket_id == ticket["ticket_id"]
            return {"ok": True, "ticket": ticket}

    generated._load_client = lambda: (
        ReviewClient,
        frozenset(),
        "project_registry",
        frozenset({"ticket_submitted"}),
        lambda _registry, _home: ["pursers"],
        parse_project_registry,
        registry_project_work_dirs,
        registry_work_dirs,
        resolve_registry_target,
        None,
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "https://central.example/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "reviewer-a")

    asyncio.run(generated._execute(generated._parser().parse_args(["verify", "TK-review"])))

    output = capsys.readouterr().out
    assert f"verified-sha: {sha}" in output
    assert verified_paths and all(not path.exists() for path in verified_paths)
    assert list(seat_root.glob(".verify-*")) == []
    assert git_output("branch", "--show-current") == original_branch
    assert git_output("rev-parse", "HEAD") == original_head
    assert git_output("status", "--porcelain=v1") == original_status
    assert {
        name: (routed / name).read_text(encoding="utf-8")
        for name in original_files
    } == original_files


@pytest.mark.parametrize(
    ("remote_branch", "error"),
    [
        ("main", "already on origin/main"),
        ("reviewer-copy", "other remote branches: origin/reviewer-copy"),
    ],
)
def test_verify_refreshes_all_remote_heads_before_containment_checks(
    tmp_path: Path, remote_branch: str, error: str
) -> None:
    author, clone, generated, ticket, sha = _review_verification_fixture(tmp_path)
    subprocess.run(
        ["git", "push", "origin", f"{sha}:refs/heads/{remote_branch}"],
        cwd=author, check=True, capture_output=True,
    )

    with pytest.raises(ValueError, match=error):
        generated._verify_ticket(ticket, clone)

    assert subprocess.run(
        ["git", "rev-parse", f"origin/{remote_branch}"], cwd=clone,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == sha


def test_submission_rejects_invalid_git_ref(tmp_path: Path) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_verify_invalid_ref",
    )
    ticket = {
        "submission_history": [{
            "notes": "branch_and_commit: goose/TK-review.lock @ " + "a" * 40,
        }]
    }

    with pytest.raises(ValueError, match="valid platform/branch"):
        generated._submission(ticket)


@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        ("home-directory-path", "path=/Users/" + "fixture-user/project"),
        ("home-directory-path", "path=/home/" + "fixture-user/project"),
        ("home-directory-path", "path=C:\\Users\\" + "fixture-user" + "\\project"),
        ("bearer-token", "Authorization: " + "Bearer" + " " + "A" * 24),
        ("api-key", "api_key=" + "Z" * 24),
        ("api-key", "OPENAI_API_KEY=" + "Z" * 24),
        ("api-key", "AWS_ACCESS_KEY_ID=" + "Z" * 24),
        ("api-key", "MY_CLIENT_SECRET=" + "Z" * 24),
        ("private-key", "-----BEGIN " + "PRIVATE" + " KEY-----"),
    ],
)
def test_verify_leak_rules_cover_mandatory_categories(
    tmp_path: Path, rule: str, sample: str
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_verify_leak_" + rule.replace("-", "_"),
    )

    assert rule in generated._leak_rule_names(sample)


def test_verify_leak_rules_allow_documented_synthetic_fixtures(tmp_path: Path) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_verify_synthetic_leaks",
    )
    fixtures = "\n".join(
        [
            "Authorization: " + "Bearer" + " synthetic-local-bearer",
            "api_key=placeholder_value",
            "/Users/" + "synthetic-user/project",
            "documented `/Users/" + "synthetic-user` fixture",
            "/home/" + "synthetic-user/project",
            "C:\\Users\\" + "synthetic-user\\project",
            "https://example.com/path",
            "http://127.0.0.1:8080/healthz",
            "-----BEGIN SYNTHETIC " + "PRIVATE" + " KEY-----",
            "ey" + "J.synthetic.fixture",
        ]
    )

    assert generated._leak_rule_names(fixtures) == []


def test_verify_leak_rules_ignore_jwt_vocabulary_and_fake_fixture(
    tmp_path: Path,
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_verify_jwt_vocabulary",
    )
    harmless = "\n".join(
        [
            "JWTs and jwt values must be redacted.",
            r'SENSITIVE_KEY = re.compile(r"(?:authorization|bearer|jwt|token)")',
            "fake_fixture=" + "ey" + "Jabc.def.ghi",
        ]
    )

    assert generated._leak_rule_names(harmless) == []


def test_verify_leak_rules_detect_runtime_constructed_jwt_shape(tmp_path: Path) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_verify_jwt_shape",
    )

    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    token_shape = ".".join(
        [
            segment({"alg": "HS256", "typ": "JWT"}),
            segment({"sub": "fixture"}),
            segment("sig"),
        ]
    )

    assert "jwt" in generated._leak_rule_names("token=" + token_shape)


def test_suite_commands_allow_bounded_pythonpath_assignments(tmp_path: Path) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_suite_pythonpath",
    )
    repo = tmp_path / "repo"
    (repo / "packages" / "client" / "src").mkdir(parents=True)
    (repo / "packages" / "client" / "tests").mkdir(parents=True)
    (repo / "reports").mkdir()
    ticket = {
        "tests": [
            "test-command: PYTHONPATH=packages/client/src pytest -q packages/client/tests",
            "suite: PYTHONPATH=packages/client/src python3 -m unittest "
            "discover -s packages/client/tests",
            "suite: pytest -q packages/client/tests --junitxml=reports/results.xml "
            "--basetemp=reports/pytest-tmp",
        ]
    }

    commands = generated._suite_commands(ticket, {}, repo)

    assert [command["argv"] for command in commands] == [
        ["pytest", "-q", "packages/client/tests"],
        ["python3", "-m", "unittest", "discover", "-s", "packages/client/tests"],
        [
            "pytest", "-q", "packages/client/tests",
            "--junitxml=reports/results.xml", "--basetemp=reports/pytest-tmp",
        ],
    ]
    assert [command["pythonpath"] for command in commands] == [
        "packages/client/src",
        "packages/client/src",
        "",
    ]

    environment = generated._suite_environment(commands[0])
    assert environment["PYTHONPATH"] == "packages/client/src"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("OTHER=value pytest -q tests", "only PYTHONPATH"),
        ("PYTHONPATH=/outside pytest -q tests", "inside the worktree"),
        ("PYTHONPATH=../outside pytest -q tests", "inside the worktree"),
        ("pytest -q ../outside", "suite paths must stay inside"),
        ("pytest -q $(command)", "shell substitutions"),
        ("pytest -q tests; command", "shell substitutions"),
        ("pytest -q tests > output", "shell substitutions"),
        ("env PYTHONPATH=src pytest -q tests", "arbitrary environment"),
        ("pytest --pyargs pip", "module, plugin, and config escape"),
        ("pytest -p pip -q tests", "module, plugin, and config escape"),
        ("pytest -c pytest.ini -q tests", "module, plugin, and config escape"),
        ("pytest -c../outside -q tests", "module, plugin, and config escape"),
        ("pytest --unknown-option tests", "option is not allow-listed"),
        ("pytest --basetemp=../outside tests", "suite paths must stay inside"),
        ("python3 -m unittest pip", "unittest replay requires discover"),
        (
            "python3 -m unittest discover -s../outside",
            "discovery paths must use separate",
        ),
    ],
)
def test_suite_commands_reject_unsafe_evidence(
    tmp_path: Path, command: str, error: str
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path, role="reviewer")) / "bin" / "board.py",
        "board_suite_reject_" + str(abs(hash(command))),
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match=error):
        generated._suite_commands({"tests": ["test-command: " + command]}, {}, repo)


@pytest.mark.parametrize(
    "payload",
    [
        "/OUTSIDE/test_probe.py",
        "--pyargs pip",
        "-p external_plugin",
        "-c /OUTSIDE/pytest.ini",
        "--rootdir=/OUTSIDE",
        "--confcutdir=/OUTSIDE",
        "-o python_files=outside.py",
    ],
)
def test_suite_commands_reject_pytest_argument_files_before_expansion(
    tmp_path: Path, payload: str
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer"))
        / "bin"
        / "board.py",
        "board_suite_argument_files_" + re.sub(r"\W+", "_", payload),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    relative = repo / "args.txt"
    relative.write_text(payload + "\n", encoding="utf-8")
    outside = tmp_path / "outside-args.txt"
    outside.write_text(payload + "\n", encoding="utf-8")

    for command in ("pytest @args.txt", f"python3 -m pytest @{outside}"):
        with pytest.raises(ValueError, match="pytest argument files"):
            generated._suite_commands(
                {"tests": ["test-command: " + command]}, {}, repo
            )


def test_suite_commands_reject_nonexistent_output_through_symlink_ancestor(
    tmp_path: Path,
) -> None:
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer"))
        / "bin"
        / "board.py",
        "board_suite_symlink_output",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "tests").mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="suite paths must stay inside"):
        generated._suite_commands(
            {
                "tests": [
                    "test-command: pytest -q tests --junitxml=link/report.xml"
                ]
            },
            {},
            repo,
        )

    assert not (outside / "report.xml").exists()


def test_verify_suite_replay_discards_inherited_execution_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, clone, generated, ticket, _old_sha = _review_verification_fixture(tmp_path)
    branch = "codex/TK-review"
    (author / "test_safe.py").write_text(
        "import unittest\n\n"
        "class SafeTest(unittest.TestCase):\n"
        "    def test_inside_worktree(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_safe.py"], cwd=author, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add safe suite"],
        cwd=author, check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=author, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "origin", branch],
        cwd=author, check=True, capture_output=True,
    )
    ticket["tests"] = [
        "test-command: pytest -q test_safe.py",
        "test-command: python3 -m unittest discover -s . -p test_safe.py",
    ]
    ticket["submission_history"] = [{
        "files_changed": ["test_safe.py"],
        "notes": f"branch_and_commit: {branch} @ {sha}",
    }]

    outside = tmp_path / "outside"
    outside.mkdir()
    startup_marker = outside / "startup-ran"
    plugin_marker = outside / "plugin-ran"
    module_marker = outside / "module-ran"
    (outside / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(startup_marker)!r}).write_text('outside startup')\n",
        encoding="utf-8",
    )
    (outside / "injected_plugin.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(plugin_marker)!r}).write_text('outside plugin')\n",
        encoding="utf-8",
    )
    package = outside / "injectedpkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "test_outside.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(module_marker)!r}).write_text('outside module')\n"
        "def test_outside():\n    assert True\n",
        encoding="utf-8",
    )
    argument_file = outside / "args.txt"
    argument_file.write_text("--pyargs injectedpkg\n", encoding="utf-8")
    config_file = outside / "pytest.ini"
    config_file.write_text("[pytest]\naddopts = --pyargs injectedpkg\n", encoding="utf-8")

    monkeypatch.setenv("PYTHONPATH", str(outside))
    monkeypatch.setenv("PYTHONHOME", str(outside))
    monkeypatch.setenv("PYTHONSTARTUP", str(outside / "sitecustomize.py"))
    monkeypatch.setenv("PYTEST_PLUGINS", "injected_plugin")
    monkeypatch.setenv("PYTEST_ADDOPTS", "@" + str(argument_file))
    clean_environment = generated._suite_environment({"pythonpath": ""})
    assert not {
        "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
        "PYTEST_PLUGINS", "PYTEST_ADDOPTS",
    } & clean_environment.keys()
    first = generated._verify_ticket(ticket, clone, run_suites=True)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-c " + str(config_file))
    second = generated._verify_ticket(ticket, clone, run_suites=True)

    assert [suite["returncode"] for suite in first["suites"]] == [0, 0]
    assert [suite["returncode"] for suite in second["suites"]] == [0, 0]
    assert not startup_marker.exists()
    assert not plugin_marker.exists()
    assert not module_marker.exists()
    assert not list((clone / ".git").glob("pursers-verify-*.ini"))


def test_operator_marker_file_is_loaded_without_printing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "zz-" + "fixture-identity"
    marker_file = tmp_path / "custom-markers.txt"
    marker_file.write_text(re.escape(marker) + "\n", encoding="utf-8")
    monkeypatch.setenv("PURSERS_LEAK_MARKERS_FILE", str(marker_file))
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer")) / "bin" / "board.py",
        "board_operator_markers",
    )

    rules, count = generated._leak_scan("owner=" + marker)
    notes = "\n".join([
        "sha: " + "a" * 40,
        "1 passed in 0.01s",
        "leak-scan: clean",
        "model: fixture-model",
    ])
    assert generated._approve_notes(notes, False) == notes

    output = capsys.readouterr().out
    assert rules == ["operator-marker"]
    assert count == 1
    assert "operator-markers-loaded: 1" in output
    assert marker not in output


def test_operator_marker_regex_error_never_echoes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "[" + "zz-secret-fixture"
    marker_file = tmp_path / "invalid-markers.txt"
    marker_file.write_text(marker + "\n", encoding="utf-8")
    monkeypatch.setenv("PURSERS_LEAK_MARKERS_FILE", str(marker_file))
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer")) / "bin" / "board.py",
        "board_invalid_operator_marker",
    )

    with pytest.raises(ValueError) as error:
        generated._leak_scan("safe text")
    assert marker not in str(error.value)
    assert ":1" in str(error.value)


def test_seat_kit_repo_text_obeys_operator_marker_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker_regex = "zz-" + "[a-z]+" + "-identity"
    marker_file = tmp_path / "repo-invariant-markers.txt"
    marker_file.write_text(marker_regex + "\n", encoding="utf-8")
    monkeypatch.setenv("PURSERS_LEAK_MARKERS_FILE", str(marker_file))
    generated = load_generated(
        seat_new.generate(args(tmp_path / "seat", role="reviewer")) / "bin" / "board.py",
        "board_repo_marker_invariant",
    )
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".py", ".sh"}
    )

    rules, count = generated._leak_scan(source_text)
    assert set(generated.LEAK_PATTERNS) == {
        "api-key", "bearer-token", "home-directory-path", "jwt", "private-key",
    }
    assert generated.DEFAULT_LEAK_MARKERS_FILE == "~/.pursers/leak-markers.txt"
    assert count == 1
    assert "operator-marker" not in rules


def test_repo_clone_uses_repo_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "clone"]:
            calls.append(command)
            Path(command[-1]).mkdir()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(seat_new.subprocess, "run", fake_run)
    dest = seat_new.generate(args(tmp_path, repo="https://example.test/acme/Pursers.git"))

    assert calls == [
        [
            "git",
            "clone",
            "--",
            "https://example.test/acme/Pursers.git",
            str(dest / "Pursers"),
        ]
    ]
    assert "REPO_LEAF = 'Pursers'" in (dest / "bin" / "board.py").read_text()


def test_board_sh_missing_token_fails_cleanly_without_network(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path))

    result = subprocess.run(
        [str(dest / "bin" / "board.sh"), "list"],
        cwd=dest,
        text=True,
        capture_output=True,
        env={**os.environ, "PURSERS_TOKEN_FILE": str(tmp_path / "missing.jwt")},
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("board.sh: token file is not readable:")
    assert "Traceback" not in result.stderr


def test_blank_board_serves_registry_and_named_board_is_dedicated(tmp_path: Path) -> None:
    fleet = seat_new.generate(args(tmp_path))
    shell = (fleet / "bin" / "board.sh").read_text()
    # blank --board: bind to the registry board, wait on every active project
    assert "export ONBOARD_BOARD_ID=${PURSERS_BOARD:-pursers}" in shell
    assert "export PURSERS_BOARDS=${PURSERS_BOARDS:-registry}" in shell
    assert 'os.environ.get("PURSERS_BOARDS")' in (fleet / "bin" / "board.py").read_text()

    parsed = args(tmp_path, role="reviewer")
    parsed.board = "fullplatts"
    dedicated = seat_new.generate(parsed)
    shell = (dedicated / "bin" / "board.sh").read_text()
    assert "export ONBOARD_BOARD_ID=${PURSERS_BOARD:-fullplatts}" in shell
    assert "export PURSERS_BOARDS=${PURSERS_BOARDS:-home}" in shell

    bad = args(tmp_path)
    bad.dest = str(tmp_path / "bad")
    bad.board = "not a board id"
    with pytest.raises(ValueError, match="--board"):
        seat_new.generate(bad)


def test_nonempty_destination_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "worker"
    dest.mkdir()
    (dest / "keep.txt").write_text("owned by user", encoding="utf-8")

    with pytest.raises(ValueError, match="not empty"):
        seat_new.generate(args(tmp_path))

    assert (dest / "keep.txt").read_text() == "owned by user"


def test_upgrade_regenerates_managed_files_and_preserves_existing_content(
    tmp_path: Path,
) -> None:
    parsed = args(tmp_path)
    parsed.python = sys.executable
    dest = seat_new.generate(parsed)
    (dest / "keep.txt").write_text("operator-owned", encoding="utf-8")
    (dest / "bin/board.sh").write_text("stale", encoding="utf-8")
    parsed.upgrade = True

    seat_new.generate(parsed)

    assert (dest / "keep.txt").read_text() == "operator-owned"
    assert "ONBOARD_AGENT_NAME" in (dest / "bin/board.sh").read_text()
    assert str(Path(sys.executable).expanduser()) in (dest / "bin/board.sh").read_text()


def test_venv_interpreter_symlink_is_preserved(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime with spaces"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=True,
    )
    interpreter = runtime / "bin" / "python"
    assert interpreter.is_symlink()
    parsed = args(tmp_path / "seat")
    parsed.python = str(interpreter)

    dest = seat_new.generate(parsed)

    shell = (dest / "bin" / "board.sh").read_text(encoding="utf-8")
    assert f"exec {seat_new.shlex.quote(str(interpreter))} " in shell
    assert f"exec {seat_new.shlex.quote(str(interpreter.resolve()))} " not in shell


@pytest.mark.real_interpreter_check
def test_bare_interpreter_without_dependencies_is_rejected(tmp_path: Path) -> None:
    interpreter = tmp_path / "bare-python"
    interpreter.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 7; fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    parsed = args(tmp_path / "seat")
    parsed.python = str(interpreter)

    with pytest.raises(ValueError, match="bare Python interpreter lacks required"):
        seat_new.generate(parsed)


def test_upgrade_preserves_existing_interpreter_when_python_omitted(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime with spaces"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=True,
    )
    interpreter = runtime / "bin" / "python"
    initial = args(tmp_path / "seat")
    initial.python = str(interpreter)
    dest = seat_new.generate(initial)

    upgrade = args(tmp_path / "seat")
    upgrade.upgrade = True
    assert upgrade.python is None
    seat_new.generate(upgrade)

    assert f"exec {seat_new.shlex.quote(str(interpreter))} " in (
        dest / "bin" / "board.sh"
    ).read_text()


def test_upgrade_fast_forwards_existing_clean_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = args(tmp_path, repo="https://example.test/Pursers.git")
    parsed.upgrade = True
    clone = Path(parsed.dest) / "Pursers"
    clone.mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        if command[0] != "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        calls.append((command, kwargs.get("cwd")))
        stdout = ""
        if command[:5] == ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]:
            stdout = "main\n"
        elif command[:5] == [
            "git",
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ]:
            stdout = "origin/main\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(seat_new.subprocess, "run", run)
    seat_new.generate(parsed)

    assert (["git", "fetch", "origin"], clone) in calls
    assert (["git", "status", "--porcelain"], clone) in calls
    assert (["git", "merge", "--ff-only", "origin/main"], clone) in calls


@pytest.mark.parametrize(
    ("current_branch", "ancestor_returncode", "warning"),
    [
        ("ticket-work", 0, "not default branch main"),
        ("main", 1, "not fast-forwardable to origin/main"),
    ],
)
def test_upgrade_warns_and_leaves_non_default_or_non_ff_clone_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_branch: str,
    ancestor_returncode: int,
    warning: str,
) -> None:
    parsed = args(tmp_path, repo="https://example.test/Pursers.git")
    parsed.upgrade = True
    clone = Path(parsed.dest) / "Pursers"
    clone.mkdir(parents=True)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        if command[0] != "git":
            return subprocess.CompletedProcess(command, 0, "", "")
        calls.append(command)
        if command[-1] == "refs/remotes/origin/HEAD":
            return subprocess.CompletedProcess(command, 0, "origin/main\n", "")
        if command[-1] == "HEAD" and command[1] == "symbolic-ref":
            return subprocess.CompletedProcess(command, 0, f"{current_branch}\n", "")
        if command[1] == "merge-base":
            return subprocess.CompletedProcess(command, ancestor_returncode, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(seat_new.subprocess, "run", run)
    dest = seat_new.generate(parsed)

    assert (dest / "bin" / "board.sh").is_file()
    assert ["git", "fetch", "origin"] in calls
    assert not any(command[1:3] == ["merge", "--ff-only"] for command in calls)
    assert warning in capsys.readouterr().err


@pytest.mark.parametrize(
    ("client", "host_timeout", "wait_timeout"),
    [
        ("goose", 300, 270),
        ("codex", 620, 560),
        ("claude", 21_600, 21_540),
        ("generic", 180, 150),
    ],
)
def test_client_profile_renders_derived_wait_default(
    tmp_path: Path, client: str, host_timeout: int, wait_timeout: int
) -> None:
    dest = seat_new.generate(args(tmp_path / client, client=client))
    generated = load_generated(dest / "bin" / "board.py", f"board_{client}")
    parsed = generated._parser().parse_args(["wait"])
    instructions = (dest / "AGENTS.md").read_text(encoding="utf-8")

    assert parsed.timeout == wait_timeout
    assert f"{host_timeout}s/{wait_timeout}s" in instructions
    assert "sleep 90-120" not in instructions
    assert "wait --poll" in instructions
    if client == "goose":
        assert "`timeout: 3600`" in instructions
        assert "`board.sh wait --timeout 3540`" in instructions


def test_goose_generator_prints_exact_timeout_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = seat_new.main(
        [
            "--role",
            "worker",
            "--name",
            "goose-worker",
            "--dest",
            str(tmp_path / "goose-worker"),
            "--central-url",
            "https://central.example/mcp",
            "--token-file",
            str(tmp_path / "seat.jwt"),
            "--client",
            "goose",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "config.yaml line: timeout: 3600" in output
    assert "board.sh wait --timeout 3540 --since <cursor>" in output


def test_generated_wait_requires_approved_pure_client_api(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_legacy")

    class LegacyClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        async def events(self, from_cursor=None, *, kinds=None):
            if False:
                yield None

    with pytest.raises(RuntimeError, match="approved pure subscription API"):
        asyncio.run(
            generated._cmd_wait(
                LegacyClient(), "pursers", 4, 1, poll_fallback=False
            )
        )


def test_polling_requires_explicit_flag_and_uses_pure_catchup(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_poll")

    class PollClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
            self.calls.append(arguments)
            return {
                "next_cursor": 8,
                "events": [
                    {
                        "seq": 8,
                        "kind": "ticket_created",
                        "ticket_id": "TK-ready",
                        "recipient_identities": ["AI-worker"],
                    }
                ],
            }

    client = PollClient()
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            generated._cmd_wait(
                client, "pursers", 7, 1, poll_fallback=True
            )
        )
    result = json.loads(output.getvalue())

    assert result["new_seq"] == 8
    assert result["timed_out"] is False
    assert client.calls == [{"cursor": 7, "limit": 50, "ack": False, "touch": False}]


def test_event_wait_closes_stream_before_printing_result(tmp_path: Path) -> None:
    dest = seat_new.generate(args(tmp_path, client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_close_event")

    class EventClient:
        identity = SimpleNamespace(agent_id="AI-worker")

        def __init__(self) -> None:
            self.closed = False

        async def events(
            self,
            from_cursor: int | None = None,
            *,
            only_mine: bool = True,
            kinds: frozenset[str] | None = None,
            resource_subscriptions: tuple[str, ...] | None = None,
            acknowledge: bool = True,
            touch: bool | None = None,
            cursor_callback: Any = None,
        ):
            try:
                yield {
                    "id": "EV-ready",
                    "seq": 8,
                    "kind": "ticket_created",
                    "ticket_id": "TK-ready",
                }
            finally:
                self.closed = True

    client = EventClient()
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            generated._cmd_wait(
                client, "pursers", 7, 1, poll_fallback=False
            )
        )
    result = json.loads(output.getvalue())

    assert client.closed is True
    assert result["new_seq"] == 8
    assert result["timed_out"] is False
    assert [event["id"] for event in result["events"]] == ["EV-ready"]


async def build_local_central(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pursers_central import central

    tmp_path.mkdir(parents=True, exist_ok=True)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks_path))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, service = central.build_server("localhost", 8765, tmp_path / "data")
    principals = {
        "admin": central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        ),
        "worker": central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
        ),
        "reviewer": central.Principal(
            "PR-reviewer",
            "reviewer-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        ),
    }
    class PrincipalSelection:
        def __init__(self, principal: Any) -> None:
            self._principal = contextvars.ContextVar(
                "seat_kit_test_principal", default=principal
            )

        def __getitem__(self, key: str) -> Any:
            assert key == "principal"
            return self._principal.get()

        def __setitem__(self, key: str, principal: Any) -> None:
            assert key == "principal"
            self._principal.set(principal)

        def set(self, principal: Any) -> contextvars.Token[Any]:
            return self._principal.set(principal)

        def reset(self, token: contextvars.Token[Any]) -> None:
            self._principal.reset(token)

    active = PrincipalSelection(principals["admin"])
    original_current_principal = central.current_principal
    central.current_principal = lambda: active["principal"]

    async def call(name: str, **arguments: Any) -> Any:
        return await mcp.call_tool(name, {"board_id": "pursers", **arguments})

    joined = await call("board_join", agent_name="admin-agent")
    agent_ids = {"admin": joined.structured_content["agent_id"]}
    for key in ("worker", "reviewer"):
        await call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=principals[key].principal_id,
            role="reviewer" if key == "reviewer" else "member",
        )
        active["principal"] = principals[key]
        joined = await call("board_join", agent_name=f"{key}-agent")
        agent_ids[key] = joined.structured_content["agent_id"]
        active["principal"] = principals["admin"]
    return central, mcp, service, principals, active, agent_ids, call, original_current_principal


def test_live_registry_wait_restarts_stable_seat_and_delivers_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client
        from pursers_client import BoardClient, wait_for_boards
        import pursers_client.client as client_module
        import pursers_client.project_registry as registry_module

        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        other_board = "fullplatts"

        async def call_other(name: str, **arguments: Any) -> Any:
            return await mcp.call_tool(
                name, {"board_id": other_board, **arguments}
            )

        capabilities = {
            "tier_max": 3,
            "skills": [],
            "can_work": True,
            "can_review": False,
            "host": "test",
            "max_parallel": 1,
        }
        try:
            active["principal"] = principals["admin"]
            await call_other("board_join", agent_name="admin-agent")
            await call_other(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principals["worker"].principal_id,
                role="member",
            )
            active["principal"] = principals["worker"]
            await call_other(
                "board_join",
                agent_name="worker-agent",
                capabilities=capabilities,
            )
            cursors = {
                board_id: int(service.journal.read_after(board_id, 0)["latest_cursor"])
                for board_id in ("pursers", other_board)
            }

            @asynccontextmanager
            async def http_context():
                yield object()

            class LocalBoardClient(BoardClient):
                def _http(self):
                    return http_context()

            ready = asyncio.Event()

            class SignalingClient:
                def __init__(self, transport, **kwargs):
                    self.inner = Client(transport, **kwargs)

                async def __aenter__(self):
                    await self.inner.__aenter__()
                    return self

                async def __aexit__(self, *arguments):
                    return await self.inner.__aexit__(*arguments)

                async def call_tool(self, *arguments, **kwargs):
                    token = active.set(principals["worker"])
                    try:
                        return await self.inner.call_tool(*arguments, **kwargs)
                    finally:
                        active.reset(token)

                @asynccontextmanager
                async def listen(self, **kwargs):
                    async with self.inner.listen(**kwargs) as subscription:
                        async def signaled_subscription():
                            ready.set()
                            async for cue in subscription:
                                yield cue

                        yield signaled_subscription()

            monkeypatch.setattr(
                client_module, "streamable_http_client", lambda *_args, **_kwargs: mcp
            )
            monkeypatch.setattr(
                registry_module,
                "streamable_http_client",
                lambda *_args, **_kwargs: mcp,
            )
            monkeypatch.setattr(registry_module, "Client", SignalingClient)

            active["principal"] = principals["worker"]
            async with LocalBoardClient(
                "http://central.invalid/mcp",
                "test-token",
                "pursers",
                agent_name="worker-agent",
                role="worker",
                capabilities=capabilities,
                allow_takeover=True,
            ) as board_client:
                waiting = asyncio.create_task(wait_for_boards(
                    board_client,
                    ["pursers", other_board],
                    cursors,
                    3,
                    kinds=WORKER_WAIT_KINDS,
                    submitted=False,
                    work_dirs={
                        "pursers": "/repo/home",
                        other_board: "/repo/other",
                    },
                    capabilities=capabilities,
                    allow_takeover=True,
                ))
                await asyncio.wait_for(ready.wait(), timeout=1)
                active["principal"] = principals["admin"]
                created = await call(
                    "ticket_create",
                    agent_name="admin-agent",
                    title="registry restart offer probe",
                    description="prove restarted stable-seat registry delivery",
                    target_url="home/item",
                    scope="interactive-no-send",
                    required_fields=["test_output"],
                    assigned_to=agent_ids["worker"],
                )
                ticket_id = created.structured_content["ticket"]["ticket_id"]
                active["principal"] = principals["worker"]
                result = await asyncio.wait_for(waiting, timeout=2)

            assert result["boards"] == [other_board, "pursers"]
            assert result["skipped_boards"] == {}
            assert result["events"][0]["ticket_id"] == ticket_id
            assert result["events"][0]["kind"] == "ticket_offered"
            if os.environ.get("PURSERS_LIVE_PROBE_OUTPUT") == "1":
                print(json.dumps({
                    "boards": result["boards"],
                    "skipped_boards": result["skipped_boards"],
                    "events": [{
                        "kind": result["events"][0]["kind"],
                        "ticket_id": ticket_id,
                    }],
                }, sort_keys=True))
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_goose_generated_wait_60_second_idle_is_pure_and_rearms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client

        dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
        generated = load_generated(dest / "bin" / "board.py", "board_goose_idle")
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            _call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        try:
            active["principal"] = principals["worker"]
            cursor = int(service.journal.read_after("pursers", 0)["latest_cursor"])
            before = persisted_documents(service)
            async with Client(mcp, mode="2026-07-28", cache=None) as raw_client:
                adapter = LocalSubscriptionAdapter(
                    raw_client, service, agent_ids["worker"]
                )
                output = io.StringIO()
                started = time.monotonic()
                with redirect_stdout(output):
                    await generated._cmd_wait(
                        adapter, "pursers", cursor, 60, poll_fallback=False,
                        dispatch_kinds=WORKER_WAIT_KINDS,
                    )
                elapsed = time.monotonic() - started
                result = json.loads(output.getvalue())

                assert 59.5 <= elapsed < 65
                assert result["timed_out"] is True
                assert result["events"] == []
                assert result["new_seq"] == cursor
                assert adapter.ticket_list_calls == 0
                assert adapter.catchup_calls == 0
                assert adapter.events_calls == [
                    {
                        "from_cursor": cursor,
                        "only_mine": True,
                        "kinds": frozenset({"ticket_created"}) | WORKER_WAIT_KINDS,
                        "resource_subscriptions": (
                            "board://pursers/journal",
                            f"board://pursers/agent/{agent_ids['worker']}",
                        ),
                        "acknowledge": False,
                        "touch": False,
                    }
                ]
                assert persisted_documents(service) == before

                rearm_output = io.StringIO()
                with redirect_stdout(rearm_output):
                    await generated._cmd_wait(
                        adapter,
                        "pursers",
                        result["new_seq"],
                            1,
                            poll_fallback=False,
                            dispatch_kinds=WORKER_WAIT_KINDS,
                    )
                rearmed = json.loads(rearm_output.getvalue())
                assert rearmed["new_seq"] == result["new_seq"]
                assert rearmed["timed_out"] is True
                assert persisted_documents(service) == before
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_reviewer_wait_submitted_wakes_on_real_central_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client

        dest = seat_new.generate(
            args(tmp_path / "seat", role="reviewer", client="goose")
        )
        generated = load_generated(dest / "bin" / "board.py", "board_reviewer")
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        try:
            active["principal"] = principals["admin"]
            created = await call(
                "ticket_create",
                agent_name="admin-agent",
                title="review wait fixture",
                description="exercise reviewer subscription wait",
                target_url="pursers/tools/seat-kit",
                scope="interactive-no-send",
                required_fields=["test_output"],
                assigned_to=agent_ids["worker"],
            )
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            active["principal"] = principals["worker"]
            await call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )
            cursor = int(service.journal.read_after("pursers", 0)["latest_cursor"])
            active["principal"] = principals["reviewer"]

            parsed = generated._parser().parse_args(
                ["wait", "--submitted", "--since", str(cursor), "--timeout", "3"]
            )
            assert parsed.submitted is True
            async with Client(mcp, mode="2026-07-28", cache=None) as raw_client:
                adapter = LocalSubscriptionAdapter(
                    raw_client, service, agent_ids["reviewer"]
                )
                output = io.StringIO()

                async def run_wait() -> None:
                    with redirect_stdout(output):
                        await generated._cmd_wait(
                            adapter,
                            "pursers",
                            parsed.since,
                            parsed.timeout,
                            submitted=parsed.submitted,
                            poll_fallback=False,
                            submitted_relevant_kinds=REVIEWER_WAIT_KINDS,
                        )

                waiting = asyncio.create_task(run_wait())
                await asyncio.wait_for(adapter.ready.wait(), timeout=1)
                active["principal"] = principals["worker"]
                submitted = await call(
                    "ticket_submit",
                    agent_name="worker-agent",
                    ticket_id=ticket_id,
                    summary="ready for review",
                    notes="test_output: integration fixture",
                    files_changed=["tools/seat-kit/seat_new.py"],
                    stay_active=True,
                )
                assert submitted.structured_content["ticket"]["status"] == "submitted"
                await asyncio.wait_for(waiting, timeout=2)
                result = json.loads(output.getvalue())

                assert result["timed_out"] is False
                assert result["new_seq"] > cursor
                assert len(result["events"]) == 1
                assert result["events"][0]["ticket_id"] == ticket_id
                assert result["events"][0]["status_to"] == "submitted"
                assert adapter.ticket_list_calls == 0
                assert adapter.catchup_calls == 0
                assert adapter.events_calls[0]["only_mine"] is False
                assert adapter.events_calls[0]["touch"] is False
                assert adapter.events_calls[0]["kinds"] == REVIEWER_WAIT_KINDS
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_live_holder_annotation_wakes_and_reviewer_claims_expired_broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from mcp import Client

        generated = load_generated(
            seat_new.generate(args(tmp_path / "seat", client="goose"))
            / "bin" / "board.py",
            "board_held_live_probe",
        )
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        capabilities = {
            "tier_max": 3,
            "skills": [],
            "can_work": True,
            "can_review": False,
            "host": "test",
            "max_parallel": 1,
        }
        try:
            active["principal"] = principals["worker"]
            await call(
                "board_join",
                agent_name="worker-agent",
                capabilities=capabilities,
                allow_takeover=True,
            )
            active["principal"] = principals["reviewer"]
            await call(
                "board_join",
                agent_name="reviewer-agent",
                capabilities={
                    **capabilities,
                    "can_work": False,
                    "can_review": True,
                },
                allow_takeover=True,
            )
            active["principal"] = principals["admin"]
            await call(
                "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=1
            )
            created = await call(
                "ticket_create",
                agent_name="admin-agent",
                title="held update live probe",
                description="prove annotation subscription wake",
                target_url="pursers/tools/seat-kit",
                scope="interactive-no-send",
                required_fields=["test_output"],
                assigned_to=agent_ids["worker"],
            )
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            active["principal"] = principals["worker"]
            claimed = await call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )
            assert claimed.structured_content["ticket"]["claimed_by_agent_id"] == agent_ids["worker"]
            cursor = int(service.journal.read_after("pursers", 0)["latest_cursor"])

            async with Client(mcp, mode="2026-07-28", cache=None) as raw_client:
                adapter = LocalSubscriptionAdapter(
                    raw_client, service, agent_ids["worker"]
                )
                output = io.StringIO()

                async def run_wait() -> None:
                    with redirect_stdout(output):
                        await generated._cmd_wait(
                            adapter,
                            "pursers",
                            cursor,
                            3,
                            poll_fallback=False,
                            dispatch_kinds=WORKER_WAIT_KINDS,
                        )

                waiting = asyncio.create_task(run_wait())
                await asyncio.wait_for(adapter.ready.wait(), timeout=1)
                started = time.monotonic()
                active["principal"] = principals["admin"]
                await call(
                    "ticket_annotate",
                    agent_name="admin-agent",
                    ticket_id=ticket_id,
                    text="live probe evidence",
                    kind="evidence",
                )
                await asyncio.wait_for(waiting, timeout=2)
                elapsed = time.monotonic() - started
                result = json.loads(output.getvalue())

            assert elapsed < 2
            assert result["reason"] == "held_ticket_update"
            assert result["events"][0]["kind"] == "ticket_annotated"
            assert adapter.events_calls[0]["kinds"] == (
                frozenset({"ticket_created"}) | WORKER_WAIT_KINDS
            )

            active["principal"] = principals["worker"]
            submitted = await call(
                "ticket_submit",
                agent_name="worker-agent",
                ticket_id=ticket_id,
                summary="ready for broadcast probe",
                notes="test_output: live probe",
                files_changed=["tools/seat-kit/seat_new.py"],
            )
            assert submitted.structured_content["ticket"]["review_offer"]["agent_id"] == agent_ids["reviewer"]
            await asyncio.sleep(1.05)
            active["principal"] = principals["admin"]
            await call("board_reap")
            current = await call("ticket_get", ticket_id=ticket_id)
            assert current.structured_content["ticket"]["dispatch_state"]["state"] == "broadcast"
            assert "review_offer" not in current.structured_content["ticket"]

            active["principal"] = principals["reviewer"]
            reviewed = await call(
                "ticket_review_claim",
                agent_name="reviewer-agent",
                ticket_id=ticket_id,
            )
            assert reviewed.structured_content["ok"] is True
            assert reviewed.structured_content["ticket"]["review_lease"]["reviewer_agent_id"] == agent_ids["reviewer"]
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_live_registry_wait_resumes_stable_seat_and_wakes_on_held_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PURSERS_CAN_REVIEW", "false")
    monkeypatch.setenv("PURSERS_CAN_WORK", "true")

    async def exercise() -> None:
        from mcp import Client
        from pursers_client import (
            BoardClient,
            active_registry_boards,
            registry_project_work_dirs,
            registry_work_dirs,
            wait_for_boards,
        )
        import pursers_client.client as client_module
        import pursers_client.project_registry as registry_module

        generated = load_generated(
            seat_new.generate(args(tmp_path / "seat", client="goose"))
            / "bin" / "board.py",
            "board_registry_live_probe",
        )
        (
            central,
            mcp,
            service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)
        other_board = "fullplatts"

        async def call_other(name: str, **arguments: Any) -> Any:
            return await mcp.call_tool(
                name, {"board_id": other_board, **arguments}
            )

        capabilities = {
            "tier_max": 3,
            "skills": [],
            "can_work": True,
            "can_review": False,
            "host": "test",
            "max_parallel": 1,
        }
        registry = {
            "schema_version": 1,
            "projects": {
                "home": {
                    "board_id": "pursers",
                    "work_dir": "/repo/home",
                    "status": "active",
                },
                "other": {
                    "board_id": other_board,
                    "work_dir": "/repo/other",
                    "status": "active",
                },
            },
        }
        try:
            active["principal"] = principals["admin"]
            await call_other("board_join", agent_name="admin-agent")
            await call_other(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principals["worker"].principal_id,
                role="member",
            )
            active["principal"] = principals["worker"]
            other_joined = await call_other(
                "board_join",
                agent_name="worker-agent",
                capabilities=capabilities,
            )
            other_agent_id = other_joined.structured_content["agent_id"]

            active["principal"] = principals["admin"]
            created = await call(
                "ticket_create",
                agent_name="admin-agent",
                title="registry held update live probe",
                description="prove stable-seat registry subscription wake",
                target_url="home/tools/seat-kit",
                scope="interactive-no-send",
                required_fields=["test_output"],
                assigned_to=agent_ids["worker"],
            )
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            active["principal"] = principals["worker"]
            await call("ticket_claim", agent_name="worker-agent", ticket_id=ticket_id)
            cursors = {
                board_id: int(service.journal.read_after(board_id, 0)["latest_cursor"])
                for board_id in ("pursers", other_board)
            }

            @asynccontextmanager
            async def http_context():
                yield object()

            class LocalBoardClient(BoardClient):
                def _http(self):
                    return http_context()

            ready = asyncio.Event()

            class SignalingClient:
                def __init__(self, transport, **kwargs):
                    self.inner = Client(transport, **kwargs)

                async def __aenter__(self):
                    await self.inner.__aenter__()
                    return self

                async def __aexit__(self, *args):
                    return await self.inner.__aexit__(*args)

                async def call_tool(self, *args, **kwargs):
                    token = active.set(principals["worker"])
                    try:
                        return await self.inner.call_tool(*args, **kwargs)
                    finally:
                        active.reset(token)

                @asynccontextmanager
                async def listen(self, **kwargs):
                    async with self.inner.listen(**kwargs) as subscription:
                        async def signaled_subscription():
                            ready.set()
                            async for cue in subscription:
                                yield cue

                        yield signaled_subscription()

            monkeypatch.setattr(
                client_module, "streamable_http_client", lambda *_args, **_kwargs: mcp
            )
            monkeypatch.setattr(
                registry_module,
                "streamable_http_client",
                lambda *_args, **_kwargs: mcp,
            )
            monkeypatch.setattr(registry_module, "Client", SignalingClient)

            output = io.StringIO()
            async with LocalBoardClient(
                "http://central.invalid/mcp",
                "test-token",
                "pursers",
                agent_name="worker-agent",
                role="worker",
                capabilities=capabilities,
                allow_takeover=True,
            ) as board_client:
                await board_client.board_join(
                    capabilities=capabilities, allow_takeover=True
                )

                async def run_wait() -> None:
                    with redirect_stdout(output):
                        await generated._cmd_wait(
                            board_client,
                            "pursers",
                            cursors,
                            10,
                            boards="registry",
                            registry=registry,
                            active_registry_boards=active_registry_boards,
                            registry_work_dirs=registry_work_dirs,
                            registry_project_work_dirs=registry_project_work_dirs,
                            wait_for_boards=wait_for_boards,
                            dispatch_kinds=WORKER_WAIT_KINDS,
                        )

                waiting = asyncio.create_task(run_wait())
                await asyncio.wait_for(ready.wait(), timeout=5)
                active["principal"] = principals["admin"]
                await call(
                    "ticket_annotate",
                    agent_name="admin-agent",
                    ticket_id=ticket_id,
                    text="registry live probe evidence",
                    kind="evidence",
                )
                active["principal"] = principals["worker"]
                await asyncio.wait_for(waiting, timeout=5)
            result = json.loads(output.getvalue())

            assert result["boards"] == [other_board, "pursers"]
            assert result["skipped_boards"] == {}
            assert result["reason"] == "held_ticket_update"
            assert result["events"][0]["kind"] == "ticket_annotated"
            assert result["events"][0]["ticket_id"] == ticket_id
            other_member = service.load(other_board)["members"][other_agent_id]
            assert other_member["lifecycle_status"] == "active"
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_generated_submit_preflights_exact_remote_tip_before_board_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(author)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seat Test"], cwd=author, check=True)
    subprocess.run(["git", "config", "user.email", "seat@example.test"], cwd=author, check=True)
    (author / "change.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "change.txt"], cwd=author, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=author, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=author, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=author, check=True, capture_output=True)
    branch = "codex/TK-submit"
    subprocess.run(["git", "switch", "-c", branch], cwd=author, check=True, capture_output=True)
    (author / "change.txt").write_text("stale\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "stale candidate"], cwd=author, check=True, capture_output=True)
    stale_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=author, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=author, check=True, capture_output=True)

    dest = seat_new.generate(
        args(tmp_path / "generated", repo=str(origin), client="goose")
    )
    generated = load_generated(dest / "bin" / "board.py", "board_submit_preflight")
    (author / "change.txt").write_text("current\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "current candidate"], cwd=author, check=True, capture_output=True)
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=author, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "origin", branch], cwd=author, check=True, capture_output=True)

    ticket = {
        "ticket_id": "TK-submit",
        "target_url": "origin/tools/seat-kit",
        "required_fields": ["branch_and_commit", "test_output"],
    }
    submissions: list[dict[str, object]] = []

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def ticket_get(self, _ticket_id: str) -> dict[str, object]:
            return {"ticket": ticket}

        async def ticket_submit(self, ticket_id: str, **arguments: object):
            submissions.append({"ticket_id": ticket_id, **arguments})
            return {"ok": True}

    monkeypatch.setattr(generated, "_load_client", lambda: Client)
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

    def parsed(notes: str):
        return generated._parser().parse_args(
            ["submit", "TK-submit", "ready", notes, "change.txt"]
        )

    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(generated._execute(parsed(
            f"branch_and_commit: {branch} @ {current_sha[:12]}"
        )))
    wrong_sha = current_sha[:-1] + ("0" if current_sha[-1] != "0" else "1")
    with pytest.raises(ValueError, match="nonexistent commit"):
        asyncio.run(generated._execute(parsed(
            f"branch_and_commit: {branch} @ {wrong_sha}"
        )))
    with pytest.raises(ValueError, match="nonexistent commit"):
        asyncio.run(generated._execute(parsed(
            f"branch_and_commit: {branch} @ {'f' * 40}"
        )))
    with pytest.raises(ValueError, match="moved or mismatched"):
        asyncio.run(generated._execute(parsed(
            f"branch_and_commit: {branch} @ {stale_sha}"
        )))
    with pytest.raises(RuntimeError, match="could not fetch origin/codex/TK-missing"):
        asyncio.run(generated._execute(parsed(
            f"branch_and_commit: codex/TK-missing @ {current_sha}"
        )))
    assert submissions == []

    asyncio.run(generated._execute(parsed(
        f"branch_and_commit: {branch} @ {current_sha}\npytest: 1 passed"
    )))
    result = json.loads(capsys.readouterr().out)
    assert result["submission_preflight"] == {
        "branch": branch,
        "commit": current_sha,
        "remote_ref": f"origin/{branch}",
        "remote_tip": current_sha,
    }
    assert f"branch_and_commit: {branch} @ {current_sha}" in str(
        submissions[0]["notes"]
    )

    long_notes = (
        "test-command: PYTHONPATH=. pytest -q .\n"
        + "test_output: " + "x" * 5_500
        + f"\nbranch_and_commit: {branch} @ {current_sha}"
    )
    asyncio.run(generated._execute(parsed(long_notes)))
    long_result = json.loads(capsys.readouterr().out)
    submitted_notes = str(submissions[1]["notes"])
    assert len(submitted_notes) <= 5_000
    assert len(generated.SUBMIT_BRANCH_COMMIT_RE.findall(submitted_notes)) == 1
    replay_ticket = {"submission_history": [{"notes": submitted_notes}]}
    _submission, replay_sha, replay_branch = generated._submission(replay_ticket)
    assert (replay_branch, replay_sha) == (branch, current_sha)
    assert f"remote_tip={current_sha}" in submitted_notes
    assert long_result["submission_preflight"]["remote_tip"] == current_sha
    replay_commands = generated._suite_commands(
        {}, {"notes": submitted_notes}, author
    )
    assert replay_commands == [{
        "argv": ["pytest", "-q", "."],
        "pythonpath": ".",
        "display": ["PYTHONPATH=.", "pytest", "-q", "."],
    }]
    replay_environment = generated._suite_environment(replay_commands[0])
    assert replay_environment["PYTHONPATH"] == "."
    assert replay_environment["PYTHONNOUSERSITE"] == "1"
    assert replay_environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

    selected = generated._submit_source_repo(
        ticket,
        routed=str(dest / "origin"),
        operator_dir=None,
        route_error=None,
        seat_repo=tmp_path / "unrelated",
        repo_leaf="unrelated",
    )
    routed_evidence = generated._submit_preflight(
        ticket,
        selected,
        summary="ready",
        notes=f"branch_and_commit: {branch} @ {current_sha}",
    )
    assert routed_evidence["remote_tip"] == current_sha

    non_git = tmp_path / "not-git"
    non_git.mkdir()
    with pytest.raises(ValueError, match="routed target to be a git checkout"):
        generated._submit_source_repo(
            ticket,
            routed=str(non_git),
            operator_dir=None,
            route_error=None,
            seat_repo=dest / "origin",
            repo_leaf="origin",
        )
    with pytest.raises(ValueError, match="no git checkout for the ticket target"):
        generated._submit_source_repo(
            ticket,
            routed=None,
            operator_dir=None,
            route_error=None,
            seat_repo=dest / "origin",
            repo_leaf=None,
        )
    cross_project = {**ticket, "target_url": "other/tools/seat-kit"}
    with pytest.raises(ValueError, match="unrelated seat clone"):
        generated._submit_source_repo(
            cross_project,
            routed=None,
            operator_dir=None,
            route_error=None,
            seat_repo=dest / "origin",
            repo_leaf="origin",
        )

    ticket["required_fields"] = ["research_findings"]
    asyncio.run(generated._execute(parsed("research_findings: complete")))
    research_result = json.loads(capsys.readouterr().out)
    assert research_result["ok"] is True
    assert "submission_preflight" not in research_result
    assert len(submissions) == 3


def test_invalid_submit_keeps_central_ticket_unsubmitted_but_join_may_renew_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        from pursers_client import BoardClient
        import pursers_client.client as client_module

        project = tmp_path / "project"
        subprocess.run(
            ["git", "init", "-b", "main", str(project)],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "config", "user.name", "Seat Test"], cwd=project, check=True)
        subprocess.run(
            ["git", "config", "user.email", "seat@example.test"],
            cwd=project,
            check=True,
        )
        (project / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=project,
            check=True,
            capture_output=True,
        )
        generated = load_generated(
            seat_new.generate(args(tmp_path / "seat", repo=str(project), client="goose"))
            / "bin"
            / "board.py",
            "board_submit_central_state",
        )
        (
            central,
            mcp,
            _service,
            principals,
            active,
            agent_ids,
            call,
            original_current_principal,
        ) = await build_local_central(tmp_path / "central", monkeypatch)

        @asynccontextmanager
        async def http_context():
            yield object()

        class LocalBoardClient(BoardClient):
            def _http(self):
                return http_context()

        monkeypatch.setattr(
            client_module, "streamable_http_client", lambda *_args, **_kwargs: mcp
        )
        monkeypatch.setattr(generated, "_load_client", lambda: LocalBoardClient)
        monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
        monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
        monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
        monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

        try:
            active["principal"] = principals["admin"]
            created = await call(
                "ticket_create",
                agent_name="admin-agent",
                title="submit preflight state probe",
                description="prove invalid evidence does not submit",
                target_url="project/tools/seat-kit",
                scope="interactive-no-send",
                required_fields=["branch_and_commit", "test_output"],
                assigned_to=agent_ids["worker"],
            )
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            active["principal"] = principals["worker"]
            claimed = await call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )
            before_lease = claimed.structured_content["ticket"]["lease_expires_at"]
            await asyncio.sleep(0.01)
            parsed = generated._parser().parse_args([
                "submit",
                ticket_id,
                "ready",
                "branch_and_commit: codex/TK-probe @ deadbeef",
                "base.txt",
            ])
            with pytest.raises(ValueError, match="exactly one"):
                await generated._execute(parsed)
            current = await call("ticket_get", ticket_id=ticket_id)
            current_ticket = current.structured_content["ticket"]
            assert current_ticket["status"] == "claimed"
            assert current_ticket.get("submission_history", []) == []
            assert current_ticket["lease_expires_at"] > before_lease
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_generated_submit_truncates_notes_and_reports_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    active_seats: set[str] = set()

    class Client:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured["constructor_kwargs"] = kwargs
            self.agent_name = str(kwargs["agent_name"])
            self.allow_takeover = bool(kwargs["allow_takeover"])

        async def __aenter__(self):
            if self.agent_name in active_seats and not self.allow_takeover:
                raise RuntimeError(
                    "seat name already active under this principal; "
                    "choose another name or pass allow_takeover=true"
                )
            active_seats.add(self.agent_name)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["allow_takeover"] is True
            return {"ok": True}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {
                "ticket": {
                    "ticket_id": ticket_id,
                    "required_fields": ["research_findings"],
                }
            }

        async def ticket_submit(self, ticket_id: str, **arguments: object):
            captured.update({"ticket_id": ticket_id, **arguments})
            return {"ok": True}

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_submit_notes")
    monkeypatch.setattr(generated, "_load_client", lambda: Client)
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")
    notes = "\n".join(f"line-{index:03d}-" + "x" * 90 for index in range(60))
    parsed = generated._parser().parse_args(
        ["submit", "TK-long-notes", "ready", notes, "changed.py"]
    )

    asyncio.run(generated._execute(parsed))

    streams = capsys.readouterr()
    result = json.loads(streams.out)
    asyncio.run(generated._execute(parsed))
    restarted_streams = capsys.readouterr()
    assert json.loads(restarted_streams.out)["ok"] is True
    submitted = captured["notes"]
    metadata = result["input_truncation"]["notes"]
    assert captured["constructor_kwargs"]["allow_takeover"] is True
    assert len(submitted) <= 5_000
    assert submitted.endswith(metadata["marker"])
    assert metadata["truncated_chars"] > 0
    assert "warning: ticket_submit notes exceeded 5000 characters" in streams.err
    assert active_seats == {"worker-agent"}


def test_generated_claim_refuses_operator_checkout_before_board_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims: list[str] = []
    registry = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "board_id": "pursers",
                "work_dir": "/operator/alpha",
                "work_dir_owner": "operator",
                "status": "active",
            }
        },
    }

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def board_state_get(self, **_kwargs: object) -> dict[str, object]:
            return {"state": {"value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {"ticket": {"ticket_id": ticket_id, "target_url": "alpha/task"}}

        async def ticket_claim(self, ticket_id: str) -> dict[str, object]:
            claims.append(ticket_id)
            return {"ok": True}

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_clone_guard")
    monkeypatch.setattr(
        generated,
        "_load_client",
        lambda: (
            Client,
            frozenset(),
            "project_registry",
            frozenset(),
            lambda value, _home: value,
            lambda value: json.loads(value["state"]["value"]),
            lambda value: {"alpha": value["projects"]["alpha"]["work_dir"]},
            lambda value: {"pursers": value["projects"]["alpha"]["work_dir"]},
            object(),
        ),
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

    asyncio.run(generated._execute(generated._parser().parse_args(["claim", "TK-unsafe"])))

    result = json.loads(capsys.readouterr().out)
    assert claims == []
    assert result["claim_refused"] is True
    assert result["error"] == {
        "code": "operator_checkout_read_only",
        "message": "operator checkout is read-only for seats",
    }


def test_generated_claim_routes_matching_seat_owned_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims: list[str] = []
    registry = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "board_id": "pursers",
                "work_dir": "/operator/alpha",
                "status": "active",
            }
        },
    }

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def board_state_get(self, **_kwargs: object) -> dict[str, object]:
            return {"state": {"value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {"ticket": {"ticket_id": ticket_id, "target_url": "alpha/task"}}

        async def ticket_claim(self, ticket_id: str) -> dict[str, object]:
            claims.append(ticket_id)
            return {"ok": True, "ticket": {"ticket_id": ticket_id, "target_url": "alpha/task"}}

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    (dest / "alpha" / ".git").mkdir(parents=True)
    generated = load_generated(dest / "bin" / "board.py", "board_own_clone")
    generated.REPO_LEAF = "alpha"
    monkeypatch.setattr(
        generated,
        "_load_client",
        lambda: (
            Client,
            frozenset(),
            "project_registry",
            frozenset(),
            lambda value, _home: value,
            lambda value: json.loads(value["state"]["value"]),
            lambda value: {"alpha": value["projects"]["alpha"]["work_dir"]},
            lambda value: {"pursers": value["projects"]["alpha"]["work_dir"]},
            object(),
        ),
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

    asyncio.run(generated._execute(generated._parser().parse_args(["claim", "TK-safe"])))

    result = json.loads(capsys.readouterr().out)
    assert claims == ["TK-safe"]
    assert result["work_dir"] == str(dest / "alpha")


@pytest.mark.parametrize(
    ("target_url", "make_git", "expected_code"),
    [
        ("https://[", True, "target_url_malformed"),
        ("https://example.test/acme/unknown", True, "repository_url_not_registered"),
        ("https://example.test/acme/alpha", False, "routed_repository_unavailable"),
    ],
)
def test_generated_claim_refuses_invalid_repository_route_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_url: str,
    make_git: bool,
    expected_code: str,
) -> None:
    from pursers_client import (
        parse_project_registry,
        registry_project_work_dirs,
        registry_work_dirs,
        resolve_registry_target,
    )

    claims: list[str] = []
    fleet = tmp_path / "fleet-alpha"
    if make_git:
        (fleet / ".git").mkdir(parents=True)
    registry = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "board_id": "pursers",
                "work_dir": "/operator/alpha",
                "fleet_clone_dir": str(fleet),
                "repository_url": "https://example.test/acme/alpha",
                "status": "active",
            }
        },
    }

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.identity = SimpleNamespace(agent_id="AI-worker")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def board_state_get(self, **_kwargs: object) -> dict[str, object]:
            return {"state": {"value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {"ticket": {"ticket_id": ticket_id, "target_url": target_url}}

        async def ticket_claim(self, ticket_id: str) -> dict[str, object]:
            claims.append(ticket_id)
            return {"ok": True}

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", f"board_{expected_code}")
    monkeypatch.setattr(
        generated,
        "_load_client",
        lambda: (
            Client,
            frozenset(),
            "project_registry",
            frozenset(),
            lambda value, home: [home],
            parse_project_registry,
            registry_project_work_dirs,
            registry_work_dirs,
            resolve_registry_target,
            object(),
        ),
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

    asyncio.run(generated._execute(generated._parser().parse_args(["claim", "TK-route"])))

    result = json.loads(capsys.readouterr().out)
    assert claims == []
    assert result["claim_refused"] is True
    assert result["error"]["code"] == expected_code


def test_generated_claim_accepts_exact_registered_repository_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pursers_client import (
        parse_project_registry,
        registry_project_work_dirs,
        registry_work_dirs,
        resolve_registry_target,
    )

    claims: list[str] = []
    fleet = tmp_path / "fleet-alpha"
    (fleet / ".git").mkdir(parents=True)
    registry = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "board_id": "pursers",
                "work_dir": "/operator/alpha",
                "fleet_clone_dir": str(fleet),
                "repository_url": "https://example.test/acme/alpha",
                "status": "active",
            }
        },
    }

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.identity = SimpleNamespace(agent_id="AI-worker")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def board_state_get(self, **_kwargs: object) -> dict[str, object]:
            return {"state": {"value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {"ticket": {
                "ticket_id": ticket_id,
                "target_url": "https://example.test/acme/alpha",
            }}

        async def ticket_claim(self, ticket_id: str) -> dict[str, object]:
            claims.append(ticket_id)
            return {"ok": True, "ticket": {
                "ticket_id": ticket_id,
                "target_url": "https://example.test/acme/alpha",
            }}

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_url_route")
    monkeypatch.setattr(
        generated,
        "_load_client",
        lambda: (
            Client, frozenset(), "project_registry", frozenset(),
            lambda value, home: [home], parse_project_registry,
            registry_project_work_dirs, registry_work_dirs,
            resolve_registry_target, object(),
        ),
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")

    asyncio.run(generated._execute(generated._parser().parse_args(["claim", "TK-url"])))

    result = json.loads(capsys.readouterr().out)
    assert claims == ["TK-url"]
    assert result["work_dir"] == str(fleet)


def test_generated_claim_surfaces_gate_error_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claims: list[str] = []
    message = "ticket is not offered to this seat; wait for your offer"
    registry = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "board_id": "pursers",
                "work_dir": "/operator/alpha",
                "status": "active",
            }
        },
    }

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True}

        async def board_state_get(self, **_kwargs: object) -> dict[str, object]:
            return {"state": {"value": json.dumps(registry)}}

        async def ticket_get(self, ticket_id: str) -> dict[str, object]:
            return {"ticket": {"ticket_id": ticket_id, "target_url": "alpha/task"}}

        async def ticket_claim(self, ticket_id: str) -> dict[str, object]:
            claims.append(ticket_id)
            raise RuntimeError(message)

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    (dest / "alpha" / ".git").mkdir(parents=True)
    generated = load_generated(dest / "bin" / "board.py", "board_claim_gate")
    generated.REPO_LEAF = "alpha"
    monkeypatch.setattr(
        generated,
        "_load_client",
        lambda: (
            Client,
            frozenset(),
            "project_registry",
            frozenset(),
            lambda value, _home: value,
            lambda value: json.loads(value["state"]["value"]),
            lambda value: {"alpha": value["projects"]["alpha"]["work_dir"]},
            lambda value: {"pursers": value["projects"]["alpha"]["work_dir"]},
            object(),
        ),
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")
    monkeypatch.setattr(sys, "argv", ["board.py", "claim", "TK-unoffered"])

    assert generated.main() == 1
    streams = capsys.readouterr()
    assert streams.out == ""
    assert streams.err.strip() == f"board.sh: {message}"
    assert claims == ["TK-unoffered"]


def test_generated_main_real_listen_event_exits_zero_without_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pursers_client import BoardClient
    import pursers_client.client as client_module

    async def prepare():
        fixture = await build_local_central(tmp_path / "central", monkeypatch)
        (
            _central,
            _mcp,
            _service,
            principals,
            active,
            _agent_ids,
            call,
            _original_current_principal,
        ) = fixture
        active["principal"] = principals["worker"]
        await call("board_join", agent_name="event-actor")
        created = await call(
            "ticket_create",
            agent_name="event-actor",
            title="generated main early exit",
            description="exercise generated CLI over a real in-process listen",
            target_url="pursers/tools/seat-kit",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        return fixture, created.structured_content["ticket"]["ticket_id"]

    fixture, ticket_id = asyncio.run(prepare())
    central, mcp, _service, principals, active, _agent_ids, _call, original = fixture

    @asynccontextmanager
    async def http_context():
        yield object()

    class RealListenBoardClient(BoardClient):
        def _http(self):
            return http_context()

    dest = seat_new.generate(args(tmp_path / "seat", client="goose"))
    generated = load_generated(dest / "bin" / "board.py", "board_real_event")
    monkeypatch.setattr(generated, "_load_client", lambda: RealListenBoardClient)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_a, **_k: mcp
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_URL", "http://central.invalid/mcp")
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "test-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "pursers")
    monkeypatch.setenv("ONBOARD_AGENT_NAME", "worker-agent")
    monkeypatch.setattr(sys, "argv", ["board.sh", "wait", "--timeout", "1"])

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        active["principal"] = principals["worker"]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = generated.main()
    finally:
        central.current_principal = original

    result = json.loads(stdout.getvalue())
    assert returncode == 0
    assert stderr.getvalue() == ""
    assert result["timed_out"] is False
    assert result["events"][0]["ticket_id"] == ticket_id
