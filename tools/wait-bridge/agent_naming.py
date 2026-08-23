"""Deterministic wait-bridge agent naming."""

from __future__ import annotations

import re


AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def resolve_agent_name(base_name: str, instance: str | None) -> str:
    """Return the legacy base name or a stable instance-qualified name."""
    if not isinstance(base_name, str) or not AGENT_NAME_RE.fullmatch(base_name):
        raise ValueError(
            "ONBOARD_AGENT_NAME must contain 1-80 letters, digits, dots, underscores, or hyphens"
        )
    if instance in (None, ""):
        return base_name
    if not isinstance(instance, str) or not INSTANCE_RE.fullmatch(instance):
        raise ValueError(
            "ONBOARD_AGENT_INSTANCE must contain 1-32 letters, digits, dots, underscores, or hyphens"
        )
    resolved = f"{base_name}-{instance}"
    if len(resolved) > 80:
        raise ValueError("resolved agent name must not exceed 80 characters")
    return resolved
