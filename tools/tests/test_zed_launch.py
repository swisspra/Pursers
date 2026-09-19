from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.zed.check_registry import (
    RegistryCheckError,
    pr_body,
    require_no_git_lfs,
    registry_stanza,
    write_operator_artifacts,
)
from tools.zed.export_extension import ExportError, export_extension


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _fixture_source(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@invalid")

    (repo / "README.md").write_text("Pursers fixture\n", encoding="utf-8")
    _commit(repo, "Initial fixture")

    extension = repo / "integrations/zed/pursers-mcp"
    extension.mkdir(parents=True)
    (extension / "LICENSE").write_text("Apache License fixture\n", encoding="utf-8")
    (extension / "extension.toml").write_text(
        'id = "pursers-mcp"\nname = "Pursers MCP"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (extension / "src").mkdir()
    (extension / "src/lib.rs").write_text("pub fn ready() {}\n", encoding="utf-8")
    _commit(repo, "Add extension fixture")

    (extension / "src/lib.rs").write_text("pub fn ready() -> bool { true }\n", encoding="utf-8")
    _commit(repo, "Update extension fixture")

    (repo / "README.md").write_text("Unrelated source change\n", encoding="utf-8")
    _commit(repo, "Unrelated fixture change")
    return repo


def test_export_is_deterministic_and_keeps_only_extension_history(
    tmp_path: Path,
) -> None:
    source = _fixture_source(tmp_path)
    source_head = _git(source, "rev-parse", "HEAD")

    first = export_extension(source, source_head, tmp_path / "first")
    second = export_extension(source, source_head, tmp_path / "second")

    assert first["source_commit"] == source_head
    assert first["export_commit"] == second["export_commit"]
    assert first["export_tree"] == second["export_tree"]
    assert first["files"] == ["LICENSE", "extension.toml", "src/lib.rs"]
    assert int(_git(tmp_path / "first", "rev-list", "--count", "HEAD")) == 2
    assert _git(source, "status", "--porcelain") == ""


def test_export_refuses_nonempty_output(tmp_path: Path) -> None:
    source = _fixture_source(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ExportError, match="not empty"):
        export_extension(source, "HEAD", output)


def test_export_rejects_escaping_prefix(tmp_path: Path) -> None:
    source = _fixture_source(tmp_path)
    with pytest.raises(ExportError, match="repository-relative"):
        export_extension(source, "HEAD", tmp_path / "output", prefix=Path("../x"))


def test_export_rejects_option_like_revision(tmp_path: Path) -> None:
    source = _fixture_source(tmp_path)
    with pytest.raises(ExportError, match="must not start"):
        export_extension(source, "--all", tmp_path / "output")


@pytest.mark.parametrize("version", ["v1.0.0", "01.0.0", "1.0.0-beta", "1.0"])
def test_registry_stanza_rejects_non_registry_semver(version: str) -> None:
    with pytest.raises(RegistryCheckError, match="invalid extension version"):
        registry_stanza("pursers-mcp", version)


def test_registry_stanza_rejects_invalid_id() -> None:
    with pytest.raises(RegistryCheckError, match="invalid extension ID"):
        registry_stanza("Pursers MCP", "0.1.0")


def test_git_lfs_filter_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "export"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / ".gitattributes").write_text("*.wasm filter=lfs diff=lfs\n", encoding="utf-8")

    with pytest.raises(RegistryCheckError, match="Git LFS filter"):
        require_no_git_lfs(repo)


def test_registry_stanza_is_exact() -> None:
    assert registry_stanza("pursers-mcp", "0.1.0") == (
        "[pursers-mcp]\n"
        'submodule = "extensions/pursers-mcp"\n'
        'version = "0.1.0"\n'
    )


def test_operator_artifacts_are_short_and_exact(tmp_path: Path) -> None:
    sha = "a" * 40
    output = tmp_path / "dist"
    write_operator_artifacts(
        output,
        extension_id="pursers-mcp",
        version="0.1.0",
        source_commit=sha,
        repository_url="https://github.com/swisspra/pursers-zed.git",
    )

    assert (output / "extensions.toml").read_text(encoding="utf-8") == registry_stanza(
        "pursers-mcp", "0.1.0"
    )
    body = (output / "PR_BODY.md").read_text(encoding="utf-8")
    assert f"swisspra/pursers-zed@{sha}" in body
    assert "uvx --from pursers-client pursers-mcp" in body
    assert "Apache-2.0" in body
    assert body == pr_body(
        source_commit=sha,
        version="0.1.0",
        repository_url="https://github.com/swisspra/pursers-zed.git",
    )
