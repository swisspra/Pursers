"""Configurable write-time secret/PII scrub gate for the A4 spike."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence


Mode = Literal["reject", "redact"]


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    flags: int = 0
    secret_group: str | None = None

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, self.flags)


@dataclass(frozen=True)
class Violation:
    rule: str
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class Policy:
    mode: Mode = "reject"
    enabled_rules: frozenset[str] | None = None
    extra_rules: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ("reject", "redact"):
            raise ValueError("mode must be 'reject' or 'redact'")


class ScrubRejected(ValueError):
    def __init__(self, violations: Sequence[Violation]):
        self.violations = tuple(violations)
        names = ", ".join(sorted({item.rule for item in violations}))
        super().__init__(f"write rejected by scrub policy: {names}")


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        "pem_private_key",
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?"
        r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    ),
    Rule("aws_access_key_id", r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"),
    Rule(
        "aws_secret_access_key",
        r"\b(?:aws_secret_access_key|secret_access_key)\s*[:=]\s*[\"']?"
        r"(?P<secret>[A-Za-z0-9/+=]{40})",
        re.IGNORECASE,
        "secret",
    ),
    Rule("gcp_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    Rule("gcp_oauth_token", r"\bya29\.[0-9A-Za-z_-]{20,}\b"),
    Rule(
        "azure_storage_key",
        r"\bAccountKey\s*=\s*(?P<secret>[A-Za-z0-9+/]{20,}={0,2})",
        re.IGNORECASE,
        "secret",
    ),
    Rule(
        "azure_client_secret",
        r"\b(?:AZURE_CLIENT_SECRET|client_secret)\s*[:=]\s*[\"']?"
        r"(?P<secret>[A-Za-z0-9._~+/=-]{12,})",
        re.IGNORECASE,
        "secret",
    ),
    Rule(
        "azure_sas_signature",
        r"(?:[?&]sig=)(?P<secret>[A-Za-z0-9%._~+/=-]{16,})",
        re.IGNORECASE,
        "secret",
    ),
    # Raw documentation placeholders can satisfy the token length/charset.
    # Exempt only complete, recognizable placeholder forms; suffixed values
    # with credential-like material still fail closed.
    Rule(
        "bearer_token",
        r"\bBearer[ \t]+(?P<secret>"
        r"(?!(?:placeholder|redacted|example|sample|dummy|your)"
        r"(?:[_-](?:access|auth|bearer|credential|secret|token|value|here))*"
        r"(?![A-Za-z0-9._~+/=-]))"
        r"[A-Za-z0-9._~+/=-]{16,})(?![A-Za-z0-9._~+/=-])",
        re.IGNORECASE,
        "secret",
    ),
    Rule("jwt", r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\b"),
    Rule(
        "url_password",
        r"\b[a-z][a-z0-9+.-]*://[^:\s/@]+:(?P<secret>[^@\s/]+)@",
        re.IGNORECASE,
        "secret",
    ),
    Rule("posix_home", r"/Users/(?P<secret>[^/\s]+)", 0, "secret"),
    Rule("windows_home", r"[A-Z]:\\Users\\(?P<secret>[^\\\s]+)", re.IGNORECASE, "secret"),
    Rule(
        "email",
        r"(?<![\w.+-])(?P<secret>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])",
        re.IGNORECASE,
        "secret",
    ),
)


def _active_rules(policy: Policy) -> tuple[Rule, ...]:
    rules = DEFAULT_RULES + policy.extra_rules
    if policy.enabled_rules is None:
        return rules
    return tuple(rule for rule in rules if rule.name in policy.enabled_rules)


def _find_violations(text: str, policy: Policy) -> list[Violation]:
    candidates: list[tuple[int, int, int, Violation]] = []
    for priority, rule in enumerate(_active_rules(policy)):
        for match in rule.compiled().finditer(text):
            start, end = match.span(rule.secret_group) if rule.secret_group else match.span()
            replacement = f"[REDACTED:{rule.name.upper()}]"
            violation = Violation(rule=rule.name, start=start, end=end, replacement=replacement)
            candidates.append((start, priority, -(end - start), violation))

    accepted: list[Violation] = []
    for _, _, _, candidate in sorted(candidates):
        if any(candidate.start < item.end and item.start < candidate.end for item in accepted):
            continue
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: item.start)


def scrub(text: str, policy: Policy) -> tuple[str, list[Violation]]:
    """Scrub text or reject it. Violation metadata never contains matched content."""
    violations = _find_violations(text, policy)
    if violations and policy.mode == "reject":
        raise ScrubRejected(violations)
    if not violations:
        return text, []

    pieces: list[str] = []
    cursor = 0
    for violation in violations:
        pieces.append(text[cursor : violation.start])
        pieces.append(violation.replacement)
        cursor = violation.end
    pieces.append(text[cursor:])
    return "".join(pieces), violations
