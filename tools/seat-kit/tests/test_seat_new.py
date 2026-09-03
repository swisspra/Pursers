from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("seat_new", ROOT / "seat_new.py")
assert SPEC and SPEC.loader
seat_new = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seat_new)


def args(tmp_path: Path, *, role: str = "worker", repo: str | None = None):
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
            "codex",
            *(["--repo", repo] if repo else []),
        ]
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


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
    assert "reviewers never claim/submit/write code/push" in (
        reviewer / "AGENTS.md"
    ).read_text()


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
