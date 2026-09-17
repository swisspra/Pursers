from __future__ import annotations

import stat

import pytest

from pursers_client import (
    REQUEST_STATE_TTL_S,
    load_or_create_request_state_keys,
)


def test_request_state_keyring_is_private_persistent_and_ordered(tmp_path) -> None:
    path = tmp_path / "private" / "request-state.keys"
    created = load_or_create_request_state_keys(path)
    assert len(created) == 1
    assert len(created[0]) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(
        b"new-key-material-with-at-least-32-bytes\n"
        b"old-key-material-with-at-least-32-bytes\n"
    )
    assert load_or_create_request_state_keys(path) == [
        b"new-key-material-with-at-least-32-bytes",
        b"old-key-material-with-at-least-32-bytes",
    ]
    assert REQUEST_STATE_TTL_S == 3600.0


def test_request_state_keyring_rejects_short_or_public_file(tmp_path) -> None:
    path = tmp_path / "request-state.keys"
    path.write_text("short\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        load_or_create_request_state_keys(path)
    path.write_text("long-enough-request-state-key-material\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions must be 0600"):
        load_or_create_request_state_keys(path)
