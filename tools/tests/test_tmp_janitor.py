from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import tmp_janitor  # noqa: E402


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def idle_runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
    if Path(command[0]).name == "lsof":
        return completed(returncode=1)
    if command[0] == "/bin/ps":
        return completed(stdout="1 /sbin/launchd\n")
    raise AssertionError(command)


def make_old(path: Path, *, seconds: int = 7_200) -> None:
    timestamp = time.time() - seconds
    for current in [*path.rglob("*"), path]:
        os.utime(current, (timestamp, timestamp), follow_symlinks=False)


def test_in_use_check_refuses_a_live_directory(tmp_path: Path) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir(parents=True)
    (candidate / "payload").write_text("data", encoding="utf-8")
    make_old(candidate)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; f=open(sys.argv[1]); print('ready', flush=True); time.sleep(30)",
            str(candidate / "payload"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        inspection = tmp_janitor.inspect_candidate(
            candidate, older_than_seconds=3_600
        )
        assert inspection.selected is False
        assert "in use" in inspection.reason
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_delete_refuses_process_with_cwd_exactly_at_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    make_old(candidate)
    environment = os.environ.copy()
    for name in ("TMPDIR", "TMP", "TEMP", "TEMPDIR", "PYTEST_DEBUG_TEMPROOT"):
        environment.pop(name, None)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        cwd=candidate,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert tmp_janitor.run(
            [candidate], older_than_seconds=3_600, delete=True
        ) == 0
        assert candidate.is_dir()
        output = capsys.readouterr().out
        assert f"SKIP {candidate}" in output
        assert "in use" in output
        assert "working directory" in output
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_partial_lsof_positive_result_is_active(tmp_path: Path) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    make_old(candidate)
    calls = 0

    def partial_runner(
        command: list[str], **_kwargs: object
    ) -> SimpleNamespace:
        nonlocal calls
        if Path(command[0]).name == "lsof":
            calls += 1
            if calls == 2:
                return completed(returncode=1, stdout="p123\nfcwd\n")
        return idle_runner(command)

    result = tmp_janitor.inspect_candidate(
        candidate, older_than_seconds=3_600, runner=partial_runner
    )

    assert result.selected is False
    assert "inside the root" in result.reason


def test_dry_run_deletes_nothing_and_reports_exact_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    (candidate / "payload").write_bytes(b"payload")
    make_old(candidate)

    assert tmp_janitor.run(
        [candidate], older_than_seconds=3_600, delete=False, runner=idle_runner
    ) == 0

    assert candidate.is_dir()
    output = capsys.readouterr().out
    assert f"WOULD_DELETE {candidate}" in output
    assert "would_reclaim_bytes=" in output
    assert "candidates=1" in output


def test_age_rule_selects_only_old_trees(tmp_path: Path) -> None:
    old = tmp_path / "old"
    recent = tmp_path / "recent"
    old.mkdir()
    recent.mkdir()
    (old / "payload").write_text("old", encoding="utf-8")
    (recent / "payload").write_text("recent", encoding="utf-8")
    now = time.time()
    make_old(old, seconds=7_200)

    old_result = tmp_janitor.inspect_candidate(
        old, older_than_seconds=3_600, now=now, runner=idle_runner
    )
    recent_result = tmp_janitor.inspect_candidate(
        recent, older_than_seconds=3_600, now=now, runner=idle_runner
    )

    assert old_result.selected is True
    assert recent_result.selected is False
    assert "only" in recent_result.reason


def test_symlink_root_is_never_selected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    result = tmp_janitor.inspect_candidate(
        link, older_than_seconds=0, runner=idle_runner
    )

    assert result.selected is False
    assert "symlink" in result.reason


def test_unknown_process_state_fails_closed(tmp_path: Path) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    make_old(candidate)

    def unavailable(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("probe unavailable")

    result = tmp_janitor.inspect_candidate(
        candidate, older_than_seconds=3_600, runner=unavailable
    )

    assert result.selected is False
    assert "cannot run in-use probes" in result.reason


def test_live_temp_environment_refuses_dormant_directory() -> None:
    candidate = (
        Path.home()
        / ".cache"
        / "pursers-tmp-janitor-tests"
        / f"dormant-{os.getpid()}-{time.time_ns()}"
    )
    candidate.mkdir(parents=True)
    make_old(candidate)

    environment = os.environ.copy()
    environment["TMPDIR"] = str(candidate)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        result = tmp_janitor.inspect_candidate(
            candidate, older_than_seconds=3_600
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)
        shutil.rmtree(candidate, ignore_errors=True)

    assert result.selected is False
    assert f"live pid {holder.pid}" in result.reason


def test_delete_refuses_ambiguous_live_temp_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = tmp_path / "seat cache"
    candidate.mkdir()
    make_old(candidate)

    def live_environment_runner(
        command: list[str], **_kwargs: object
    ) -> SimpleNamespace:
        if Path(command[0]).name == "lsof":
            return completed(returncode=1)
        if command[0] == "/bin/ps":
            return completed(stdout=f"123 runner TMPDIR={candidate}\n")
        raise AssertionError(command)

    assert tmp_janitor.run(
        [candidate],
        older_than_seconds=3_600,
        delete=True,
        runner=live_environment_runner,
    ) == 0

    assert candidate.is_dir()
    output = capsys.readouterr().out
    assert f"SKIP {candidate}" in output
    assert "ambiguous in process environment output" in output
    assert "reclaimed_bytes=0 candidates=0" in output


def test_temp_environment_comparison_normalizes_lexical_aliases(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    make_old(candidate)
    lexical_alias = candidate.parent / "alias-parent" / ".." / candidate.name

    def live_environment_runner(
        command: list[str], **_kwargs: object
    ) -> SimpleNamespace:
        if Path(command[0]).name == "lsof":
            return completed(returncode=1)
        if command[0] == "/bin/ps":
            return completed(stdout=f"123 runner TMPDIR={lexical_alias}\n")
        raise AssertionError(command)

    result = tmp_janitor.inspect_candidate(
        candidate,
        older_than_seconds=3_600,
        runner=live_environment_runner,
    )

    assert result.selected is False
    assert "live pid 123" in result.reason


def test_temp_environment_comparison_resolves_whitespace_symlink_alias(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    make_old(candidate)
    alias = tmp_path / "seat alias"
    alias.symlink_to(candidate, target_is_directory=True)

    def live_environment_runner(
        command: list[str], **_kwargs: object
    ) -> SimpleNamespace:
        if Path(command[0]).name == "lsof":
            return completed(returncode=1)
        if command[0] == "/bin/ps":
            return completed(stdout=f"123 runner TMPDIR={alias} PATH=/bin\n")
        raise AssertionError(command)

    result = tmp_janitor.inspect_candidate(
        candidate,
        older_than_seconds=3_600,
        runner=live_environment_runner,
    )

    assert result.selected is False
    assert "live pid 123" in result.reason


def test_exited_owner_pid_does_not_pin_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    (candidate / tmp_janitor.OWNER_FILE).write_text("987654321\n", encoding="ascii")
    make_old(candidate)

    def exited(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(tmp_janitor.os, "kill", exited)
    result = tmp_janitor.inspect_candidate(
        candidate, older_than_seconds=3_600, runner=idle_runner
    )

    assert result.selected is True


def test_delete_unlinks_internal_symlink_without_touching_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected"
    protected.write_text("keep", encoding="utf-8")
    candidate = tmp_path / "seat-cache"
    candidate.mkdir()
    (candidate / "outside-link").symlink_to(outside, target_is_directory=True)
    make_old(candidate)

    assert tmp_janitor.run(
        [candidate], older_than_seconds=3_600, delete=True, runner=idle_runner
    ) == 0

    assert not candidate.exists()
    assert protected.read_text(encoding="utf-8") == "keep"
    output = capsys.readouterr().out
    assert f"DELETED {candidate}" in output
    assert "reclaimed_bytes=" in output
