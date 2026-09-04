#!/usr/bin/env python3
"""CI leak scanner: detects secrets, credentials, and identifying user paths.

Enforces the repo hygiene policy: repo text uses placeholders only.
Operator-specific markers live in an operator-local file outside the repo.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    flags: int = 0
    group: str | None = None

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, self.flags)


@dataclass(frozen=True)
class LeakViolation:
    filename: str
    line_number: int
    rule_name: str

    def format_finding(self) -> str:
        return f"{self.filename}:{self.line_number}: [{self.rule_name}] <masked>"


EXEMPT_HOME_USERS = frozenset(
    {
        "synthetic-user",
    }
)

EXEMPT_AWS_KEYS = frozenset(
    {
        "AK" + "IAABCDEFGHIJKLMNOP",
    }
)

EXEMPT_BEARER_TOKENS = frozenset(
    {
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "TESTTOKEN_123456",
        "synthetic-local-bearer",
        "SECRET-INPUT-VALUE",
        "pp4-unique-secret-token",
    }
)

EXEMPT_JWT_FIXTURES = (
    "TOKEN_MUST_NOT_APPEAR",
    "SECRET_PAYLOAD_CONTENT",
    "TOP_SECRET_GOOD_JWT_PAYLOAD",
    "TOP_SECRET_MALFORMED_JWT",
)

RULE_EXEMPTION_PATTERN = re.compile(
    r"#\s*(?:pragma:\s*allowlist|leak-scan:\s*exempt|noqa:\s*leak)\s+([A-Za-z0-9_-]+)"
)

# Only specific generic rules can be exempted by name.
# 'secret' alias is NOT permitted.
# 'operator_marker' is NEVER exemptable.
EXEMPTABLE_RULES = frozenset(
    {
        "posix_home",
        "linux_home",
        "windows_home",
        "jwt",
        "bearer_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "gcp_api_key",
        "gcp_oauth_token",
        "azure_client_secret",
        "pem_private_key",
    }
)

# Pattern fragments prevent the scanner source from matching its own rules
GENERIC_RULES: tuple[Rule, ...] = (
    Rule("posix_home", "/Us" + "ers/(?P<user>[A-Za-z0-9_-]+)", 0, "user"),
    Rule("linux_home", "/ho" + "me/(?P<user>[A-Za-z0-9_-]+)", 0, "user"),
    Rule("windows_home", r"[A-Za-z]:\\Us" + r"ers\\(?P<user>[A-Za-z0-9_-]+)", 0, "user"),
    Rule(
        "pem_private_key",
        "-----BEG" + r"IN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----",
    ),
    Rule(
        "aws_access_key_id",
        r"\b(?:AK" + r"IA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b",
    ),
    Rule(
        "aws_secret_access_key",
        r"\b(?:aws_secret_access_key|secret_access_key)\s*[:=]\s*[\"']?(?P<secret>[A-Za-z0-9/+=]{40})",
        re.IGNORECASE,
        "secret",
    ),
    Rule("gcp_api_key", r"\bAI" + r"za[0-9A-Za-z_-]{35}\b"),
    Rule("gcp_oauth_token", r"\bya" + r"29\.[0-9A-Za-z_-]{20,}\b"),
    Rule(
        "azure_client_secret",
        r"\b(?:AZURE_CLIENT_SECRET|client_secret)\s*[:=]\s*[\"']?(?P<secret>[A-Za-z0-9._~+/=-]{12,})",
        re.IGNORECASE,
        "secret",
    ),
    Rule(
        "bearer_token",
        r"\bBea" + r"rer[ \t]+"
        r"(?!(?:placeholder|redacted|example|sample|dummy|your)"
        r"(?:[_-](?:access|auth|bearer|credential|secret|token|value|here))*"
        r"(?![A-Za-z0-9._~+/=-]))"
        r"(?P<token>[A-Za-z0-9._~+/=-]{16,})(?![A-Za-z0-9._~+/=-])",
        re.IGNORECASE,
        "token",
    ),
    Rule(
        "jwt",
        r"\bey" + r"J[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\b",
    ),
)


def _load_operator_markers(path: Path | None) -> list[Rule]:
    """Load operator-local patterns if configured in an operator-local file outside repo."""
    if not path or not path.is_file():
        return []
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(Rule("operator_marker", line, 0))
    return rules


def scan_line(
    line: str,
    rules: Sequence[Rule] = GENERIC_RULES,
) -> list[str]:
    """Scan a single line for violations with match-scoped and rule-specific exemptions."""
    available_exemptions: dict[str, int] = {}
    for marker in RULE_EXEMPTION_PATTERN.findall(line):
        if marker in EXEMPTABLE_RULES:
            available_exemptions[marker] = available_exemptions.get(marker, 0) + 1

    hits: list[str] = []
    for rule in rules:
        for match in rule.compiled().finditer(line):
            matched_text = match.group(0)

            if rule.name in ("posix_home", "linux_home", "windows_home"):
                user = match.group("user")
                if user in EXEMPT_HOME_USERS:
                    continue

            elif rule.name == "bearer_token":
                token = match.group("token") if rule.group else matched_text
                if token in EXEMPT_BEARER_TOKENS or any(token == ex for ex in EXEMPT_BEARER_TOKENS):
                    continue

            elif rule.name == "aws_access_key_id":
                if matched_text in EXEMPT_AWS_KEYS:
                    continue

            elif rule.name == "jwt":
                if any(fixture in matched_text for fixture in EXEMPT_JWT_FIXTURES):
                    continue

            # Check match-scoped inline exemption: consumes at most ONE match per marker
            if available_exemptions.get(rule.name, 0) > 0:
                available_exemptions[rule.name] -= 1
                continue

            hits.append(rule.name)
    return hits


def scan_file(
    file_path: Path,
    rel_path: str,
    rules: Sequence[Rule] = GENERIC_RULES,
) -> list[LeakViolation]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    violations: list[LeakViolation] = []
    for line_num, line in enumerate(content.splitlines(), start=1):
        matched_rules = scan_line(line, rules)
        for rule_name in matched_rules:
            violations.append(
                LeakViolation(
                    filename=rel_path,
                    line_number=line_num,
                    rule_name=rule_name,
                )
            )
    return violations


def tracked_files(root: Path) -> list[Path]:
    """List git tracked files under root."""
    try:
        raw = subprocess.check_output(
            ["git", "ls-files"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return [root / line for line in raw.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = []
        for p in root.rglob("*"):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                files.append(p)
        return files


def run_scan(
    targets: Sequence[Path] | None = None,
    root: Path | None = None,
    operator_markers_path: Path | None = None,
) -> list[LeakViolation]:
    root_path = (root or Path(__file__).resolve().parents[1]).resolve()
    operator_rules = _load_operator_markers(operator_markers_path)
    all_rules = tuple(GENERIC_RULES) + tuple(operator_rules)

    if targets:
        files: list[Path] = []
        for t in targets:
            target_path = Path(t).resolve()
            if target_path.is_file():
                files.append(target_path)
            elif target_path.is_dir():
                files.extend(p for p in target_path.rglob("*") if p.is_file())
    else:
        files = tracked_files(root_path)

    all_violations: list[LeakViolation] = []
    for path in sorted(files):
        try:
            rel = path.relative_to(root_path).as_posix()
        except ValueError:
            rel = path.as_posix()
        all_violations.extend(scan_file(path, rel, all_rules))

    return all_violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked files or directories for secrets and identifying paths."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Paths to scan (default: all git-tracked files)",
    )
    parser.add_argument(
        "--operator-markers",
        type=Path,
        default=Path(os.environ.get("PURSER_LEAK_MARKERS_FILE", "")).expanduser()
        if os.environ.get("PURSER_LEAK_MARKERS_FILE")
        else None,
        help="Path to operator-local markers file outside the repo",
    )
    args = parser.parse_args(argv)

    violations = run_scan(
        targets=args.paths or None,
        operator_markers_path=args.operator_markers,
    )

    if violations:
        print(f"leak_scan: FAIL ({len(violations)} non-exempt violation(s) found):", file=sys.stderr)
        for v in violations:
            print(v.format_finding(), file=sys.stderr)
        return 1

    print("leak_scan: clean (0 violations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
