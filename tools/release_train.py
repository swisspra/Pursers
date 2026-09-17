#!/usr/bin/env python3
"""Bump or verify every release-version consumer from one TOML manifest."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable

try:
    from .release_versions import (
        PACKAGE_KEYS,
        TOOLCHAIN_KEYS,
        ReleaseVersions,
        load_versions,
    )
except ImportError:  # Direct execution.
    from release_versions import (
        PACKAGE_KEYS,
        TOOLCHAIN_KEYS,
        ReleaseVersions,
        load_versions,
    )


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools/release_versions.toml"
VERSION_FILES: dict[str, tuple[str, ...]] = {
    "product": (
        "packages/personal/pyproject.toml",
        "packages/personal/src/pursers_personal/__init__.py",
        "packages/personal/tests/test_apps_contract.py",
        "packages/personal/src/pursers_personal/resources/dashboard.html",
        "packages/pursers/pyproject.toml",
        "tools/dashboard-ui/src/dashboard.ts",
        "tools/seat-kit/README.md",
    ),
    "central": (
        "packages/central/pyproject.toml",
        "packages/personal/pyproject.toml",
        "packages/personal/tests/test_apps_contract.py",
        "packages/pursers/pyproject.toml",
    ),
    "client": (
        "packages/central/pyproject.toml",
        "packages/client/pyproject.toml",
        "packages/personal/pyproject.toml",
        "packages/personal/src/pursers_personal/apps_server.py",
        "packages/personal/tests/test_apps_contract.py",
        "packages/pursers/pyproject.toml",
        "tools/seat-kit/README.md",
        "tools/wait-bridge/pyproject.toml",
        "tools/wait-bridge/tests/test_seat_admin.py",
    ),
    "import": (
        "packages/import/PERSONAL-IMPORT.md",
        "packages/import/personal_import.py",
        "packages/import/pyproject.toml",
        "packages/import/src/pursers_personal_import/__init__.py",
        "packages/pursers/pyproject.toml",
    ),
    "wait_bridge": (
        "tools/fleet-dashboard/tests/test_fleet_dashboard.py",
        "tools/fleet-dashboard/tests/test_seat_config.py",
        "tools/seat-kit/README.md",
        "tools/wait-bridge/pursers_wait_server.py",
        "tools/wait-bridge/pyproject.toml",
    ),
}

# These documents describe one installable release cohort. Every package key in the
# manifest belongs to each document; deriving the keys at runtime means a newly added
# component cannot silently retain a stale version here.
COHORT_VERSION_FILES = (
    "README.md",
    "docs-local/architecture-th.html",
    "docs-local/manual-en.html",
    "docs-local/manual-th.html",
    "docs-local/whats-new.html",
)

WHATS_NEW_HISTORY_HEADING = re.compile(
    r"(?m)^\s*<h3>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)? · \d{4}-\d{2}-\d{2}</h3>\s*$"
)
VERSION_REFERENCE = r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?"

LOCKED_SOURCE_PACKAGES = {
    "pursers-central": ("packages/central/src", "pursers_central"),
    "pursers-client": ("packages/client/src", "pursers_client"),
}


class ReleaseTrainError(RuntimeError):
    pass


def _manifest_text(versions: ReleaseVersions) -> str:
    lines = [
        "schema_version = 1",
        f'product = "{versions.product}"',
        f'source_date_epoch = "{versions.source_date_epoch}"',
        "",
        "[packages]",
    ]
    lines.extend(f'{key} = "{versions.packages[key]}"' for key in PACKAGE_KEYS)
    lines.extend(("", "[build_toolchain]"))
    lines.extend(
        f'{key} = "{versions.build_toolchain[key]}"' for key in TOOLCHAIN_KEYS
    )
    return "\n".join(lines) + "\n"


def _alpha_next(value: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)a(\d+)", value)
    if match is None:
        raise ReleaseTrainError(f"cannot apply patch-alpha to {value!r}")
    return f"{match[1]}.{match[2]}.{match[3]}a{int(match[4]) + 1}"


def bumped_versions(
    current: ReleaseVersions,
    assignments: Iterable[str],
    next_kind: str | None,
) -> ReleaseVersions:
    packages = dict(current.packages)
    product = current.product
    if next_kind:
        if next_kind != "patch-alpha":
            raise ReleaseTrainError(f"unsupported --next value: {next_kind}")
        product = _alpha_next(product)
        packages = {key: _alpha_next(value) for key, value in packages.items()}
    for assignment in assignments:
        if "=" not in assignment:
            raise ReleaseTrainError(f"--set expects KEY=VERSION, got {assignment!r}")
        key, value = assignment.split("=", 1)
        if not value:
            raise ReleaseTrainError(f"empty version for {key!r}")
        if key == "product":
            product = value
            packages["pursers"] = value
            packages["personal"] = value
        elif key in packages:
            packages[key] = value
        else:
            raise ReleaseTrainError(f"unknown release component: {key}")
    if packages["pursers"] != product or packages["personal"] != product:
        raise ReleaseTrainError("product, pursers, and personal versions must match")
    return replace(current, product=product, packages=packages)


def _replace_versions(
    root: Path,
    current: ReleaseVersions,
    target: ReleaseVersions,
) -> dict[Path, str]:
    planned: dict[Path, str] = {}
    values = {"product": (current.product, target.product)}
    if current.packages.keys() != target.packages.keys():
        raise ReleaseTrainError("release package key set changed during a bump")
    values.update(
        (key, (old, target.packages[key]))
        for key, old in current.packages.items()
    )
    by_path: dict[str, list[str]] = {}
    for key, paths in VERSION_FILES.items():
        for path in paths:
            by_path.setdefault(path, []).append(key)
    for relative in COHORT_VERSION_FILES:
        by_path[relative] = list(values)
    for relative, keys in by_path.items():
        path = root / relative
        original = path.read_text(encoding="utf-8")
        editable, history = _version_reference_regions(relative, original)
        updated = editable
        replacements: dict[str, str] = {}
        for key in keys:
            old, new = values[key]
            if old != new:
                previous = replacements.setdefault(old, new)
                if previous != new:
                    raise ReleaseTrainError(
                        f"ambiguous replacement in {relative}: {old} -> {previous}/{new}"
                    )
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        updated += history
        if updated != original:
            planned[path] = updated
    return planned


def _version_reference_regions(relative: str, text: str) -> tuple[str, str]:
    """Return the current-reference region and protected release history."""
    if relative != "docs-local/whats-new.html":
        return text, ""
    match = WHATS_NEW_HISTORY_HEADING.search(text)
    if match is None:
        raise ReleaseTrainError(
            "docs-local/whats-new.html: release history boundary is missing"
        )
    return text[: match.start()], text[match.start() :]


def _package_distribution(root: Path, key: str) -> str:
    candidates = (
        root / "packages" / key / "pyproject.toml",
        root / "tools" / key.replace("_", "-") / "pyproject.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(_pyproject(candidate)["project"]["name"])
    raise ReleaseTrainError(f"no project metadata found for package key {key!r}")


def _component_aliases(distribution: str, key: str) -> tuple[str, ...]:
    """Return reference labels derived from manifest keys and project metadata."""
    words = distribution.split("-")
    aliases = {
        distribution,
        distribution.replace("-", "_"),
        distribution.replace("-", " "),
        key.replace("_", " "),
    }
    if words[:1] == ["pursers"] and len(words) > 1:
        aliases.add(" ".join(words[1:]))
    return tuple(sorted(aliases, key=len, reverse=True))


def _visible_reference_line(line: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", line))


def _component_version_references(
    root: Path,
    versions: ReleaseVersions,
    text: str,
) -> dict[str, list[tuple[str, int]]]:
    references: dict[str, list[tuple[str, int]]] = {
        key: [] for key in versions.packages
    }
    forward_separator = r"(?:\s|==|[-:=/·,()])+"
    reverse_separator = r"\s+"
    for key in versions.packages:
        distribution = _package_distribution(root, key)
        alias = "(?:" + "|".join(
            re.escape(value) for value in _component_aliases(distribution, key)
        ) + ")"
        forward = re.compile(
            rf"(?<![\w-]){alias}(?![\w]){forward_separator}(?P<version>{VERSION_REFERENCE})(?![\w])",
            re.IGNORECASE,
        )
        reverse = re.compile(
            rf"(?<![\w])(?P<version>{VERSION_REFERENCE})(?![\w]){reverse_separator}{alias}(?![\w-])",
            re.IGNORECASE,
        )
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            line = _visible_reference_line(raw_line)
            seen: set[tuple[int, int]] = set()
            for pattern in (forward, reverse):
                for match in pattern.finditer(line):
                    if match.span() in seen:
                        continue
                    seen.add(match.span())
                    references[key].append((match.group("version"), line_number))
    return references


def _cohort_version_errors(root: Path, versions: ReleaseVersions) -> list[str]:
    errors: list[str] = []
    for relative in COHORT_VERSION_FILES:
        try:
            current, _history = _version_reference_regions(
                relative,
                (root / relative).read_text(encoding="utf-8"),
            )
            references = _component_version_references(root, versions, current)
        except ReleaseTrainError as exc:
            errors.append(str(exc))
            continue
        correct_versions: set[str] = set()
        for key, expected in versions.packages.items():
            distribution = _package_distribution(root, key)
            for actual, line_number in references[key]:
                if actual == expected:
                    correct_versions.add(expected)
                else:
                    errors.append(
                        f"{relative}:{line_number}: {distribution} reference "
                        f"{actual} != {key} version {expected}"
                    )
        # Product, pursers, and personal intentionally share one version. Requiring
        # each distinct manifest value keeps combined labels valid while ensuring a
        # component's version cannot be supplied by another component's label.
        for key, expected in {"product": versions.product, **versions.packages}.items():
            if expected not in correct_versions:
                errors.append(f"{relative}: missing bound {key} version {expected}")
    return errors


def _release_summary(versions: ReleaseVersions) -> str:
    package = versions.packages
    return (
        f"This release includes `pursers-central=={package['central']}`,\n"
        f"`pursers-client=={package['client']}`, "
        f"`pursers-personal-import=={package['import']}`,\n"
        f"`pursers-personal=={package['personal']}`, "
        f"`pursers=={package['pursers']}`, and\n"
        f"`pursers-wait-bridge=={package['wait_bridge']}`.\n"
    )


def _plan_changelog(root: Path, target: ReleaseVersions) -> str:
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    marker = "## [Unreleased]"
    start = text.find(marker)
    if start < 0:
        raise ReleaseTrainError("CHANGELOG.md has no [Unreleased] section")
    body_start = start + len(marker)
    next_heading = text.find("\n## [", body_start)
    if next_heading < 0:
        raise ReleaseTrainError("CHANGELOG.md has no released section")
    unreleased = text[body_start:next_heading].strip()
    body = _release_summary(target)
    if unreleased:
        body += "\n" + unreleased + "\n"
    return (
        text[:body_start]
        + "\n\n"
        + f"## [{target.product}] - {date.today().isoformat()}\n\n"
        + body
        + text[next_heading:]
    )


def _update_view_attestation(root: Path, planned: dict[Path, str]) -> None:
    view_path = root / "packages/personal/src/pursers_personal/resources/dashboard.html"
    view = planned.get(view_path, view_path.read_text(encoding="utf-8")).encode()
    digest = hashlib.sha256(view).hexdigest()
    size = len(view)
    generator = root / "tools/regenerate_component_lock.py"
    generator_text = planned.get(generator, generator.read_text(encoding="utf-8"))
    generator_text = re.sub(
        r'EXPECTED_VIEW_SHA256 = \(\n\s*"[0-9a-f]{64}"\n\)',
        f'EXPECTED_VIEW_SHA256 = (\n    "{digest}"\n)',
        generator_text,
    )
    generator_text = re.sub(
        r"EXPECTED_VIEW_SIZE = \d+", f"EXPECTED_VIEW_SIZE = {size}", generator_text
    )
    planned[generator] = generator_text
    test_path = root / "packages/personal/tests/test_apps_contract.py"
    test_text = planned.get(test_path, test_path.read_text(encoding="utf-8"))
    test_text = re.sub(
        r'expected = "[0-9a-f]{64}"', f'expected = "{digest}"', test_text, count=1
    )
    test_text = re.sub(
        r"assert len\(payload\) == \d+", f"assert len(payload) == {size}", test_text, count=1
    )
    planned[test_path] = test_text


def plan_bump(
    root: Path,
    current: ReleaseVersions,
    target: ReleaseVersions,
) -> dict[Path, str]:
    planned = _replace_versions(root, current, target)
    planned[root / "tools/release_versions.toml"] = _manifest_text(target)
    if current.product != target.product:
        planned[root / "CHANGELOG.md"] = _plan_changelog(root, target)
    _update_view_attestation(root, planned)
    return {
        path: content
        for path, content in planned.items()
        if path.read_text(encoding="utf-8") != content
    }


def _diff(root: Path, planned: dict[Path, str]) -> str:
    chunks: list[str] = []
    for path in sorted(planned):
        relative = path.relative_to(root).as_posix()
        chunks.extend(
            difflib.unified_diff(
                path.read_text(encoding="utf-8").splitlines(keepends=True),
                planned[path].splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    return "".join(chunks)


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _component_source_lock_errors(root: Path, lock: dict) -> list[str]:
    """Compare locked wheel members with the source bytes that produce them."""
    errors: list[str] = []
    components = lock.get("components", {})
    for distribution, (source_relative, import_name) in LOCKED_SOURCE_PACKAGES.items():
        source_root = root / source_relative / import_name
        if not source_root.is_dir():
            continue
        locked = components.get(distribution, {}).get("members", {})
        prefix = f"{import_name}/"
        locked_source = {
            member: digest
            for member, digest in locked.items()
            if member.startswith(prefix)
        }
        actual_source = {
            f"{import_name}/{path.relative_to(source_root).as_posix()}": hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(source_root.rglob("*"))
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        }
        for member in sorted(actual_source.keys() - locked_source.keys()):
            errors.append(
                f"component-lock.json: {distribution} source member missing: {member}"
            )
        for member in sorted(locked_source.keys() - actual_source.keys()):
            errors.append(
                f"component-lock.json: {distribution} locked member has no source: {member}"
            )
        for member in sorted(actual_source.keys() & locked_source.keys()):
            if actual_source[member] != locked_source[member]:
                errors.append(
                    f"component-lock.json: {distribution} source digest mismatch: {member}"
                )
    return errors


def check(root: Path, versions: ReleaseVersions) -> list[str]:
    errors: list[str] = []
    package = versions.packages
    projects = {
        "packages/pursers/pyproject.toml": ("pursers", "pursers"),
        "packages/central/pyproject.toml": ("pursers-central", "central"),
        "packages/client/pyproject.toml": ("pursers-client", "client"),
        "packages/personal/pyproject.toml": ("pursers-personal", "personal"),
        "packages/import/pyproject.toml": ("pursers-personal-import", "import"),
        "tools/wait-bridge/pyproject.toml": ("pursers-wait-bridge", "wait_bridge"),
    }
    for relative, (name, key) in projects.items():
        document = _pyproject(root / relative)
        actual = document["project"]["version"]
        if document["project"]["name"] != name or actual != package[key]:
            errors.append(f"{relative}: expected {name}=={package[key]}, found {actual}")
    dependencies = {
        "packages/pursers/pyproject.toml": ("central", "client", "personal", "import"),
        "packages/central/pyproject.toml": ("client",),
        "packages/personal/pyproject.toml": ("central", "client"),
        "tools/wait-bridge/pyproject.toml": ("client",),
    }
    distributions = {
        "central": "pursers-central",
        "client": "pursers-client",
        "personal": "pursers-personal",
        "import": "pursers-personal-import",
    }
    for relative, keys in dependencies.items():
        actual = set(_pyproject(root / relative)["project"].get("dependencies", []))
        for key in keys:
            expected = f"{distributions[key]}=={package[key]}"
            if expected not in actual:
                errors.append(f"{relative}: missing {expected}")
    for key, paths in VERSION_FILES.items():
        expected = versions.product if key == "product" else package[key]
        for relative in paths:
            if expected not in (root / relative).read_text(encoding="utf-8"):
                errors.append(f"{relative}: missing {key} version {expected}")
    errors.extend(_cohort_version_errors(root, versions))
    bridge_source = root / "tools/wait-bridge/pursers_wait_server.py"
    bridge_module = ast.parse(bridge_source.read_text(encoding="utf-8"))
    source_version = next(
        (
            node.value.value
            for node in bridge_module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SOURCE_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ),
        None,
    )
    if source_version != package["wait_bridge"]:
        errors.append(
            "tools/wait-bridge/pursers_wait_server.py: "
            f"SOURCE_VERSION {source_version!r} != {package['wait_bridge']!r}"
        )
    lock_path = root / "packages/personal/src/pursers_personal/resources/component-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("product_version") != versions.product:
        errors.append("component-lock.json: product_version mismatch")
    if lock.get("build_toolchain") != dict(versions.build_toolchain):
        errors.append("component-lock.json: build_toolchain mismatch")
    for key, distribution in (
        ("central", "pursers-central"),
        ("client", "pursers-client"),
    ):
        actual = lock.get("components", {}).get(distribution, {}).get("version")
        if actual != package[key]:
            errors.append(f"component-lock.json: {distribution} version mismatch")
    view = root / "packages/personal/src/pursers_personal/resources/dashboard.html"
    payload = view.read_bytes()
    if lock.get("view", {}).get("size_bytes") != len(payload):
        errors.append("component-lock.json: dashboard size mismatch")
    if lock.get("view", {}).get("sha256") != hashlib.sha256(payload).hexdigest():
        errors.append("component-lock.json: dashboard hash mismatch")
    errors.extend(_component_source_lock_errors(root, lock))
    return errors


def _regenerate_lock(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pursers-release-wheels-") as temporary:
        subprocess.run(
            [
                sys.executable,
                str(root / "tools/regenerate_component_lock.py"),
                "--wheel-dir",
                temporary,
            ],
            cwd=root,
            check=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="fail if a version consumer drifted")
    bump = subparsers.add_parser("bump", help="rewrite all version consumers")
    bump.add_argument("--set", action="append", default=[], metavar="KEY=VERSION")
    bump.add_argument("--next", choices=("patch-alpha",))
    bump.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = load_versions(MANIFEST)
    if args.command == "check":
        errors = check(ROOT, current)
        if errors:
            print("release version drift:", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"release versions OK: product={current.product}")
        return 0
    try:
        target = bumped_versions(current, args.set, args.next)
        planned = plan_bump(ROOT, current, target)
    except (OSError, ValueError, ReleaseTrainError) as exc:
        print(f"release_train: {exc}", file=sys.stderr)
        return 2
    if not planned:
        print("release train already matches tools/release_versions.toml")
        return 0
    print(_diff(ROOT, planned), end="")
    if args.dry_run:
        return 0
    for path, content in planned.items():
        path.write_text(content, encoding="utf-8")
    _regenerate_lock(ROOT)
    errors = check(ROOT, target)
    if errors:
        raise ReleaseTrainError("post-bump check failed: " + "; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
