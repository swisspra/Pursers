#!/usr/bin/env python3
"""Fail when a produced artifact has no explicit delivery disposition."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("delivery-manifest.toml")
RELEASE_VERSIONS = Path("tools/release_versions.toml")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "fixtures",
    "node_modules",
    "tests",
    "vendor",
}
NON_PRODUCT_CONTENT_ROOTS = {
    ".superdesign",
    "docs",
    "docs-local",
    "planning",
}


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    source: str
    entry_point: str | None = None


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


_TRACKED_CACHE: dict[Path, frozenset[str] | None] = {}


def _tracked_files(root: Path) -> frozenset[str] | None:
    """Return the git-tracked paths under root, or None outside a work tree.

    Only a tracked file can be delivered. Walking the filesystem instead lets
    anything a build step leaves in the checkout count as an artifact: CI
    creates a venv at .ci/isolated-wheel-imports and each of its files failed
    as an unregistered operator tool. The git top-level must be root itself,
    so a fixture that happens to sit inside another repository is still
    walked in full rather than silently losing its files.
    """
    if root not in _TRACKED_CACHE:
        tracked: frozenset[str] | None = None
        try:
            top = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            if top and Path(top).resolve() == root:
                listed = subprocess.run(
                    ["git", "-C", str(root), "ls-files", "-z"],
                    capture_output=True, check=True,
                ).stdout
                tracked = frozenset(
                    entry.decode("utf-8") for entry in listed.split(b"\0") if entry
                )
        except (OSError, subprocess.CalledProcessError):
            tracked = None
        _TRACKED_CACHE[root] = tracked
    return _TRACKED_CACHE[root]


def _ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in IGNORED_PARTS for part in relative.parts):
        return True
    tracked = _tracked_files(root)
    return tracked is not None and relative.as_posix() not in tracked


def discover_artifacts(root: Path) -> dict[str, Artifact]:
    """Discover artifact-shaped files without consulting the manifest."""
    root = root.resolve()
    found: dict[str, Artifact] = {}

    def add(
        artifact_id: str,
        kind: str,
        path: Path,
        *,
        entry_point: str | None = None,
    ) -> None:
        relative = _relative(path, root)
        artifact = Artifact(artifact_id, kind, relative, entry_point)
        previous = found.get(artifact_id)
        if previous is not None and previous != artifact:
            raise ValueError(
                f"discovery produced duplicate id {artifact_id!r}: "
                f"{previous.source!r}, {relative!r}"
            )
        found[artifact_id] = artifact

    for path in sorted(root.rglob("pyproject.toml")):
        if _ignored(path, root):
            continue
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot inspect {path}: {exc}") from exc
        project = document.get("project")
        name = project.get("name") if isinstance(project, dict) else None
        if isinstance(name, str) and name:
            add(f"python-distribution:{name}", "python-distribution", path)

            scripts = project.get("scripts")
            if scripts is None:
                continue
            if not isinstance(scripts, dict):
                raise ValueError(
                    f"{_relative(path, root)}: project.scripts must be a table"
                )
            for script_name, target in sorted(scripts.items()):
                if not isinstance(script_name, str) or not script_name:
                    raise ValueError(
                        f"{_relative(path, root)}: project.scripts has an invalid name"
                    )
                if not isinstance(target, str) or ":" not in target:
                    raise ValueError(
                        f"{_relative(path, root)}: project.scripts.{script_name} "
                        "must name module:callable"
                    )
                module, callable_name = target.split(":", 1)
                if not module or not callable_name:
                    raise ValueError(
                        f"{_relative(path, root)}: project.scripts.{script_name} "
                        "must name module:callable"
                    )
                module_path = Path(*module.split("."))
                candidates = (
                    path.parent / f"{module_path}.py",
                    path.parent / module_path / "__init__.py",
                    path.parent / "src" / f"{module_path}.py",
                    path.parent / "src" / module_path / "__init__.py",
                    path.parent / f"{module_path.name}.py",
                )
                sources = sorted(
                    {candidate for candidate in candidates if candidate.is_file()}
                )
                if len(sources) != 1:
                    rendered = [_relative(candidate, root) for candidate in sources]
                    raise ValueError(
                        f"{_relative(path, root)}: cannot resolve project.scripts."
                        f"{script_name} target {target!r} to exactly one source; "
                        f"found {rendered!r}"
                    )
                add(
                    f"python-console-script:{name}:{script_name}",
                    "python-console-script",
                    sources[0],
                    entry_point=target,
                )

    for path in sorted(root.rglob("package.json")):
        relative_parts = path.relative_to(root).parts
        if (
            _ignored(path, root)
            or relative_parts[0] in NON_PRODUCT_CONTENT_ROOTS
        ):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot inspect {path}: {exc}") from exc
        name = document.get("name") if isinstance(document, dict) else None
        if isinstance(name, str) and name:
            add(f"node-application:{name}", "node-application", path)

    for path in sorted(root.rglob("aion-extension.json")):
        relative_parts = path.relative_to(root).parts
        if (
            _ignored(path, root)
            or relative_parts[0] in NON_PRODUCT_CONTENT_ROOTS
        ):
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        name = document.get("name")
        if isinstance(name, str) and name:
            add(f"host-extension:{name}", "host-extension", path)

    for path in sorted(root.rglob("*.html")):
        relative_parts = path.relative_to(root).parts
        if (
            _ignored(path, root)
            or relative_parts[0] in NON_PRODUCT_CONTENT_ROOTS
        ):
            continue
        relative = _relative(path, root)
        add(f"web-surface:{relative}", "web-surface", path)

    tools_root = root / "tools"
    for path in sorted(root.rglob("*")):
        relative_parts = path.relative_to(root).parts
        if (
            _ignored(path, root)
            or relative_parts[0] in NON_PRODUCT_CONTENT_ROOTS
        ):
            continue
        if path.is_symlink() and not path.exists():
            raise ValueError(
                f"{_relative(path, root)}: dangling symlink cannot be "
                "classified; repair or remove its target"
            )
        if not path.is_file():
            continue
        if relative_parts[0] == "packages" or "src" in relative_parts:
            continue
        is_executable = bool(path.stat().st_mode & 0o111)
        with path.open("rb") as stream:
            has_shebang = stream.read(2) == b"#!"
        if path.suffix == ".py":
            text = path.read_text(encoding="utf-8", errors="replace")
            if (
                path.parent != tools_root
                and "__main__" not in text
                and not is_executable
            ):
                continue
        elif not is_executable and not has_shebang:
            continue
        relative = _relative(path, root)
        add(f"operator-tool:{relative}", "operator-tool", path)

    generated = {
        "release-artifact:aionui-extension-zip": Path(
            "tools/aionui-extension/build.py"
        ),
        "release-artifact:home-runtime-wheelhouse": Path(
            "tools/build_home_runtime_wheelhouse.py"
        ),
    }
    for artifact_id, relative in generated.items():
        path = root / relative
        if path.is_file():
            add(artifact_id, "release-artifact", path)

    fleet = root / "tools/fleet-dashboard/fleet_dashboard.py"
    if fleet.is_file():
        add("web-surface:fleet-dashboard", "web-surface", fleet)

    return found


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a TOML table")
    return document


def _version_map(root: Path) -> tuple[str, dict[str, str]]:
    document = _read_toml(root / RELEASE_VERSIONS)
    product = document.get("product")
    packages = document.get("packages")
    if not isinstance(product, str) or not isinstance(packages, dict):
        raise ValueError(f"{RELEASE_VERSIONS}: invalid release version map")
    aliases = {
        "pursers": "pursers",
        "pursers-central": "central",
        "pursers-client": "client",
        "pursers-personal": "personal",
        "pursers-personal-import": "import",
        "pursers-wait-bridge": "wait_bridge",
        "pursers-acp": "acp",
    }
    resolved = {
        distribution: str(packages[key])
        for distribution, key in aliases.items()
        if key in packages
    }
    return product, resolved


def validate(root: Path, manifest_path: Path = MANIFEST) -> list[str]:
    root = root.resolve()
    document = _read_toml(root / manifest_path)
    failures: list[str] = []
    if document.get("schema_version") != 1:
        failures.append(f"{manifest_path}: schema_version must equal 1")

    raw_rows = document.get("artifacts")
    if not isinstance(raw_rows, list):
        failures.append(f"{manifest_path}: artifacts must be an array of tables")
        raw_rows = []
    rows: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            failures.append(f"{manifest_path}: artifacts[{index}] must be a table")
            continue
        artifact_id = row.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            failures.append(f"{manifest_path}: artifacts[{index}].id is required")
            continue
        if artifact_id in rows:
            failures.append(f"{manifest_path}: duplicate artifact id {artifact_id!r}")
            continue
        rows[artifact_id] = row

    discovered = discover_artifacts(root)
    for artifact_id in sorted(discovered.keys() - rows.keys()):
        artifact = discovered[artifact_id]
        failures.append(
            f"unregistered artifact: {artifact_id} ({artifact.source}); add a "
            "delivered row or an explicit exemption"
        )
    for artifact_id in sorted(rows.keys() - discovered.keys()):
        if rows[artifact_id].get("kind") != "external-dependency":
            failures.append(f"stale manifest row: {artifact_id} is not discoverable")

    for artifact_id, row in sorted(rows.items()):
        artifact = discovered.get(artifact_id)
        if artifact is not None:
            if row.get("kind") != artifact.kind:
                failures.append(
                    f"{artifact_id}: kind {row.get('kind')!r} != {artifact.kind!r}"
                )
            if row.get("source") != artifact.source:
                failures.append(
                    f"{artifact_id}: source {row.get('source')!r} != "
                    f"{artifact.source!r}"
                )
            if (
                artifact.entry_point is not None
                and row.get("entry_point") != artifact.entry_point
            ):
                failures.append(
                    f"{artifact_id}: entry_point {row.get('entry_point')!r} != "
                    f"{artifact.entry_point!r}"
                )
        state = row.get("state")
        if state == "exempt":
            reason = row.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 20:
                failures.append(f"{artifact_id}: exemption requires a specific reason")
            continue
        if state != "delivered":
            failures.append(f"{artifact_id}: state must be delivered or exempt")
            continue
        channel = row.get("channel")
        evidence = row.get("evidence")
        if not isinstance(channel, str) or not channel.strip():
            failures.append(f"{artifact_id}: delivered row requires channel")
        elif re.search(r"\bon main\b", channel, flags=re.IGNORECASE):
            failures.append(f"{artifact_id}: 'on main' is not a delivery channel")
        if not isinstance(evidence, list) or not evidence:
            failures.append(f"{artifact_id}: delivered row requires evidence paths")
            continue
        evidence_text = ""
        for raw_path in evidence:
            if not isinstance(raw_path, str) or not raw_path:
                failures.append(f"{artifact_id}: invalid evidence path {raw_path!r}")
                continue
            path = root / raw_path
            if not path.is_file():
                failures.append(
                    f"{artifact_id}: delivery channel is stale; evidence path "
                    f"does not exist: {raw_path}"
                )
                continue
            evidence_text += "\n" + path.read_text(encoding="utf-8", errors="replace")
        if isinstance(channel, str) and "workflow" in channel.lower() and not any(
            isinstance(path, str) and path.startswith(".github/workflows/")
            for path in evidence
        ):
            failures.append(
                f"{artifact_id}: workflow channel requires checked-in workflow evidence"
            )
        if "DRAFT ONLY" in evidence_text:
            failures.append(
                f"{artifact_id}: delivery channel is stale; draft-only evidence "
                "does not deliver an artifact"
            )
        contains = row.get("evidence_contains", [])
        if not isinstance(contains, list) or not all(
            isinstance(token, str) and token for token in contains
        ):
            failures.append(f"{artifact_id}: evidence_contains must be strings")
        else:
            for token in contains:
                if token not in evidence_text:
                    failures.append(
                        f"{artifact_id}: delivery channel is stale; evidence does "
                        f"not contain {token!r}"
                    )

    product_version, package_versions = _version_map(root)
    for artifact in discovered.values():
        if artifact.kind != "python-distribution":
            continue
        name = artifact.artifact_id.removeprefix("python-distribution:")
        expected = package_versions.get(name)
        if expected is None:
            continue
        project = _read_toml(root / artifact.source).get("project")
        actual = project.get("version") if isinstance(project, dict) else None
        if actual != expected:
            failures.append(
                f"{artifact.source}: version {actual!r} != release manifest "
                f"version {expected!r} for {name}"
            )

    surfaces = document.get("version_surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        failures.append(f"{manifest_path}: version_surfaces must be non-empty")
        surfaces = []
    for index, surface in enumerate(surfaces):
        if not isinstance(surface, dict):
            failures.append(f"version_surfaces[{index}] must be a table")
            continue
        relative = surface.get("path")
        prefix = surface.get("prefix")
        key = surface.get("version_key")
        if not all(isinstance(item, str) and item for item in (relative, prefix, key)):
            failures.append(f"version_surfaces[{index}] requires path/prefix/version_key")
            continue
        path = root / relative
        if not path.is_file():
            failures.append(f"version surface does not exist: {relative}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(re.escape(prefix) + f"(?P<version>{VERSION.pattern})", text)
        if match is None:
            failures.append(f"{relative}: version prefix {prefix!r} was not found")
            continue
        expected = product_version if key == "product" else package_versions.get(key)
        if expected is None:
            failures.append(f"{relative}: unknown version_key {key!r}")
            continue
        actual = match.group("version")
        if actual != expected:
            failures.append(
                f"{relative}: self-described version {actual!r} != "
                f"{key} {expected!r}"
            )
        if surface.get("reject_other_product_versions") is True:
            product_major = re.escape(product_version.split(".")[0])
            product_family = re.compile(
                rf"\b{product_major}\.\d+\.\d+(?:(?:a|b|rc)\d+)?\b"
            )
            mismatches = sorted(set(product_family.findall(text)) - {product_version})
            if mismatches:
                failures.append(
                    f"{relative}: stale product versions remain visible: {mismatches!r}"
                )

    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--list", action="store_true", dest="list_artifacts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_artifacts:
        for artifact in discover_artifacts(args.repository).values():
            print(f"{artifact.artifact_id}\t{artifact.kind}\t{artifact.source}")
        return 0
    try:
        failures = validate(args.repository)
    except ValueError as exc:
        print(f"delivery manifest error: {exc}")
        return 1
    if failures:
        print("delivery manifest errors:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("delivery manifest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
