from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
PACK = REPOSITORY / "docs" / "demo" / "fleet-public-media"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SAFE_CHUNKS = {b"IHDR", b"iCCP", b"IDAT", b"IEND"}


def _png(path: Path) -> tuple[tuple[int, int], set[bytes]]:
    raw = path.read_bytes()
    assert raw.startswith(PNG_SIGNATURE)
    cursor = len(PNG_SIGNATURE)
    chunks: set[bytes] = set()
    size: tuple[int, int] | None = None
    while cursor < len(raw):
        length = struct.unpack(">I", raw[cursor : cursor + 4])[0]
        kind = raw[cursor + 4 : cursor + 8]
        payload = raw[cursor + 8 : cursor + 8 + length]
        chunks.add(kind)
        if kind == b"IHDR":
            size = struct.unpack(">II", payload[:8])
        cursor += length + 12
    assert cursor == len(raw)
    assert size is not None
    return size, chunks


def test_public_media_manifest_matches_sanitized_pngs() -> None:
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["source_commit"] == "05864cec15ddf116829548151d7c27440c224f76"
    assert manifest["source_snapshot_persisted"] is False
    assert manifest["viewport"] == {
        "width": 1440,
        "height": 900,
        "device_scale_factor": 1,
    }
    assert [screen["route"] for screen in manifest["screens"]] == [
        "home",
        "projects",
        "work",
        "team",
        "approvals",
        "activity",
    ]
    assert manifest["omissions"] == [
        {
            "screen": "settings",
            "reason": "not exposed by the read-only public projection",
        }
    ]

    for screen in manifest["screens"]:
        image = PACK / screen["file"]
        raw = image.read_bytes()
        assert len(raw) == screen["bytes"]
        assert hashlib.sha256(raw).hexdigest() == screen["sha256"]
        dimensions, chunks = _png(image)
        assert dimensions == (1440, 900)
        assert chunks <= SAFE_CHUNKS
        assert not ({b"tEXt", b"zTXt", b"iTXt", b"eXIf"} & chunks)


def test_public_media_text_contains_no_private_identifiers() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            PACK / "README.md",
            PACK / "STORYBOARD.md",
            PACK / "manifest.json",
        )
    )
    forbidden = (
        r"/Users/",
        r"/home/",
        r"(?:TK|AI|PR)-[A-Za-z0-9]",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        r"\b(?:127\.0\.0\.1|localhost)(?::\d+)?\b",
        r"\b(?:Bearer|Authorization|token-file)\b",
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    )
    for pattern in forbidden:
        assert re.search(pattern, text, re.IGNORECASE) is None, pattern
