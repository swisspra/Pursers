from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from tools.check_delivery_manifest import discover_artifacts


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "integrations/skills"
MANAGER = BUNDLE / "manage.py"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))


def run_manager(target: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            *arguments,
            "--host",
            "zed",
            "--target",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_manifest_names_files_frontmatter_and_canonical_guides() -> None:
    assert MANIFEST["schema_version"] == 1
    assert MANIFEST["bundle_version"] == "1.0.0"
    assert [item["name"] for item in MANIFEST["skills"]] == [
        "pursers-start",
        "pursers-work",
        "pursers-review",
        "pursers-operate",
    ]
    for item in MANIFEST["skills"]:
        skill_root = BUNDLE / item["path"]
        actual = sorted(
            path.relative_to(skill_root).as_posix()
            for path in skill_root.rglob("*")
            if path.is_file()
        )
        assert actual == sorted(item["files"])
        text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(
            r"\A---\nname: (?P<name>[^\n]+)\ndescription: (?P<description>[^\n]+)\n---\n",
            text,
        )
        assert match is not None
        assert match.group("name") == item["name"]
        assert 40 <= len(match.group("description")) <= 1024
    for relative in MANIFEST["canonical_guides"]:
        assert (ROOT / relative).is_file(), relative


def test_delivery_inventory_discovers_skill_bundle_and_manager() -> None:
    artifacts = discover_artifacts(ROOT)
    assert artifacts["skill-bundle:pursers"].source == "integrations/skills/manifest.json"
    assert artifacts["operator-tool:integrations/skills/manage.py"].source == (
        "integrations/skills/manage.py"
    )


def test_preview_install_check_idempotent_and_remove(tmp_path: Path) -> None:
    target = tmp_path / "project/.agents/skills"
    preview = run_manager(target, "plan")
    assert preview.returncode == 0, preview.stderr
    assert {item["status"] for item in json.loads(preview.stdout)["skills"]} == {
        "absent"
    }
    assert not target.exists()

    dry_install = run_manager(target, "install")
    assert dry_install.returncode == 0, dry_install.stderr
    assert not target.exists()

    installed = run_manager(target, "install", "--apply")
    assert installed.returncode == 0, installed.stderr
    assert all(
        (target / item["name"] / "SKILL.md").is_file()
        for item in MANIFEST["skills"]
    )

    repeated = run_manager(target, "install", "--apply")
    assert repeated.returncode == 0, repeated.stderr
    assert {item["status"] for item in json.loads(repeated.stdout)["skills"]} == {
        "current"
    }
    checked = run_manager(target, "check")
    assert checked.returncode == 0, checked.stderr

    remove_preview = run_manager(target, "remove")
    assert remove_preview.returncode == 0, remove_preview.stderr
    assert (target / "pursers-start").is_dir()
    removed = run_manager(target, "remove", "--apply")
    assert removed.returncode == 0, removed.stderr
    assert all(not (target / item["name"]).exists() for item in MANIFEST["skills"])


def test_conflict_is_preserved_and_explicit_replace_creates_backup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project/.agents/skills"
    conflict = target / "pursers-work"
    conflict.mkdir(parents=True)
    original = "user-owned\n"
    (conflict / "SKILL.md").write_text(original, encoding="utf-8")

    refused = run_manager(target, "install", "--apply")
    assert refused.returncode == 2
    assert (conflict / "SKILL.md").read_text(encoding="utf-8") == original
    assert not (target / "pursers-start").exists()

    replaced = run_manager(target, "install", "--apply", "--replace-conflicts")
    assert replaced.returncode == 0, replaced.stderr
    payload = json.loads(replaced.stdout)
    backup = Path(payload["backup"])
    assert (backup / "pursers-work/SKILL.md").read_text(encoding="utf-8") == original
    assert run_manager(target, "check").returncode == 0


def test_remove_refuses_modified_bundle_without_partial_deletion(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project/.agents/skills"
    assert run_manager(target, "install", "--apply").returncode == 0
    modified = target / "pursers-review/SKILL.md"
    modified.write_text(modified.read_text(encoding="utf-8") + "\nlocal edit\n")

    result = run_manager(target, "remove", "--apply")
    assert result.returncode == 2
    assert modified.is_file()
    assert (target / "pursers-start/SKILL.md").is_file()


def test_manager_rejects_a_broad_target(tmp_path: Path) -> None:
    broad_targets = [tmp_path / "project", Path(Path.cwd().anchor) / "skills"]
    for target in broad_targets:
        result = run_manager(target, "plan")
        assert result.returncode == 2
        assert "dedicated directory named 'skills'" in result.stderr


def test_project_targets_match_each_host_contract(tmp_path: Path) -> None:
    expected = {
        "zed": ".agents/skills",
        "codex": ".agents/skills",
        "goose": ".agents/skills",
    }
    project = tmp_path / "project"
    for host, relative in expected.items():
        result = subprocess.run(
            [
                sys.executable,
                str(MANAGER),
                "plan",
                "--host",
                host,
                "--scope",
                "project",
                "--project-root",
                str(project),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert Path(json.loads(result.stdout)["target"]) == project / relative

    user_home = tmp_path / "home"
    codex_user = subprocess.run(
        [
            sys.executable,
            str(MANAGER),
            "plan",
            "--host",
            "codex",
            "--scope",
            "user",
            "--user-home",
            str(user_home),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert codex_user.returncode == 0, codex_user.stderr
    assert Path(json.loads(codex_user.stdout)["target"]) == (
        user_home / ".agents/skills"
    )
