"""Scrub email-rule boundary regression tests.

The email rule previously used a trailing ``(?![\\w.-])`` lookahead, which
failed to match an address immediately followed by a sentence-ending period
(e.g. ``grace@example.com.``) — a false negative that let emails at the end of
a sentence pass through unquarantined. The boundary is now
``(?![\\w-])(?!\\.[A-Z0-9])``: a terminal ``.`` (not followed by another domain
label) is treated as punctuation, while a ``.`` that continues the domain still
forces a full-domain match.
"""

from __future__ import annotations

import pytest

from scrub import Policy, ScrubRejected, scrub

EMAIL_ONLY = Policy(mode="reject", enabled_rules=frozenset({"email"}))
BEARER_ONLY = Policy(mode="reject", enabled_rules=frozenset({"bearer_token"}))


def _hits(text: str) -> bool:
    try:
        scrub(text, EMAIL_ONLY)
        return False
    except Exception:
        return True


@pytest.mark.parametrize(
    "text",
    [
        "contact was grace@example.com.",          # the regression: end-of-sentence period
        "email alice@example.com for questions.",  # mid-sentence (already worked)
        "bob@example.com.au replied",              # multi-label TLD, full match
        "see carol@example.com, thanks",           # trailing comma
        "(dan@example.com)",                       # wrapped in parens
        "END:eve@sub.example.co.uk",               # colon-prefixed, multi-label
    ],
)
def test_email_addresses_are_detected(text: str) -> None:
    assert _hits(text), f"email should be flagged in: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "no email here, just a /Users path reference",
        "the @ handle mention_only without a domain",
        "version bump 4.0.4 shipped",
    ],
)
def test_non_emails_are_not_flagged_by_email_rule(text: str) -> None:
    assert not _hits(text), f"email rule false positive on: {text!r}"


def test_domain_continuation_is_not_truncated() -> None:
    """A trailing dot that continues the domain must force a full-domain match,
    not stop at an interior label."""
    from scrub import DEFAULT_RULES
    import re

    email_rule = next(r for r in DEFAULT_RULES if r.name == "email")
    m = re.search(email_rule.pattern, "bob@example.com.au", re.IGNORECASE)
    assert m is not None
    assert m.group("secret") == "bob@example.com.au"


@pytest.mark.parametrize(
    "text",
    [
        "The literal Bearer scheme word is harmless prose.",
        "Authorization: Bearer <placeholder>",
        "Authorization: Bearer [REDACTED]",
        "Authorization: Bearer placeholder_value",
        "Authorization: Bearer REDACTED_BEARER_TOKEN",
    ],
)
def test_bearer_documentation_and_placeholders_are_not_flagged(text: str) -> None:
    try:
        scrub(text, BEARER_ONLY)
    except ScrubRejected as exc:  # pragma: no cover - assertion reports violation
        pytest.fail(f"bearer documentation should be allowed: {exc}")


@pytest.mark.parametrize(
    "value",
    [
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "z9F4mQ2vL8sT6nP1xR5c",
        "placeholder_value_7f2a9c4e",
    ],
)
def test_credential_like_bearer_values_remain_blocked(value: str) -> None:
    with pytest.raises(ScrubRejected, match="bearer_token"):
        scrub(f"Authorization: Bearer {value}", BEARER_ONLY)
