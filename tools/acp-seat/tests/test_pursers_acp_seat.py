from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
ACP_ROOT = ROOT / "tools" / "acp-seat"
CENTRAL_SRC = ROOT / "packages" / "central" / "src" / "pursers_central"
CLIENT_SRC = ROOT / "packages" / "client" / "src"
for entry in (ACP_ROOT, CENTRAL_SRC, CLIENT_SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

spec = importlib.util.spec_from_file_location(
    "pursers_acp_seat", ACP_ROOT / "pursers_acp_seat.py"
)
assert spec and spec.loader
seat = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = seat
spec.loader.exec_module(seat)

FAKE = ACP_ROOT / "tests" / "fake_acp_agent.py"


class FakeBoard:
    def __init__(self, ticket: dict[str, object]) -> None:
        self.ticket = ticket
        self.agent_id = "AI-seat"
        self.principal_id = "PR-private"
        self.checkpoints: list[str] = []
        self.renewals = 0
        self.submissions: list[dict[str, object]] = []
        self.unclaims = 0

    async def claim(self, _ticket_id: str) -> dict[str, object]:
        self.ticket.update(status="claimed", claimed_by_agent_id=self.agent_id)
        return self.ticket

    async def ticket_get(self, _ticket_id: str) -> dict[str, object]:
        return self.ticket

    async def checkpoint(
        self, _ticket_id: str, summary: str, *, files: list[str] | None = None
    ) -> None:
        self.checkpoints.append(summary)

    async def renew(self, _ticket_id: str) -> None:
        self.renewals += 1

    async def submit(self, _ticket_id: str, completion: dict[str, object]) -> None:
        self.submissions.append(completion)
        self.ticket["status"] = "submitted"

    async def unclaim(self, _ticket_id: str) -> None:
        self.unclaims += 1
        self.ticket["status"] = "open"


def write_script(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "agent-script.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def committed_worktree(work_root: Path, ticket_id: str) -> tuple[Path, str, str]:
    work = work_root / ticket_id
    work.mkdir(parents=True)
    git("init", "-b", "acp/test-ticket", cwd=work)
    git("config", "user.name", "ACP Test", cwd=work)
    git("config", "user.email", "acp-test@example.invalid", cwd=work)
    (work / "result.txt").write_text("base\n", encoding="utf-8")
    git("add", "result.txt", cwd=work)
    git("commit", "-m", "base", cwd=work)
    (work / "result.txt").write_text("complete\n", encoding="utf-8")
    git("add", "result.txt", cwd=work)
    git("commit", "-m", "complete work", cwd=work)
    return work, "acp/test-ticket", git("rev-parse", "HEAD", cwd=work)


def base_ticket(ticket_id: str = "TK-acp-e2e") -> dict[str, object]:
    return {
        "ticket_id": ticket_id,
        "title": "ACP end to end",
        "description": "make the requested change",
        "scope": "interactive",
        "required_fields": ["branch_and_commit", "test-output"],
        "forbidden": ["tokens"],
        "annotations": [{"kind": "decision", "text": "decision-marker"}],
        "status": "open",
    }


async def permission_denied_releases_with_checkpoint(tmp_path: Path) -> None:
    board = FakeBoard(base_ticket("TK-denied"))
    script = write_script(
        tmp_path,
        {
            "promptActions": [
                {
                    "type": "permission",
                    "toolCall": {
                        "toolCallId": "net-1",
                        "title": "network",
                        "kind": "fetch",
                    },
                    "expectedOutcome": {
                        "outcome": "selected",
                        "optionId": "reject-once",
                    },
                }
            ]
        },
    )
    runtime = seat.ACPSeatRuntime(
        board,
        [sys.executable, str(FAKE), "--script", str(script)],
        tmp_path / "work",
        lease_interval_s=0.01,
    )

    assert await runtime.run_ticket("TK-denied") == "released"
    assert board.unclaims == 1
    assert any("completion metadata" in item for item in board.checkpoints)
    assert any(
        "ACP permission" in item and "reject_once" in item
        for item in board.checkpoints
    )


async def agent_crash_releases_with_checkpoint(tmp_path: Path) -> None:
    board = FakeBoard(base_ticket("TK-crash"))
    script = write_script(
        tmp_path,
        {"promptActions": [{"type": "crash", "code": 31, "stderr": "boom"}]},
    )
    runtime = seat.ACPSeatRuntime(
        board,
        [sys.executable, str(FAKE), "--script", str(script)],
        tmp_path / "work",
    )

    assert await runtime.run_ticket("TK-crash") == "released"
    assert board.unclaims == 1
    assert any("ACPProcessError" in item for item in board.checkpoints)


async def fake_agent_submits_through_in_process_central(tmp_path: Path) -> None:
    import central
    from mcp import Client

    work_root = tmp_path / "work"
    _work, branch, commit = committed_worktree(work_root, "TK-acp-e2e")
    notes = "\n".join(
        [
            f"branch_and_commit: {branch}@{commit}",
            "test-command: python3 -m pytest -q tools/acp-seat/tests",
            "test-output: passed",
            "observations: fake agent drove an in-process Central",
        ]
    )
    script = write_script(
        tmp_path,
        {
            "promptMustContain": ["ACP end to end", "decision-marker"],
            "promptMustNotContain": ["PR-seat-private"],
            "promptActions": [
                {
                    "type": "update",
                    "update": {
                        "sessionUpdate": "plan",
                        "entries": [{"content": "submit", "status": "completed"}],
                    },
                },
                {"type": "sleep", "seconds": 0.03},
                {
                    "type": "update",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": seat.COMPLETION_PREFIX
                            + json.dumps(
                                {
                                    "summary": "ACP completed the ticket",
                                    "files_changed": ["result.txt"],
                                    "notes": notes,
                                }
                            ),
                        },
                    },
                },
            ],
            "stopReason": "end_turn",
        },
    )

    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    environment = {
        "CENTRAL_AUTH_MODE": "jwt",
        "CENTRAL_JWT_ISSUER": "https://issuer.example",
        "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
        "CENTRAL_JWKS_PATH": str(jwks),
        "CENTRAL_ADMISSION": "invite",
        "STORE_BACKEND": "sqlite",
    }
    admin = central.Principal(
        "PR-admin-acp", "admin-acp", frozenset({"board:read", "board:write", "board:review"})
    )
    worker = central.Principal(
        "PR-seat-private", "seat-acp", frozenset({"board:read", "board:write"})
    )
    principal = contextvars.ContextVar("acp_test_principal", default=admin)
    original = central.current_principal

    with patch.dict(os.environ, environment):
        mcp, _service = central.build_server("localhost", 8765, tmp_path / "central")
        central.current_principal = principal.get
        try:
            async with Client(mcp, mode="2026-07-28", cache=None) as raw:
                async def call(who: object, name: str, **arguments: object) -> dict[str, object]:
                    token = principal.set(who)
                    try:
                        result = await raw.call_tool(name, {"board_id": "pursers", **arguments})
                        assert not result.is_error
                        assert result.structured_content is not None
                        return result.structured_content
                    finally:
                        principal.reset(token)

                await call(admin, "board_join", agent_name="admin-acp")
                await call(
                    admin,
                    "board_member_add",
                    agent_name="admin-acp",
                    principal_id=worker.principal_id,
                    role="member",
                )
                joined = await call(
                    worker,
                    "board_onboard",
                    agent_name="acp-seat",
                    role="worker",
                    allow_takeover=True,
                    capabilities={
                        "can_work": True,
                        "can_review": False,
                        "tier_max": 2,
                        "max_parallel": 1,
                    },
                )
                await call(
                    admin,
                    "ticket_create",
                    ticket_id="TK-acp-e2e",
                    agent_name="admin-acp",
                    title="ACP end to end",
                    description="make the requested change",
                    scope="interactive",
                    required_fields=["branch_and_commit", "test-output"],
                    forbidden=["tokens"],
                    assigned_to="acp-seat",
                )
                offered = await call(worker, "ticket_get", ticket_id="TK-acp-e2e")
                assert offered["ticket"]["work_offer"]["agent_name"] == "acp-seat"
                await call(
                    admin,
                    "ticket_annotate",
                    ticket_id="TK-acp-e2e",
                    agent_name="admin-acp",
                    kind="decision",
                    text="decision-marker",
                )

                class InProcessClient:
                    agent_name = "acp-seat"
                    renewals = 0
                    mutation_names: list[tuple[str, str]] = []

                    async def _call(
                        self, name: str, arguments: dict[str, object]
                    ) -> dict[str, object]:
                        selected_name = str(
                            arguments.get("agent_name", self.agent_name)
                        )
                        who = worker if selected_name == "acp-seat" else admin
                        if name in {"ticket_claim", "lease_renew", "ticket_submit"}:
                            self.mutation_names.append((name, selected_name))
                        return await call(who, name, **arguments)

                    async def ticket_claim(
                        self, ticket_id: str, *, agent_name: str | None = None
                    ) -> dict[str, object]:
                        result = await self._call(
                            "ticket_claim",
                            {
                                "ticket_id": ticket_id,
                                "agent_name": (
                                    self.agent_name
                                    if agent_name is None
                                    else agent_name
                                ),
                            },
                        )
                        # Model a shared transport whose default seat changes after
                        # the claim. Later mutations must carry the claim identity.
                        self.agent_name = "admin-acp"
                        return result

                    async def ticket_get(self, ticket_id: str) -> dict[str, object]:
                        result = await call(worker, "ticket_get", ticket_id=ticket_id)
                        return result

                    async def memory_checkpoint(
                        self,
                        summary: str,
                        *,
                        files: list[str] | None = None,
                        remaining_tasks: list[str] | None = None,
                    ) -> dict[str, object]:
                        return await call(
                            worker,
                            "memory_checkpoint",
                            agent_name="acp-seat",
                            summary=summary,
                            remaining_tasks=remaining_tasks or [],
                            files=files or [],
                        )

                    async def lease_renew(
                        self, ticket_id: str, *, agent_name: str | None = None
                    ) -> dict[str, object]:
                        self.renewals += 1
                        return await self._call(
                            "lease_renew",
                            {
                                "ticket_id": ticket_id,
                                "agent_name": (
                                    self.agent_name
                                    if agent_name is None
                                    else agent_name
                                ),
                            },
                        )

                    async def ticket_submit(
                        self,
                        ticket_id: str,
                        *,
                        agent_name: str | None = None,
                        stay_active: bool = False,
                        **completion: object,
                    ) -> dict[str, object]:
                        return await self._call(
                            "ticket_submit",
                            {
                                "ticket_id": ticket_id,
                                "agent_name": (
                                    self.agent_name
                                    if agent_name is None
                                    else agent_name
                                ),
                                "stay_active": stay_active,
                                **completion,
                            },
                        )

                client = InProcessClient()
                board = object.__new__(seat.CentralBoard)
                board.config = SimpleNamespace(agent_name="acp-seat")
                board.client = client
                board.agent_id = str(joined["agent_id"])
                board.principal_id = worker.principal_id
                board._claim_identities = {}
                runtime = seat.ACPSeatRuntime(
                    board,
                    [sys.executable, str(FAKE), "--script", str(script)],
                    work_root,
                    lease_interval_s=0.01,
                )
                assert await runtime.run_ticket("TK-acp-e2e") == "submitted"
                final = await call(worker, "ticket_get", ticket_id="TK-acp-e2e")
                ticket = final["ticket"]
                assert ticket["status"] == "submitted"
                assert ticket["files_changed"] == ["result.txt"]
                assert ticket["claimed_by_agent_id"] == joined["agent_id"]
                assert ticket["claimed_by_principal_id"] == worker.principal_id
                assert ticket["claimed_by"] == "acp-seat"
                assert ticket["submitted_by_principal_id"] == worker.principal_id
                assert client.renewals >= 1
                assert client.mutation_names[0] == ("ticket_claim", "acp-seat")
                assert client.mutation_names[-1] == ("ticket_submit", "acp-seat")
                assert all(
                    agent_name == "acp-seat"
                    for _operation, agent_name in client.mutation_names
                )
        finally:
            central.current_principal = original


