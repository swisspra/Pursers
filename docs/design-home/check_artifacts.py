#!/usr/bin/env python3
"""Validate design-home artifacts against the exact release baseline."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


BASELINE_SHA = "c2ebac5de803a0f7a00468ec4d3cdf06e4719096"
REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_HOME = REPO_ROOT / "docs" / "design-home"
MANIFEST_PATH = DESIGN_HOME / "context" / "source-manifest.json"


def fail(message: str) -> None:
    raise ValueError(message)


def checked_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        fail(f"unsafe path: {relative!r}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes repository: {relative}") from exc
    return path


def design_artifact(design_home: Path, relative: str) -> Path:
    prefix = "docs/design-home/"
    if not relative.startswith(prefix):
        fail(f"artifact path must be under docs/design-home: {relative}")
    return checked_path(design_home, relative.removeprefix(prefix))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        fail(f"JSON root must be an object: {path}")
    return value


def git_source(relative: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{BASELINE_SHA}:{relative}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        fail(f"baseline source missing: {relative}: {detail}")
    return result.stdout


def line_count(content: bytes) -> int:
    return content.count(b"\n")


def load_manifest(design_home: Path) -> dict[str, Any]:
    manifest = read_json(design_home / "context" / "source-manifest.json")
    if manifest.get("schemaVersion") != 1:
        fail("source-manifest schemaVersion must be 1")
    if manifest.get("baselineSha") != BASELINE_SHA:
        fail("source-manifest baselineSha mismatch")
    return manifest


def manifest_sources(manifest: dict[str, Any]) -> list[str]:
    paths = manifest.get("sourceInventory")
    if not isinstance(paths, list) or not paths:
        fail("sourceInventory must be a non-empty array")
    if not all(isinstance(path, str) and path for path in paths):
        fail("sourceInventory contains an invalid path")
    if len(paths) != len(set(paths)):
        fail("sourceInventory contains duplicate paths")
    return paths


def baseline_sources(manifest: dict[str, Any]) -> dict[str, bytes]:
    sources: dict[str, bytes] = {}
    for relative in manifest_sources(manifest):
        checked_path(REPO_ROOT, relative)
        baseline = git_source(relative)
        current = checked_path(REPO_ROOT, relative)
        if not current.is_file():
            fail(f"current source missing: {relative}")
        sources[relative] = baseline
    return sources


def render_excerpt(source_path: str, content: bytes, ranges: list[list[int]]) -> bytes:
    lines = content.decode("utf-8").splitlines()
    range_label = ",".join(f"{start}-{end}" for start, end in ranges)
    output = [
        f"SOURCE {source_path}",
        f"BASELINE {BASELINE_SHA}",
        f"RANGES {range_label}",
    ]
    for start, end in ranges:
        output.extend(["", f"===== {source_path}:{start}-{end} ====="])
        output.extend(lines[start - 1 : end])
    return ("\n".join(output) + "\n").encode()


def validate_literal_sources(
    design_home: Path,
    manifest: dict[str, Any],
    sources: dict[str, bytes],
) -> list[str]:
    errors: list[str] = []
    entries = manifest.get("literalSources")
    if not isinstance(entries, list) or not entries:
        return ["literalSources must be a non-empty array"]
    seen_sources: set[str] = set()
    seen_artifacts: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("literalSources entry must be an object")
            continue
        source_path = entry.get("sourcePath")
        artifact_path = entry.get("artifactPath")
        if source_path not in sources or not isinstance(artifact_path, str):
            errors.append(f"invalid literal source entry: {entry!r}")
            continue
        if source_path in seen_sources or artifact_path in seen_artifacts:
            errors.append(f"duplicate literal source entry: {source_path}")
            continue
        seen_sources.add(source_path)
        seen_artifacts.add(artifact_path)
        try:
            artifact = design_artifact(design_home, artifact_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not artifact.is_file():
            errors.append(f"literal artifact missing: {artifact_path}")
        elif artifact.read_bytes().splitlines() != sources[source_path].splitlines():
            errors.append(f"literal artifact differs from baseline lines: {artifact_path}")
        try:
            artifact.relative_to(design_home.resolve())
        except ValueError:
            errors.append(f"literal artifact outside design-home: {artifact_path}")
    return errors


def validate_excerpts(
    design_home: Path,
    manifest: dict[str, Any],
    sources: dict[str, bytes],
) -> list[str]:
    errors: list[str] = []
    entries = manifest.get("excerpts")
    if not isinstance(entries, list) or not entries:
        return ["excerpts must be a non-empty array"]
    seen_artifacts: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("excerpt entry must be an object")
            continue
        source_path = entry.get("sourcePath")
        artifact_path = entry.get("artifactPath")
        ranges = entry.get("ranges")
        if source_path not in sources or not isinstance(artifact_path, str):
            errors.append(f"invalid excerpt entry: {entry!r}")
            continue
        if artifact_path in seen_artifacts:
            errors.append(f"duplicate excerpt artifact: {artifact_path}")
            continue
        seen_artifacts.add(artifact_path)
        if not isinstance(ranges, list) or not ranges:
            errors.append(f"excerpt has no ranges: {artifact_path}")
            continue
        source_lines = line_count(sources[source_path])
        previous_end = 0
        normalized_ranges: list[list[int]] = []
        for item in ranges:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, int) for value in item)
            ):
                errors.append(f"invalid excerpt range: {artifact_path}: {item!r}")
                continue
            start, end = item
            if start < 1 or end < start or end > source_lines:
                errors.append(
                    f"excerpt range outside source: {source_path}:{start}-{end}"
                )
                continue
            if start <= previous_end:
                errors.append(f"excerpt ranges overlap or are unsorted: {artifact_path}")
                continue
            previous_end = end
            normalized_ranges.append([start, end])
        if len(normalized_ranges) != len(ranges):
            continue
        artifact = design_artifact(design_home, artifact_path)
        if not artifact.is_file():
            errors.append(f"excerpt artifact missing: {artifact_path}")
            continue
        expected = render_excerpt(source_path, sources[source_path], normalized_ranges)
        if artifact.read_bytes() != expected:
            errors.append(f"excerpt artifact differs from declared ranges: {artifact_path}")
    return errors


def find_method(module: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        fail(f"expected one {name} method, found {len(matches)}")
    return matches[0]


def normalized_route(value: str) -> str | None:
    if value == "/":
        return value
    if not value.startswith("/api/"):
        return None
    if value == "/api/config/jobs/([a-f0-9]{32})":
        return "/api/config/jobs/<hash>"
    if value == "/api/workers/([a-z0-9-]{2,32})/(test|start|stop|restart)":
        return "/api/workers/<name>/<action>"
    return value


def source_route_sets(source: bytes) -> dict[str, set[str]]:
    module = ast.parse(source.decode("utf-8"))
    result: dict[str, set[str]] = {}
    for method in ("do_GET", "do_POST"):
        node = find_method(module, method)
        routes = {
            route
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
            if (route := normalized_route(child.value)) is not None
        }
        result[method.removeprefix("do_")] = routes
    return result


def markdown_section(text: str, start: str, end: str | None) -> str:
    start_index = text.find(start)
    if start_index < 0:
        fail(f"missing markdown section: {start}")
    content_start = start_index + len(start)
    if end is None:
        return text[content_start:]
    end_index = text.find(end, content_start)
    if end_index < 0:
        fail(f"missing markdown section terminator: {end}")
    return text[content_start:end_index]


def markdown_routes(section: str) -> set[str]:
    return set(re.findall(r"^\| `(/[^`]*)` \|", section, flags=re.MULTILINE))


def validate_route_matrices(
    design_home: Path,
    sources: dict[str, bytes],
) -> list[str]:
    errors: list[str] = []
    routes_doc = (design_home / "context" / "routes.md").read_text(encoding="utf-8")
    source_sets = source_route_sets(sources["tools/fleet-dashboard/fleet_dashboard.py"])
    get_section = markdown_section(
        routes_doc, "### GET API endpoints", "### POST API endpoints"
    )
    post_section = markdown_section(
        routes_doc, "### POST API endpoints", "**Config routes set**"
    )
    documented = {
        "GET": markdown_routes(get_section),
        "POST": markdown_routes(post_section),
    }
    for method in ("GET", "POST"):
        missing = sorted(source_sets[method] - documented[method])
        extra = sorted(documented[method] - source_sets[method])
        if missing or extra:
            errors.append(
                f"{method} route matrix mismatch: missing={missing}, extra={extra}"
            )

    extension = json.loads(sources["tools/aionui-extension/aion-extension.json"])
    actual_extension = {
        (item["method"], item["path"])
        for item in extension["contributes"]["webui"]["apiRoutes"]
    }
    extension_section = markdown_section(
        routes_doc,
        "### API routes (from `aion-extension.json` webui.apiRoutes)",
        "### Static assets",
    )
    documented_extension = {
        (method, path)
        for path, method in re.findall(
            r"^\| `(/[^`]+)` \| (GET|POST) \|",
            extension_section,
            flags=re.MULTILINE,
        )
    }
    if actual_extension != documented_extension:
        errors.append(
            "extension route matrix mismatch: "
            f"missing={sorted(actual_extension - documented_extension)}, "
            f"extra={sorted(documented_extension - actual_extension)}"
        )
    return errors


def validate_source_counts(
    design_home: Path,
    manifest: dict[str, Any],
    sources: dict[str, bytes],
) -> list[str]:
    inventory = (design_home / "inventory.md").read_text(encoding="utf-8")
    section = markdown_section(
        inventory, "<!-- source-counts:start -->", "<!-- source-counts:end -->"
    )
    documented = {
        path: int(count)
        for path, count in re.findall(
            r"^\| `([^`]+)` \| (\d+) \|$", section, flags=re.MULTILINE
        )
    }
    expected_paths = set(manifest_sources(manifest))
    errors: list[str] = []
    if set(documented) != expected_paths:
        errors.append(
            "source count inventory path mismatch: "
            f"missing={sorted(expected_paths - set(documented))}, "
            f"extra={sorted(set(documented) - expected_paths)}"
        )
    for path in sorted(expected_paths & set(documented)):
        actual = line_count(sources[path])
        if documented[path] != actual:
            errors.append(
                f"source count mismatch: {path}: documented={documented[path]}, actual={actual}"
            )
    return errors


def validate_acceptance_inventory(
    design_home: Path,
    manifest: dict[str, Any],
) -> list[str]:
    inventory = (design_home / "inventory.md").read_text(encoding="utf-8")
    documented = re.findall(
        r"^\| `((?:dashboard-ui|fleet-dashboard|extension-join|personal-mcp)\.[a-z0-9.-]+)` \|",
        inventory,
        flags=re.MULTILINE,
    )
    expected = manifest.get("acceptanceInventoryIds")
    if not isinstance(expected, list) or not all(
        isinstance(item, str) and item for item in expected
    ):
        return ["acceptanceInventoryIds must be a non-empty string array"]
    errors: list[str] = []
    if len(documented) != len(set(documented)):
        errors.append("acceptance inventory contains duplicate IDs")
    if len(expected) != len(set(expected)):
        errors.append("acceptanceInventoryIds contains duplicate IDs")
    missing = sorted(set(expected) - set(documented))
    extra = sorted(set(documented) - set(expected))
    if missing or extra:
        errors.append(
            f"acceptance inventory mismatch: missing={missing}, extra={extra}"
        )
    return errors


def validate_artifact_manifest(
    design_home: Path,
    manifest: dict[str, Any],
) -> tuple[list[str], tuple[int, int]]:
    errors: list[str] = []
    entries = manifest.get("artifactManifest")
    expected_total = manifest.get("artifactTotal")
    if not isinstance(entries, list) or not entries:
        return ["artifactManifest must be a non-empty array"], (0, 0)
    if not isinstance(expected_total, dict):
        return ["artifactTotal must be an object"], (0, 0)
    seen: set[str] = set()
    total_lines = 0
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("artifactManifest entry must be an object")
            continue
        path = entry.get("path")
        expected_lines = entry.get("lines")
        expected_bytes = entry.get("bytes")
        if (
            not isinstance(path, str)
            or not isinstance(expected_lines, int)
            or not isinstance(expected_bytes, int)
        ):
            errors.append(f"invalid artifactManifest entry: {entry!r}")
            continue
        if path in seen:
            errors.append(f"duplicate artifact manifest path: {path}")
            continue
        seen.add(path)
        artifact = design_artifact(design_home, path)
        if not artifact.is_file():
            errors.append(f"manifest artifact missing: {path}")
            continue
        content = artifact.read_bytes()
        actual_lines = line_count(content)
        actual_bytes = len(content)
        total_lines += actual_lines
        total_bytes += actual_bytes
        if actual_lines != expected_lines or actual_bytes != expected_bytes:
            errors.append(
                f"artifact count mismatch: {path}: "
                f"expected={expected_lines} lines/{expected_bytes} bytes, "
                f"actual={actual_lines} lines/{actual_bytes} bytes"
            )
    if expected_total.get("excludedPath") != "docs/design-home/context/source-manifest.json":
        errors.append("artifactTotal must exclude source-manifest.json")
    if expected_total.get("lines") != total_lines or expected_total.get("bytes") != total_bytes:
        errors.append(
            "artifact total mismatch: "
            f"expected={expected_total.get('lines')} lines/{expected_total.get('bytes')} bytes, "
            f"actual={total_lines} lines/{total_bytes} bytes"
        )
    return errors, (total_lines, total_bytes)


def validate_no_stale_labels(design_home: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted((design_home / "context").glob("*.md")) + [
        design_home / "inventory.md"
    ]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"\b(?:line|lines|range|ranges)\b[^\n]{0,50}~\d", text, re.I):
            errors.append(f"approximate source range remains: {path.relative_to(REPO_ROOT)}")
        for phrase in (
            "Full CSS (layout-relevant excerpts)",
            "full HTML document with inline",
            "full SPA JavaScript",
            "full source available",
        ):
            if phrase in text:
                errors.append(
                    f"excerpt mislabeled as full source: {path.relative_to(REPO_ROOT)}: {phrase}"
                )
    return errors


def validate(design_home: Path = DESIGN_HOME) -> tuple[list[str], tuple[int, int]]:
    try:
        manifest = load_manifest(design_home)
        sources = baseline_sources(manifest)
        errors = []
        errors.extend(validate_literal_sources(design_home, manifest, sources))
        errors.extend(validate_excerpts(design_home, manifest, sources))
        errors.extend(validate_route_matrices(design_home, sources))
        errors.extend(validate_source_counts(design_home, manifest, sources))
        errors.extend(validate_acceptance_inventory(design_home, manifest))
        errors.extend(validate_no_stale_labels(design_home))
        manifest_errors, total = validate_artifact_manifest(design_home, manifest)
        errors.extend(manifest_errors)
        return errors, total
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return [str(exc)], (0, 0)


def assert_probe(name: str, errors: list[str], expected_fragment: str) -> None:
    if not any(expected_fragment in error for error in errors):
        fail(f"{name} probe was not detected: {errors}")
    print(f"PROBE {name}: detected {expected_fragment}")


def run_negative_probes() -> None:
    clean_errors, _ = validate()
    if clean_errors:
        fail(f"clean validation failed before probes: {clean_errors}")
    with tempfile.TemporaryDirectory(prefix="design-home-probe-") as temp_dir:
        disposable = Path(temp_dir) / "design-home"
        shutil.copytree(DESIGN_HOME, disposable)

        routes_path = disposable / "context" / "routes.md"
        routes = routes_path.read_text(encoding="utf-8")
        marker = "| `/api/intake` | Submit/decide intake |"
        if marker not in routes:
            fail("route probe marker missing")
        routes_path.write_text(
            routes.replace(marker, "| `/api/intake-probe` | Submit/decide intake |", 1),
            encoding="utf-8",
        )
        route_errors, _ = validate(disposable)
        assert_probe("route-method", route_errors, "POST route matrix mismatch")

        shutil.copy2(DESIGN_HOME / "context" / "routes.md", routes_path)
        manifest_path = disposable / "context" / "source-manifest.json"
        manifest = read_json(manifest_path)
        manifest["excerpts"][0]["ranges"][0][1] += 1
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        range_errors, _ = validate(disposable)
        assert_probe(
            "count-range",
            range_errors,
            "excerpt artifact differs from declared ranges",
        )

        shutil.copy2(DESIGN_HOME / "context" / "source-manifest.json", manifest_path)
        restored_errors, _ = validate(disposable)
        if restored_errors:
            fail(f"restored disposable copy did not pass: {restored_errors}")
        print("PROBE restore: clean disposable copy passes")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--negative-probes", action="store_true")
    args = parser.parse_args()
    errors, total = validate()
    print(f"Artifact manifest total: {total[0]} lines, {total[1]} bytes")
    if errors:
        print("FAIL")
        for error in errors:
            print(f"  {error}")
        return 1
    print("PASS: baseline sources, literal copies, excerpts, matrices, counts, and manifest")
    if args.negative_probes:
        try:
            run_negative_probes()
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        print("PASS: disposable negative probes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
