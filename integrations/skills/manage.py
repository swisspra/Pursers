#!/usr/bin/env python3
"""Preview, install, check, or remove the Pursers skill bundle safely."""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


BUNDLE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = BUNDLE_ROOT / "manifest.json"
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BundleError(RuntimeError):
    """A safe bundle operation cannot continue."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read bundle manifest: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise BundleError("unsupported bundle manifest schema")
    skills = manifest.get("skills")
    hosts = manifest.get("hosts")
    if not isinstance(skills, list) or not skills:
        raise BundleError("bundle manifest has no skills")
    if not isinstance(hosts, dict) or not hosts:
        raise BundleError("bundle manifest has no hosts")
    seen: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict):
            raise BundleError("bundle manifest skill entries must be objects")
        name = skill.get("name")
        relative = skill.get("path")
        files = skill.get("files")
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise BundleError(f"invalid skill name: {name!r}")
        if name in seen:
            raise BundleError(f"duplicate skill name: {name}")
        seen.add(name)
        if relative != name:
            raise BundleError(f"skill path must match name: {name}")
        if not isinstance(files, list) or "SKILL.md" not in files:
            raise BundleError(f"skill {name} must list SKILL.md")
        source = BUNDLE_ROOT / relative
        actual = sorted(
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
        )
        if actual != sorted(files):
            raise BundleError(
                f"skill {name} files differ from manifest: "
                f"expected={sorted(files)!r} actual={actual!r}"
            )
        if any(path.is_symlink() for path in source.rglob("*")):
            raise BundleError(f"skill {name} contains a symlink")
    return manifest


def _safe_target(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    filesystem_root = Path(resolved.anchor)
    if (
        resolved == filesystem_root
        or resolved.parent == filesystem_root
        or resolved.name != "skills"
    ):
        raise BundleError("target must be a dedicated directory named 'skills'")
    return resolved


def resolve_target(args: argparse.Namespace, manifest: dict[str, Any]) -> Path:
    if args.target is not None:
        return _safe_target(args.target)
    host = manifest["hosts"].get(args.host)
    if not isinstance(host, dict):
        raise BundleError(f"unsupported host: {args.host}")
    relative = host.get(args.scope)
    if not isinstance(relative, str):
        raise BundleError(f"host {args.host} does not support scope {args.scope}")
    if args.scope == "project":
        base = args.project_root.resolve()
    else:
        base = (args.user_home or Path.home()).expanduser().resolve()
    return _safe_target(base / relative)


def _files(root: Path) -> list[Path]:
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def trees_equal(source: Path, destination: Path) -> bool:
    if not destination.is_dir() or destination.is_symlink():
        return False
    source_files = _files(source)
    if source_files != _files(destination):
        return False
    return all(
        not (destination / relative).is_symlink()
        and filecmp.cmp(source / relative, destination / relative, shallow=False)
        for relative in source_files
    )


def build_plan(manifest: dict[str, Any], target: Path) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    for skill in manifest["skills"]:
        name = skill["name"]
        source = BUNDLE_ROOT / skill["path"]
        destination = target / name
        if not destination.exists() and not destination.is_symlink():
            status = "absent"
            action = "install"
        elif trees_equal(source, destination):
            status = "current"
            action = "none"
        else:
            status = "conflict"
            action = "preserve"
        plan.append(
            {
                "skill": name,
                "status": status,
                "action": action,
                "destination": str(destination),
            }
        )
    return plan


def _print_result(
    command: str,
    manifest: dict[str, Any],
    target: Path,
    plan: list[dict[str, str]],
    *,
    applied: bool,
    backup: Path | None = None,
) -> None:
    result: dict[str, Any] = {
        "command": command,
        "schema_version": manifest["schema_version"],
        "bundle_version": manifest["bundle_version"],
        "target": str(target),
        "applied": applied,
        "skills": plan,
    }
    if backup is not None:
        result["backup"] = str(backup)
    print(json.dumps(result, indent=2, sort_keys=True))


def install(
    manifest: dict[str, Any],
    target: Path,
    *,
    apply: bool,
    replace_conflicts: bool,
) -> int:
    plan = build_plan(manifest, target)
    conflicts = [item for item in plan if item["status"] == "conflict"]
    if conflicts and not replace_conflicts:
        _print_result("install", manifest, target, plan, applied=False)
        print(
            "refusing to replace conflicting skill directories; rerun with "
            "--replace-conflicts to preserve them in a backup",
            file=sys.stderr,
        )
        return 2
    if not apply:
        _print_result("install", manifest, target, plan, applied=False)
        return 0

    target.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if conflicts:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target / ".pursers-backups" / stamp
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = target / ".pursers-backups" / f"{stamp}-{suffix}"
        backup.mkdir(parents=True)

    for item in plan:
        if item["status"] == "current":
            continue
        name = item["skill"]
        source = BUNDLE_ROOT / name
        destination = target / name
        if item["status"] == "conflict":
            assert backup is not None
            shutil.move(str(destination), str(backup / name))
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=target))
        try:
            shutil.copytree(source, staging, dirs_exist_ok=True)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    final = build_plan(manifest, target)
    _print_result("install", manifest, target, final, applied=True, backup=backup)
    return 0


def remove(manifest: dict[str, Any], target: Path, *, apply: bool) -> int:
    plan = build_plan(manifest, target)
    conflicts = [item for item in plan if item["status"] == "conflict"]
    if conflicts:
        _print_result("remove", manifest, target, plan, applied=False)
        print(
            "refusing to remove modified or unknown skill directories; preserve "
            "them and resolve the conflict manually",
            file=sys.stderr,
        )
        return 2
    removal_plan = [
        {**item, "action": "remove" if item["status"] == "current" else "none"}
        for item in plan
    ]
    if not apply:
        _print_result("remove", manifest, target, removal_plan, applied=False)
        return 0
    for item in plan:
        if item["status"] == "current":
            shutil.rmtree(target / item["skill"])
    _print_result(
        "remove", manifest, target, build_plan(manifest, target), applied=True
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "install", "check", "remove"))
    parser.add_argument("--host", required=True, choices=("zed", "codex", "goose"))
    parser.add_argument("--scope", choices=("project", "user"), default="project")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--user-home", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-conflicts", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest()
        target = resolve_target(args, manifest)
        plan = build_plan(manifest, target)
        if args.command == "plan":
            _print_result("plan", manifest, target, plan, applied=False)
            return 0
        if args.command == "check":
            _print_result("check", manifest, target, plan, applied=False)
            return 0 if all(item["status"] == "current" for item in plan) else 1
        if args.command == "install":
            return install(
                manifest,
                target,
                apply=args.apply,
                replace_conflicts=args.replace_conflicts,
            )
        return remove(manifest, target, apply=args.apply)
    except BundleError as exc:
        print(f"skill bundle error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
