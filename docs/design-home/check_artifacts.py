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
import unicodedata
from pathlib import Path
from typing import Any


BASELINE_SHA = "c2ebac5de803a0f7a00468ec4d3cdf06e4719096"
REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_HOME = REPO_ROOT / "docs" / "design-home"
MANIFEST_PATH = DESIGN_HOME / "context" / "source-manifest.json"
ACCEPTANCE_FACTS_PATH = DESIGN_HOME / "context" / "acceptance-facts.json"
TYPED_PREDICATE_DELTA_PATH = (
    DESIGN_HOME / "context" / "typed-predicate-integration-delta.json"
)
TYPED_PREDICATE_BASE = "594ec7b0fab83ae7ed1c5a5fe80d216d17cd930c"


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


def normalized_fact(value: str) -> str:
    """Case- and whitespace-normalised form used for every fact comparison."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


# Exhaustive key sets. AN-000000000360 authorises behaviour predicates for IDs that
# name behaviour rather than a rendered label, and prior_state for the transitions
# ground C requires. Every shape below is checked with set equality, never a subset
# test, so an unknown key can never pass unnoticed.
CONJUNCT_KEYS: dict[str, set[str]] = {
    "ax_name_contains": {"kind", "path", "expected"},
    "http_response": {"kind", "request", "status", "body_contains"},
    "mcp_tool_response": {"kind", "tool", "field", "expected"},
    "state_transition": {"kind", "from", "to", "via"},
    "receipt_field": {"kind", "receipt", "field", "expected"},
    "log_assertion": {"kind", "stream", "expected"},
    "prior_state": {"kind", "observation", "expected"},
}
FIELDED_CONJUNCT_KEYS: dict[str, set[str]] = {
    "http_response": {"kind", "source_id", "assertions"},
    "mcp_tool_response": {"kind", "source_id", "assertions"},
    "state_transition": {"kind", "source_id", "assertions"},
    "receipt_field": {"kind", "source_id", "assertions"},
    "log_assertion": {"kind", "source_id", "assertions"},
}
FIELDED_ASSERTION_KEYS: dict[str, set[str]] = {
    "http_response": {"target", "path", "op", "value"},
    "mcp_tool_response": {"path", "op", "value"},
    "state_transition": {"phase", "path", "op", "value"},
    "receipt_field": {"path", "op", "value"},
    "log_assertion": {"path", "op", "value"},
}
TYPED_OPERATORS = {"eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"}
DERIVATION_KEYS: dict[str, set[str]] = {
    "literal": {"kind", "source_quote"},
    "identifier": {"kind", "symbol"},
    # AN-000000000368 keeps the three failures apart. A missing evidence collector is
    # not a missing behaviour, and neither is a missing permission. Both gap kinds
    # block acceptance; only the owner and the fix differ.
    "collector_gap": {"kind", "owner", "action"},
    "observed_gap": {"kind", "owner", "action"},
    "typed_predicate": {
        "kind", "proposal_id", "source_commit", "source_lines",
    },
}
BLOCKING_DERIVATIONS = {"collector_gap", "observed_gap"}


def conjunct_signature(identifier: str, conjunct: Any) -> tuple[str, ...]:
    """Validate one conjunct exhaustively and return its canonical signature."""
    if not isinstance(conjunct, dict) or conjunct.get("kind") not in CONJUNCT_KEYS:
        fail(f"acceptance fact {identifier} has an unknown predicate conjunct kind")
    kind = conjunct["kind"]
    if kind in FIELDED_CONJUNCT_KEYS and set(conjunct) == FIELDED_CONJUNCT_KEYS[kind]:
        source_id = conjunct["source_id"]
        assertions = conjunct["assertions"]
        if not isinstance(source_id, str) or not source_id.strip():
            fail(f"acceptance fact {identifier} {kind} source_id is empty")
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 64:
            fail(f"acceptance fact {identifier} {kind} assertions are empty or unbounded")
        signatures: list[str] = []
        for assertion in assertions:
            if not isinstance(assertion, dict) or set(assertion) != FIELDED_ASSERTION_KEYS[kind]:
                fail(f"acceptance fact {identifier} {kind} assertion fields do not match schema")
            path = assertion["path"]
            op = assertion["op"]
            if not isinstance(path, str) or not isinstance(op, str) or op not in TYPED_OPERATORS:
                fail(f"acceptance fact {identifier} {kind} assertion path/operator is invalid")
            if kind == "http_response":
                target = assertion["target"]
                if target not in {"status", "action_origin", "field"}:
                    fail(f"acceptance fact {identifier} http_response target is invalid")
                if (target == "field") != path.startswith("/") or (target != "field" and path):
                    fail(f"acceptance fact {identifier} http_response assertion path is invalid")
            elif kind == "state_transition":
                if assertion["phase"] not in {"before", "action", "after"}:
                    fail(f"acceptance fact {identifier} state_transition phase is invalid")
                if path != "/status" and not path.startswith("/"):
                    fail(f"acceptance fact {identifier} state_transition path is invalid")
            elif not path.startswith("/"):
                fail(f"acceptance fact {identifier} {kind} assertion path is invalid")
            try:
                encoded = json.dumps(assertion, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            except (TypeError, ValueError):
                fail(f"acceptance fact {identifier} {kind} assertion value is not JSON")
            signatures.append(encoded)
        if len(signatures) != len(set(signatures)):
            fail(f"acceptance fact {identifier} repeats one {kind} assertion")
        return (kind, normalized_fact(source_id), *sorted(signatures))
    if set(conjunct) != CONJUNCT_KEYS[kind]:
        fail(f"acceptance fact {identifier} {kind} conjunct fields do not match schema")
    for key, value in conjunct.items():
        if key == "status":
            continue
        if not isinstance(value, (str, list)) or not value:
            fail(f"acceptance fact {identifier} {kind} conjunct field {key} is empty")
    if kind == "ax_name_contains":
        if conjunct["path"] != ["nodes"]:
            fail(f"acceptance fact {identifier} accessibility conjunct path is not nodes")
        return (kind, normalized_fact(conjunct["expected"]))
    if kind == "http_response":
        if not isinstance(conjunct["status"], int) or not 100 <= conjunct["status"] <= 599:
            fail(f"acceptance fact {identifier} http_response status is not an HTTP code")
        return (
            kind,
            normalized_fact(conjunct["request"]),
            str(conjunct["status"]),
            normalized_fact(conjunct["body_contains"]),
        )
    if kind == "mcp_tool_response":
        return (
            kind,
            normalized_fact(conjunct["tool"]),
            normalized_fact(conjunct["field"]),
            normalized_fact(conjunct["expected"]),
        )
    if kind == "state_transition":
        if normalized_fact(conjunct["from"]) == normalized_fact(conjunct["to"]):
            fail(f"acceptance fact {identifier} state_transition does not change state")
        return (
            kind,
            normalized_fact(conjunct["from"]),
            normalized_fact(conjunct["to"]),
            normalized_fact(conjunct["via"]),
        )
    if kind == "receipt_field":
        return (
            kind,
            normalized_fact(conjunct["receipt"]),
            normalized_fact(conjunct["field"]),
            normalized_fact(conjunct["expected"]),
        )
    if kind == "log_assertion":
        return (
            kind,
            normalized_fact(conjunct["stream"]),
            normalized_fact(conjunct["expected"]),
        )
    return (kind, conjunct["observation"], normalized_fact(conjunct["expected"]))


def predicate_signature(identifier: str, predicate: Any) -> tuple[tuple[str, ...], ...]:
    """Validate a predicate in either allowed shape and return its uniqueness key.

    The simple shape is the approved 35-ID contract, preserved byte-for-byte in
    meaning. The all_of shape carries one or more conjuncts and is the only place a
    prior_state may appear, because a prior state alone identifies nothing.
    """
    if not isinstance(predicate, dict):
        fail(f"acceptance fact {identifier} predicate is not an object")
    if predicate.get("name") != f"required fact: {identifier}":
        fail(f"acceptance fact {identifier} predicate name is not bound to its ID")
    if predicate.get("operator") == "all_of":
        if set(predicate) != {"name", "operator", "conjuncts"}:
            fail(f"acceptance fact {identifier} all_of predicate fields do not match schema")
        conjuncts = predicate["conjuncts"]
        if not isinstance(conjuncts, list) or not conjuncts:
            fail(f"acceptance fact {identifier} all_of predicate has no conjunct")
        signatures = [conjunct_signature(identifier, item) for item in conjuncts]
        if len(set(signatures)) != len(signatures):
            fail(f"acceptance fact {identifier} repeats one conjunct")
        if all(item[0] == "prior_state" for item in signatures):
            fail(f"acceptance fact {identifier} asserts only a prior state")
        return tuple(sorted(signatures))
    if (
        set(predicate) != {"name", "path", "operator", "expected"}
        or predicate["path"] != ["nodes"]
        or predicate["operator"] != "ax_name_contains"
        or not isinstance(predicate["expected"], str)
        or not predicate["expected"].strip()
    ):
        fail(f"acceptance fact {identifier} predicate is not state-specific")
    return (("ax_name_contains", normalized_fact(predicate["expected"])),)


def validate_derivation(identifier: str, derivation: Any) -> str:
    """Validate the declared derivation kind exhaustively and return that kind."""
    if not isinstance(derivation, dict) or derivation.get("kind") not in DERIVATION_KEYS:
        fail(f"acceptance fact {identifier} has an unknown derivation kind")
    kind = derivation["kind"]
    if set(derivation) != DERIVATION_KEYS[kind]:
        fail(f"acceptance fact {identifier} {kind} derivation fields do not match schema")
    if kind == "typed_predicate":
        for key in ("kind", "proposal_id", "source_commit"):
            value = derivation[key]
            if not isinstance(value, str) or not value.strip():
                fail(f"acceptance fact {identifier} derivation field {key} is empty")
        if derivation["proposal_id"] != identifier:
            fail(f"acceptance fact {identifier} typed proposal ID is not self-bound")
        if re.fullmatch(r"[0-9a-f]{40}", derivation["source_commit"]) is None:
            fail(f"acceptance fact {identifier} typed source commit is not immutable")
        lines = derivation["source_lines"]
        if (
            not isinstance(lines, list)
            or len(lines) != 2
            or not all(isinstance(line, int) and line > 0 for line in lines)
            or lines[0] > lines[1]
        ):
            fail(f"acceptance fact {identifier} typed source lines are invalid")
        return kind
    for key, value in derivation.items():
        if not isinstance(value, str) or not value.strip():
            fail(f"acceptance fact {identifier} derivation field {key} is empty")
    return kind


def load_acceptance_contract(
    design_home: Path = DESIGN_HOME,
) -> dict[str, Any]:
    """Load and validate the one canonical acceptance ID/fact declaration."""
    facts = read_json(design_home / "context" / "acceptance-facts.json")
    if set(facts) != {
        "schema_version", "approved_35", "sequence", "inventory", "final_gates"
    } or facts["schema_version"] != 1:
        fail("acceptance facts fields do not match schema")
    expected_counts = {"approved_35": 35, "sequence": 9, "inventory": 189, "final_gates": 3}
    rows_by_group: dict[str, list[dict[str, Any]]] = {}
    # Global across every group: AN-000000000356 makes uniqueness span all 201 rows,
    # and the key is the whole canonical conjunct tuple, not one expected string.
    signature_owner: dict[tuple[tuple[str, ...], ...], str] = {}
    declared_gaps: list[dict[str, str]] = []
    duplicate_signatures: list[tuple[str, str]] = []
    prior_state_refs: list[tuple[str, str]] = []
    for group, expected_count in expected_counts.items():
        rows = facts[group]
        if group == "approved_35":
            if (
                not isinstance(rows, list)
                or len(rows) != expected_count
                or len(rows) != len(set(rows))
                or not all(isinstance(item, str) and item for item in rows)
            ):
                fail("approved_35 must preserve 35 unique IDs")
            continue
        if not isinstance(rows, list) or len(rows) != expected_count:
            fail(f"acceptance facts {group} must contain exactly {expected_count} rows")
        identifiers: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "id", "surface", "status", "precondition", "action",
                "expected_fact", "predicate", "evidence_source", "derivation"
            }:
                fail(f"acceptance fact row fields do not match schema in {group}")
            identifier = row["id"]
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                fail(f"acceptance fact IDs must be unique in {group}")
            identifiers.add(identifier)
            if row["surface"] not in {"aionui", "fleet", "personal"}:
                fail(f"acceptance fact {identifier} has invalid surface")
            if row["status"] not in {"normative", "measured-gap"}:
                fail(f"acceptance fact {identifier} lacks normative/measured status")
            for field in ("precondition", "action", "expected_fact", "evidence_source"):
                if not isinstance(row[field], str) or not row[field].strip():
                    fail(f"acceptance fact {identifier} lacks {field}")
            signature = predicate_signature(identifier, row["predicate"])
            owner = signature_owner.get(signature)
            if owner is not None:
                # Semantic, not structural. A repeated canonical fact BLOCKS acceptance
                # but must not stop the contract loading, because the harness derives
                # its whole inventory from here and would otherwise be unable to run at
                # all. Reported by validate_acceptance_derivations instead.
                duplicate_signatures.append((identifier, owner))
            else:
                signature_owner[signature] = identifier
            for conjunct in row["predicate"].get("conjuncts", []):
                if conjunct.get("kind") == "prior_state":
                    prior_state_refs.append((identifier, conjunct["observation"]))
            derivation_kind = validate_derivation(identifier, row["derivation"])
            if derivation_kind in BLOCKING_DERIVATIONS:
                if row["status"] != "measured-gap":
                    fail(f"acceptance fact {identifier} hides a declared gap as normative")
                declared_gaps.append(
                    {
                        "id": identifier,
                        "kind": derivation_kind,
                        "owner": row["derivation"]["owner"],
                        "action": row["derivation"]["action"],
                    }
                )
            elif row["status"] != "normative":
                fail(f"acceptance fact {identifier} is measured-gap without a declared gap")
            normalized.append(row)
        rows_by_group[group] = normalized
    inventory_ids = [row["id"] for row in rows_by_group["inventory"]]
    if not set(facts["approved_35"]).issubset(inventory_ids):
        fail("approved_35 meanings were not preserved in expanded inventory")
    # A prior_state may only name another canonical observation. The harness enforces
    # the rest of the operator's condition, that the referenced observation is present
    # in the same report and has independently passed its own validation.
    known_ids = {
        row["id"]
        for group in ("sequence", "inventory", "final_gates")
        for row in rows_by_group[group]
    }
    for identifier, referenced in prior_state_refs:
        if referenced == identifier:
            fail(f"acceptance fact {identifier} names itself as its prior state")
        if referenced not in known_ids:
            fail(
                f"acceptance fact {identifier} names unknown prior observation {referenced}"
            )
    manifest = read_json(design_home / "context" / "source-manifest.json")
    if manifest.get("acceptanceFacts") != "docs/design-home/context/acceptance-facts.json":
        fail("source-manifest acceptanceFacts path mismatch")
    if manifest.get("acceptanceInventoryIds") != inventory_ids:
        fail("source-manifest inventory IDs differ from canonical facts")
    return {
        "sequence": tuple(row["id"] for row in rows_by_group["sequence"]),
        "inventory": tuple(inventory_ids),
        "final_gates": tuple(row["id"] for row in rows_by_group["final_gates"]),
        "gaps": tuple(declared_gaps),
        "duplicates": tuple(duplicate_signatures),
        "facts": {
            row["id"]: row
            for group in ("sequence", "inventory", "final_gates")
            for row in rows_by_group[group]
        },
    }


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
        elif artifact.read_bytes() != sources[source_path]:
            errors.append(f"literal artifact differs from baseline bytes: {artifact_path}")
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


def source_tool_visibility(source: bytes) -> dict[str, str]:
    module = ast.parse(source.decode("utf-8"))
    visibility: dict[str, str] = {}
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "apps"
                and decorator.func.attr == "tool"
            ):
                continue
            keyword = next(
                (item for item in decorator.keywords if item.arg == "visibility"),
                None,
            )
            if keyword is None or not isinstance(keyword.value, ast.Name):
                fail(f"@apps.tool visibility is not a name: {node.name}")
            if node.name in visibility:
                fail(f"duplicate @apps.tool definition: {node.name}")
            visibility[node.name] = keyword.value.id
    return visibility


def described_dashboard_output(package_source: bytes) -> str:
    package = json.loads(package_source)
    description = package.get("description")
    if not isinstance(description, str):
        fail("dashboard package description is missing")
    paths = set(
        re.findall(r"packages/[a-z0-9_./-]+/dashboard\.html", description)
    )
    if len(paths) != 1:
        fail(f"dashboard package description has ambiguous output paths: {sorted(paths)}")
    return paths.pop()


def validate_dashboard_build_output(
    design_home: Path,
    sources: dict[str, bytes],
) -> list[str]:
    errors: list[str] = []
    expected = described_dashboard_output(sources["tools/dashboard-ui/package.json"])
    git_source(expected)

    literal_package = (
        design_home
        / "context"
        / "raw"
        / "tools"
        / "dashboard-ui"
        / "package.json"
    ).read_bytes()
    if described_dashboard_output(literal_package) != expected:
        errors.append("literal package Dashboard-UI build-output path mismatch")

    documents = (
        "inventory.md",
        "context/routes.md",
        "context/pages.md",
    )
    for relative in documents:
        text = (design_home / relative).read_text(encoding="utf-8")
        documented = set(
            re.findall(r"packages/[a-z0-9_./-]+/dashboard\.html", text)
        )
        if documented != {expected}:
            errors.append(
                "Dashboard-UI build-output path mismatch: "
                f"{relative}: expected={[expected]}, documented={sorted(documented)}"
            )

    routes_doc = (design_home / "context" / "routes.md").read_text(encoding="utf-8")
    if "**Generated Vite output:**" not in routes_doc or "Source of truth" in routes_doc:
        errors.append("Dashboard-UI source/generated-output semantics mismatch")
    return errors


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

    source_visibility = source_tool_visibility(
        sources["packages/personal/src/pursers_personal/apps_server.py"]
    )
    visibility_section = markdown_section(
        routes_doc,
        "### Exact decorator visibility",
        "### Product-role grouping",
    )
    documented_visibility = {
        tool: visibility
        for tool, visibility in re.findall(
            r"^\| `([a-z0-9_]+)` \| `(MODEL_AND_APP|APP_ONLY|MODEL_ONLY)` \|$",
            visibility_section,
            flags=re.MULTILINE,
        )
    }
    if source_visibility != documented_visibility:
        source_pairs = set(source_visibility.items())
        documented_pairs = set(documented_visibility.items())
        errors.append(
            "tool visibility matrix mismatch: "
            f"missing={sorted(source_pairs - documented_pairs)}, "
            f"extra={sorted(documented_pairs - source_pairs)}"
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
    try:
        contract = load_acceptance_contract(design_home)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return [str(exc)]
    inventory = (design_home / "inventory.md").read_text(encoding="utf-8")
    section = markdown_section(
        inventory, "<!-- acceptance-facts:start -->", "<!-- acceptance-facts:end -->"
    )
    documented = re.findall(r"^\| `([a-z0-9._-]+)` \|", section, flags=re.MULTILINE)
    expected = [*contract["inventory"], *contract["final_gates"]]
    errors: list[str] = []
    if len(documented) != len(set(documented)):
        errors.append("acceptance fact catalog contains duplicate IDs")
    if documented != expected:
        errors.append("acceptance fact catalog IDs differ from canonical facts")
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


def predicate_expected_values(predicate: dict[str, Any]) -> list[str]:
    """Every string a predicate claims will be observed, across both shapes."""
    if predicate.get("operator") != "all_of":
        return [predicate["expected"]]
    values: list[str] = []
    for conjunct in predicate["conjuncts"]:
        if "assertions" in conjunct:
            values.extend(
                assertion["value"]
                for assertion in conjunct["assertions"]
                if isinstance(assertion.get("value"), str)
            )
            continue
        if conjunct["kind"] in {
            "ax_name_contains", "mcp_tool_response", "receipt_field",
            "log_assertion",
        }:
            values.append(conjunct["expected"])
        elif conjunct["kind"] == "http_response":
            values.append(conjunct["body_contains"])
        elif conjunct["kind"] == "state_transition":
            values.extend([conjunct["from"], conjunct["to"], conjunct["via"]])
    return values


def validate_acceptance_derivations(design_home: Path) -> list[str]:
    """Every canonical fact must trace to its named source, or block acceptance.

    literal      the quoted source text must exist in the evidence source, and every
                 value the predicate expects must appear inside that quote
    identifier   the named symbol must exist in the evidence source, because the
                 observed value is produced at runtime rather than written literally
    observed_gap the behaviour is undefined; per AN-000000000356 amendment one this
                 does NOT pass, it blocks, and it carries an owner and an action
    """
    errors: list[str] = []
    contract = load_acceptance_contract(design_home)
    delta = read_json(
        design_home / "context" / TYPED_PREDICATE_DELTA_PATH.name
    )
    proposals = delta.get("proposals")
    partition = delta.get("partition")
    if (
        delta.get("schema_version") != 1
        or delta.get("base_commit") != TYPED_PREDICATE_BASE
        or not isinstance(proposals, list)
        or not isinstance(partition, dict)
        or not isinstance(partition.get("owned_ids"), list)
    ):
        fail("typed predicate delta identity or partition is invalid")
    proposal_by_id = {
        proposal.get("id"): proposal
        for proposal in proposals
        if isinstance(proposal, dict) and isinstance(proposal.get("id"), str)
    }
    owned_ids = partition["owned_ids"]
    if (
        len(proposal_by_id) != 121
        or len(owned_ids) != 121
        or len(set(owned_ids)) != 121
        or set(proposal_by_id) != set(owned_ids)
    ):
        fail("typed predicate delta must contain the exact 121-ID owned partition")
    typed_ids = {
        identifier
        for identifier, row in contract["facts"].items()
        if row["derivation"]["kind"] == "typed_predicate"
    }
    if typed_ids != set(owned_ids):
        fail("canonical typed predicates do not consume the exact delta partition")
    for identifier, owner in contract["duplicates"]:
        errors.append(
            f"acceptance fact {identifier} repeats the canonical fact of {owner} and "
            "blocks acceptance; bind it to its distinguishing state instead of "
            "rewording it"
        )
    cache: dict[str, str | None] = {}
    for identifier, row in contract["facts"].items():
        relative = row["evidence_source"]
        if relative not in cache:
            path = REPO_ROOT / relative
            cache[relative] = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else None
            )
        source = cache[relative]
        derivation = row["derivation"]
        kind = derivation["kind"]
        if kind in BLOCKING_DERIVATIONS:
            reason = (
                "no evidence collector can observe it"
                if kind == "collector_gap"
                else "the behaviour itself is undefined"
            )
            errors.append(
                f"acceptance fact {identifier} is a {kind} and blocks acceptance because "
                f"{reason}: owner={derivation['owner']}, action={derivation['action']}"
            )
            continue
        if kind == "typed_predicate":
            proposal = proposal_by_id[identifier]
            proposal_source = proposal.get("source")
            causal = proposal.get("causal")
            canonical = proposal.get("canonical_predicate")
            if (
                proposal.get("status") != "executable_proposal"
                or not isinstance(proposal_source, dict)
                or not isinstance(causal, dict)
                or not isinstance(canonical, dict)
            ):
                errors.append(f"acceptance fact {identifier} has an invalid typed proposal")
                continue
            if row["evidence_source"] != proposal_source.get("path"):
                errors.append(
                    f"acceptance fact {identifier} evidence source differs from its typed proposal"
                )
            if derivation != {
                "kind": "typed_predicate",
                "proposal_id": identifier,
                "source_commit": proposal_source.get("commit"),
                "source_lines": proposal_source.get("lines"),
            }:
                errors.append(
                    f"acceptance fact {identifier} derivation differs from its typed proposal"
                )
            expected_fields = {
                "precondition": causal.get("precondition"),
                "action": causal.get("action"),
                "expected_fact": causal.get("expected_transition"),
            }
            for field, expected in expected_fields.items():
                if row[field] != expected:
                    errors.append(
                        f"acceptance fact {identifier} {field} differs from its typed proposal"
                    )
            expected_predicate = {
                "name": f"required fact: {identifier}",
                "operator": "all_of",
                "conjuncts": [canonical],
            }
            if row["predicate"] != expected_predicate:
                errors.append(
                    f"acceptance fact {identifier} predicate differs from its typed proposal"
                )
            commit = derivation["source_commit"]
            start, end = derivation["source_lines"]
            try:
                anchored = subprocess.check_output(
                    ["git", "show", f"{commit}:{row['evidence_source']}"],
                    cwd=REPO_ROOT,
                    stderr=subprocess.STDOUT,
                    text=True,
                ).splitlines()
            except subprocess.CalledProcessError as exc:
                errors.append(
                    f"acceptance fact {identifier} typed source anchor is unavailable: "
                    f"{exc.output.strip()}"
                )
            else:
                if not 1 <= start <= end <= len(anchored):
                    errors.append(
                        f"acceptance fact {identifier} typed source lines are out of range"
                    )
            continue
        if source is None:
            errors.append(
                f"acceptance fact {identifier} names a missing evidence source {relative}"
            )
            continue
        if kind == "identifier":
            if derivation["symbol"] not in source:
                errors.append(
                    f"acceptance fact {identifier} names symbol {derivation['symbol']!r} "
                    f"that does not exist in {relative}"
                )
            continue
        quote = derivation["source_quote"]
        if normalized_fact(quote) not in normalized_fact(source):
            errors.append(
                f"acceptance fact {identifier} quotes text absent from {relative}"
            )
            continue
        quoted = normalized_fact(quote)
        for value in predicate_expected_values(row["predicate"]):
            if normalized_fact(value) not in quoted:
                errors.append(
                    f"acceptance fact {identifier} expects {value!r}, which its own "
                    f"quoted source text does not contain"
                )
    return errors


def validate(design_home: Path = DESIGN_HOME) -> tuple[list[str], tuple[int, int]]:
    try:
        manifest = load_manifest(design_home)
        sources = baseline_sources(manifest)
        errors = []
        errors.extend(validate_literal_sources(design_home, manifest, sources))
        errors.extend(validate_excerpts(design_home, manifest, sources))
        errors.extend(validate_dashboard_build_output(design_home, sources))
        errors.extend(validate_route_matrices(design_home, sources))
        errors.extend(validate_source_counts(design_home, manifest, sources))
        errors.extend(validate_acceptance_inventory(design_home, manifest))
        errors.extend(validate_acceptance_derivations(design_home))
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
        routes = routes_path.read_text(encoding="utf-8")
        visibility_marker = "| `board_snapshot` | `MODEL_AND_APP` |"
        if visibility_marker not in routes:
            fail("tool visibility probe marker missing")
        routes_path.write_text(
            routes.replace(
                visibility_marker,
                "| `board_snapshot` | `MODEL_ONLY` |",
                1,
            ),
            encoding="utf-8",
        )
        visibility_errors, _ = validate(disposable)
        assert_probe(
            "tool-visibility",
            visibility_errors,
            "tool visibility matrix mismatch",
        )

        shutil.copy2(DESIGN_HOME / "context" / "routes.md", routes_path)
        literal_path = (
            disposable
            / "context"
            / "raw"
            / "tools"
            / "dashboard-ui"
            / "package.json"
        )
        literal_path.write_bytes(literal_path.read_bytes() + b"\n")
        literal_errors, _ = validate(disposable)
        assert_probe(
            "literal-bytes",
            literal_errors,
            "literal artifact differs from baseline bytes",
        )

        shutil.copy2(
            DESIGN_HOME
            / "context"
            / "raw"
            / "tools"
            / "dashboard-ui"
            / "package.json",
            literal_path,
        )
        inventory_path = disposable / "inventory.md"
        inventory = inventory_path.read_text(encoding="utf-8")
        output_path = "packages/personal/src/pursers_personal/resources/dashboard.html"
        if output_path not in inventory:
            fail("Dashboard-UI build-output probe marker missing")
        inventory_path.write_text(
            inventory.replace(
                output_path,
                "packages/personal/" + "resources/dashboard.html",
                1,
            ),
            encoding="utf-8",
        )
        output_errors, _ = validate(disposable)
        assert_probe(
            "build-output-path",
            output_errors,
            "Dashboard-UI build-output path mismatch",
        )

        shutil.copy2(DESIGN_HOME / "inventory.md", inventory_path)
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
        facts_path = disposable / "context" / "acceptance-facts.json"
        facts = read_json(facts_path)
        facts["inventory"][0]["predicate"] = facts["inventory"][1]["predicate"]
        facts_path.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        fact_errors, _ = validate(disposable)
        assert_probe(
            "fact-id-predicate",
            fact_errors,
            "predicate name is not bound to its ID",
        )

        shutil.copy2(DESIGN_HOME / "context" / "acceptance-facts.json", facts_path)
        facts = read_json(facts_path)
        typed_row = next(
            row
            for group in ("sequence", "inventory", "final_gates")
            for row in facts[group]
            if row["derivation"]["kind"] == "typed_predicate"
        )
        typed_row["derivation"]["source_commit"] = "main"
        facts_path.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
        anchor_errors, _ = validate(disposable)
        assert_probe(
            "typed-source-anchor",
            anchor_errors,
            "typed source commit is not immutable",
        )

        shutil.copy2(DESIGN_HOME / "context" / "acceptance-facts.json", facts_path)
        delta_path = disposable / "context" / TYPED_PREDICATE_DELTA_PATH.name
        delta = read_json(delta_path)
        delta["proposals"][0]["canonical_predicate"]["source_id"] += "-probe"
        delta_path.write_text(json.dumps(delta, indent=2) + "\n", encoding="utf-8")
        proposal_errors, _ = validate(disposable)
        assert_probe(
            "typed-proposal-binding",
            proposal_errors,
            "predicate differs from its typed proposal",
        )

        shutil.copy2(TYPED_PREDICATE_DELTA_PATH, delta_path)
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