def test_permission_denied_releases_with_checkpoint(tmp_path: Path) -> None:
    asyncio.run(permission_denied_releases_with_checkpoint(tmp_path))


def test_agent_crash_releases_with_checkpoint(tmp_path: Path) -> None:
    asyncio.run(agent_crash_releases_with_checkpoint(tmp_path))


def test_fake_agent_submits_through_in_process_central(tmp_path: Path) -> None:
    asyncio.run(fake_agent_submits_through_in_process_central(tmp_path))


def test_runtime_creates_standalone_ticket_clone(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    git("init", "-b", "main", cwd=repository)
    git("config", "user.name", "ACP Test", cwd=repository)
    git("config", "user.email", "acp-test@example.invalid", cwd=repository)
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md", cwd=repository)
    git("commit", "-m", "base", cwd=repository)
    board = FakeBoard(base_ticket("TK-worktree"))
    runtime = seat.ACPSeatRuntime(
        board,
        [sys.executable, str(FAKE)],
        tmp_path / "tickets",
        repository=repository,
        base_ref="main",
        seat_name="seat-one",
    )

    work = runtime._prepare_work_dir("TK-worktree")

    assert work == (tmp_path / "tickets" / "tk-worktree").resolve()
    assert git("branch", "--show-current", cwd=work) == "acp/seat-one-tk-worktree"
    assert git("rev-parse", "HEAD", cwd=work) == git("rev-parse", "main", cwd=repository)
    assert git("remote", cwd=work) == ""
    assert (work / ".git").is_dir()
    assert all(
        path.is_relative_to(work)
        for path in seat._git_metadata_paths(work).values()
    )


def test_production_sandbox_boundary_allows_standalone_clone_commit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    git("init", "-b", "main", cwd=repository)
    git("config", "user.name", "ACP Test", cwd=repository)
    git("config", "user.email", "acp-test@example.invalid", cwd=repository)
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md", cwd=repository)
    git("commit", "-m", "base", cwd=repository)
    runtime = seat.ACPSeatRuntime(
        FakeBoard(base_ticket("TK-sandbox-commit")),
        [sys.executable, str(FAKE)],
        tmp_path / "tickets",
        repository=repository,
        base_ref="main",
        seat_name="seat-one",
    )
    work = runtime._prepare_work_dir("TK-sandbox-commit")
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    operator_git_config = operator_home / ".gitconfig"
    operator_git_config.write_text("operator work identity\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    agent_code = f"""
import os
import subprocess
from pathlib import Path

assert Path.home() != Path({str(operator_home)!r})
try:
    (Path.home() / ".gitconfig").read_text(encoding="utf-8")
except OSError:
    pass
else:
    raise AssertionError("scratch HOME unexpectedly exposed ~/.gitconfig")
try:
    Path({str(operator_git_config)!r}).read_text(encoding="utf-8")
except PermissionError:
    pass
else:
    raise AssertionError("operator Git config escaped the sandbox")
Path("result.txt").write_text("complete\\n")
subprocess.run(["git", "add", "result.txt"], check=True)
subprocess.run(["git", "commit", "-m", "complete work"], check=True)
"""
    if not seat._sandbox_available():
        pytest.skip("macOS sandbox-exec is unavailable on this host")
    with (
        patch.object(seat, "_sandbox_available", return_value=True),
        patch.object(seat.tempfile, "gettempdir", return_value=str(scratch)),
    ):
        environment, agent_home = seat.isolated_agent_env(
            "ACP Seat Worker", "acp-seat-worker@example.invalid",
            {
                "HOME": str(operator_home),
                "PATH": os.environ["PATH"],
                "LANG": "en_US.UTF-8",
                "ONBOARD_CENTRAL_TOKEN": "must-not-pass",
            },
        )
        command = seat.sandboxed_agent_command(
            [sys.executable, "-c", agent_code],
            work,
            protected_files=[operator_git_config],
        )

    assert Path(environment["HOME"]) == agent_home
    assert agent_home.parent == scratch
    assert "ONBOARD_CENTRAL_TOKEN" not in environment
    subprocess.run(
        command,
        cwd=work,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert git("log", "-1", "--format=%an <%ae>", cwd=work) == (
        "ACP Seat Worker <acp-seat-worker@example.invalid>"
    )
    commit = git("rev-parse", "--verify", "HEAD^{commit}", cwd=work)
    completion = {
        "summary": "sandboxed commit",
        "files_changed": ["result.txt"],
        "notes": "\n".join(
            [
                f"branch_and_commit: acp/seat-one-tk-sandbox-commit@{commit}",
                "test-command: git commit",
                "test-output: passed",
                "observations: standalone clone metadata stayed writable",
            ]
        ),
    }
    assert seat.validate_completion(work, completion) == completion


def test_sandbox_boundary_rejects_linked_worktree_metadata(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    git("init", "-b", "main", cwd=repository)
    git("config", "user.name", "ACP Test", cwd=repository)
    git("config", "user.email", "acp-test@example.invalid", cwd=repository)
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md", cwd=repository)
    git("commit", "-m", "base", cwd=repository)
    linked = tmp_path / "tickets" / "linked"
    linked.parent.mkdir()
    git("worktree", "add", "-b", "linked", str(linked), cwd=repository)
    scratch = tmp_path / "separate-scratch"
    scratch.mkdir()

    with (
        patch.object(seat, "_sandbox_available", return_value=True),
        patch.object(seat.tempfile, "gettempdir", return_value=str(scratch)),
    ):
        try:
            seat.sandboxed_agent_command(["/usr/bin/true"], linked)
        except RuntimeError as exc:
            assert "Git metadata escapes writable sandbox roots" in str(exc)
        else:
            raise AssertionError("linked worktree metadata escaped the sandbox guard")


def test_sandbox_profile_denies_network_and_protects_token(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    token = tmp_path / "seat.jwt"
    token.write_text("not-a-real-token", encoding="utf-8")
    with patch.object(seat, "_sandbox_available", return_value=True):
        command = seat.sandboxed_agent_command(
            ["/usr/bin/true"], work, protected_files=[token]
        )

    profile = command[2]
    assert "(deny network*)" in profile
    assert f'(allow file-write* (subpath "{work}"))' in profile
    assert f'(deny file-read* (literal "{token}"))' in profile


def test_isolated_agent_env_uses_scratch_home_and_explicit_allowlist(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = {
        "HOME": str(operator_home),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": "/operator/tmp",
        "TERM": "xterm-256color",
        "ONBOARD_AGENT_NAME": "acp-worker-1",
        "ONBOARD_BOARD_ID": "pursers",
        "ONBOARD_CENTRAL_TOKEN": "must-not-pass",
        "PURSERS_ROLE": "worker",
        "PURSERS_MODEL": "test-model",
        "PURSERS_BOARD_CONNECTOR_TOKEN": "must-not-pass",
        "SHELL": "/bin/zsh",
        "SSH_AUTH_SOCK": "/operator/agent.sock",
    }

    with patch.object(seat.tempfile, "gettempdir", return_value=str(scratch)):
        environment, agent_home = seat.isolated_agent_env(
            'ACP "Worker"', "acp-worker@example.invalid", source
        )

    assert agent_home.parent == scratch
    assert agent_home != operator_home
    assert environment == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(scratch),
        "TERM": "xterm-256color",
        "ONBOARD_AGENT_NAME": "acp-worker-1",
        "ONBOARD_BOARD_ID": "pursers",
        "PURSERS_ROLE": "worker",
        "PURSERS_MODEL": "test-model",
        "HOME": str(agent_home),
        "XDG_CONFIG_HOME": str(agent_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(agent_home / "gitconfig"),
    }
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((agent_home / "gitconfig").stat().st_mode) == 0o600
    assert not (agent_home / ".gitconfig").exists()
    configured = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^user\."],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert configured == [
        'user.name ACP "Worker"',
        "user.email acp-worker@example.invalid",
    ]


def test_sandbox_profile_allows_lexical_and_real_interpreter_prefixes(
    tmp_path: Path,
) -> None:
    opt_root = tmp_path / "opt"
    opt_root.mkdir()
    prefixes: list[tuple[Path, Path, Path]] = []
    for name in ("python@9", "agent"):
        real_prefix = tmp_path / "Cellar" / name / "9.0"
        real_executable = real_prefix / "bin" / name
        real_executable.parent.mkdir(parents=True)
        real_executable.write_text("fake executable\n", encoding="utf-8")
        lexical_prefix = opt_root / name
        lexical_prefix.symlink_to(real_prefix, target_is_directory=True)
        prefixes.append((lexical_prefix, real_prefix, lexical_prefix / "bin" / name))
    interpreter = prefixes[0][2]
    agent_executable = prefixes[1][2]
    work = tmp_path / "work"
    work.mkdir()

    with (
        patch.object(seat, "_sandbox_available", return_value=True),
        patch.object(seat.sys, "executable", str(interpreter)),
    ):
        profile = seat.sandboxed_agent_command([str(agent_executable)], work)[2]

    for lexical_prefix, real_prefix, _executable in prefixes:
        assert f'(allow file-read* (subpath "{lexical_prefix}"))' in profile
        assert f'(allow file-read* (subpath "{real_prefix}"))' in profile
    assert f'(allow file-read* (subpath "{opt_root}"))' not in profile
    assert '(allow file-read* (literal "/"))' in profile
    assert '(allow file-read* (subpath "/private/etc"))' in profile
    assert '(allow file-read* (subpath "/private/var/db"))' in profile
    assert '(allow file-read* (subpath "/private/var/select"))' in profile
    assert '(allow file-read* (literal "/var/select"))' in profile
    assert '(allow file-read-metadata (literal "/var"))' in profile
    assert '(allow file-read-metadata (literal "/private/var"))' in profile
    assert '(allow file-read-metadata (literal "/private"))' in profile
    assert '(allow file-read* file-write* (literal "/dev/null"))' in profile
    assert '(allow file-read* (literal "/etc"))' in profile
    for _lexical_prefix, _real_prefix, executable in prefixes:
        for path in (*executable.parents, *executable.resolve().parents):
            assert f'(allow file-read-metadata (literal "{path}"))' in profile


def test_offer_event_accepts_bridge_shapes() -> None:
    assert seat._offered_ticket_id(
        {"reason": "offer", "ticket_id": "TK-direct"}
    ) == "TK-direct"
    assert seat._offered_ticket_id(
        {"reason": "offer", "offer": {"ticket_id": "TK-nested"}}
    ) == "TK-nested"
    assert seat._offered_ticket_id({"reason": "timeout"}) is None


def test_parent_runtime_publishes_validated_branch(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git("init", "--bare", cwd=remote)
    work_root = tmp_path / "work"
    work, branch, commit = committed_worktree(work_root, "TK-publish")
    git("remote", "add", "origin", str(remote), cwd=work)
    completion = {
        "summary": "publish",
        "files_changed": ["result.txt"],
        "notes": "\n".join(
            [
                f"branch_and_commit: {branch}@{commit}",
                "test-command: true",
                "test-output: passed",
                "observations: local bare remote",
            ]
        ),
    }

    validated = seat.validate_completion(work, completion)
    seat.publish_branch(work, validated)

    assert git("rev-parse", f"refs/heads/{branch}", cwd=remote) == commit


def test_parent_runtime_publishes_clone_through_source_repository(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    git("init", "--bare", cwd=upstream)
    repository = tmp_path / "source"
    repository.mkdir()
    git("init", "-b", "main", cwd=repository)
    git("config", "user.name", "ACP Test", cwd=repository)
    git("config", "user.email", "acp-test@example.invalid", cwd=repository)
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "README.md", cwd=repository)
    git("commit", "-m", "base", cwd=repository)
    git("remote", "add", "origin", str(upstream), cwd=repository)
    git("push", "-u", "origin", "main", cwd=repository)
    runtime = seat.ACPSeatRuntime(
        FakeBoard(base_ticket("TK-publish-clone")),
        [sys.executable, str(FAKE)],
        tmp_path / "tickets",
        repository=repository,
        base_ref="main",
        seat_name="seat-one",
    )
    work = runtime._prepare_work_dir("TK-publish-clone")
    git("config", "user.name", "ACP Test", cwd=work)
    git("config", "user.email", "acp-test@example.invalid", cwd=work)
    (work / "result.txt").write_text("complete\n", encoding="utf-8")
    git("add", "result.txt", cwd=work)
    git("commit", "-m", "complete work", cwd=work)
    branch = git("branch", "--show-current", cwd=work)
    commit = git("rev-parse", "--verify", "HEAD^{commit}", cwd=work)
    completion = {
        "summary": "publish standalone clone",
        "files_changed": ["result.txt"],
        "notes": "\n".join(
            [
                f"branch_and_commit: {branch}@{commit}",
                "test-command: true",
                "test-output: passed",
                "observations: parent-only two-hop publication",
            ]
        ),
    }

    validated = seat.validate_completion(work, completion)
    seat.publish_branch(work, validated, publish_repository=repository)

    assert git("rev-parse", f"refs/heads/{branch}", cwd=upstream) == commit
