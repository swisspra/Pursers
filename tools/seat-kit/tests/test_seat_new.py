from __future__ import annotations

import asyncio
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
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
from pursers_client import SUBMITTED_RELEVANT_KINDS  # noqa: E402

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
            "--ca-file",
            str(tmp_path / "ca.pem"),
            "--client",
            client,
            *(["--repo", repo] if repo else []),
        ]
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def load_generated(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert "Claim only a ticket offered to this seat" in guidance
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
        "tests": ["test-command: python3 -m unittest discover -s . -p test_sample.py"],
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

        async def board_join(self):
            return {"ok": True}

        async def board_state_get(self, *, key):
            return {"key": key, "value": {"schema_version": 1}}

        async def ticket_get(self, ticket_id):
            assert ticket_id == ticket["ticket_id"]
            return {"ok": True, "ticket": ticket}

    generated._load_client = lambda: (
        ReviewClient,
        "project_registry",
        frozenset({"ticket_submitted"}),
        lambda _registry, _home: ["pursers"],
        lambda _value: {"schema_version": 1},
        lambda _registry: {"sample": str(routed)},
        lambda _registry: {"pursers": str(routed)},
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
        (
            "jwt",
            "token=" + "e" + "yJabcde.abcdefghijkl.abcdefghijklmnop",
        ),
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

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append(command)
        Path(command[-1]).mkdir()

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
    dest = seat_new.generate(parsed)
    (dest / "keep.txt").write_text("operator-owned", encoding="utf-8")
    (dest / "bin/board.sh").write_text("stale", encoding="utf-8")
    parsed.upgrade = True

    seat_new.generate(parsed)

    assert (dest / "keep.txt").read_text() == "operator-owned"
    assert "ONBOARD_AGENT_NAME" in (dest / "bin/board.sh").read_text()
    assert str(Path(sys.executable).resolve()) in (dest / "bin/board.sh").read_text()


def test_upgrade_fast_forwards_existing_clean_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = args(tmp_path, repo="https://example.test/Pursers.git")
    parsed.upgrade = True
    clone = Path(parsed.dest) / "Pursers"
    clone.mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs.get("cwd")))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(seat_new.subprocess, "run", run)
    seat_new.generate(parsed)

    assert (["git", "status", "--porcelain"], clone) in calls
    assert (["git", "pull", "--ff-only"], clone) in calls


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
            "--ca-file",
            str(tmp_path / "ca.pem"),
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
    active = {"principal": principals["admin"]}
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
            role="member",
        )
        active["principal"] = principals[key]
        joined = await call("board_join", agent_name=f"{key}-agent")
        agent_ids[key] = joined.structured_content["agent_id"]
        active["principal"] = principals["admin"]
    return central, mcp, service, principals, active, agent_ids, call, original_current_principal


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
                        adapter, "pursers", cursor, 60, poll_fallback=False
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
                        "kinds": frozenset(
                            {"ticket_created", "ticket_status_changed"}
                        ),
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
                            submitted_relevant_kinds=SUBMITTED_RELEVANT_KINDS,
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
                assert adapter.events_calls[0]["kinds"] == SUBMITTED_RELEVANT_KINDS
        finally:
            central.current_principal = original_current_principal

    asyncio.run(exercise())


def test_generated_submit_truncates_notes_and_reports_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def board_join(self) -> dict[str, object]:
            return {"ok": True}

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
    submitted = captured["notes"]
    metadata = result["input_truncation"]["notes"]
    assert len(submitted) <= 5_000
    assert submitted.endswith(metadata["marker"])
    assert metadata["truncated_chars"] > 0
    assert "warning: ticket_submit notes exceeded 5000 characters" in streams.err


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
    central, mcp, _service, _principals, _active, _agent_ids, _call, original = fixture

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
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = generated.main()
    finally:
        central.current_principal = original

    result = json.loads(stdout.getvalue())
    assert returncode == 0
    assert stderr.getvalue() == ""
    assert result["timed_out"] is False
    assert result["events"][0]["ticket_id"] == ticket_id
