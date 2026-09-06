"""Shared safety checks for human-input form delivery."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_INPUT_RE = re.compile(
    r"\b(?:credentials?|passwords?|passphrases?|tokens?|secrets?|api\s*keys?|"
    r"payments?|payment\s*(?:credentials?|details?|cards?))\b",
    re.IGNORECASE,
)
SENSITIVE_FORM_FALLBACK = (
    "This request requires secrets or credentials. Form elicitation is disabled; "
    "provide a trusted URL."
)


def _normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[_\-/]+", " ", value)


def _schema_form_text(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    values: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return values
    for name, prop in properties.items():
        values.append(_normalized_text(str(name)))
        if not isinstance(prop, dict):
            continue
        text = _normalized_text(prop.get("title"))
        if text:
            values.append(text)
    return values


def human_form_safety(message: Any, requested_schema: Any) -> tuple[bool, str | None]:
    """Return whether a request may be emitted as form fields.

    MCP elicitation forms must not collect secrets. The check is deliberately
    limited to schema property names and titles. General contact/profile fields
    and ordinary request prose are not categorically sensitive under the spec.
    """
    del message
    candidates = _schema_form_text(requested_schema)
    if any(_SENSITIVE_INPUT_RE.search(value) for value in candidates if value):
        return False, SENSITIVE_FORM_FALLBACK
    return True, None
