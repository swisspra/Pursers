#!/usr/bin/env python3
"""Build the deterministic Pursers AionUi extension archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PACKAGE_FILES = (
    "aion-extension.json",
    "README.md",
    "contexts/reviewer.md",
    "contexts/worker.md",
    "door/adapter.cjs",
    "door/DOOR_ONBOARDING_CONTRACT.md",
    "security/loopback.cjs",
    "team/adapter.cjs",
    "team/TEAM_ADAPTER_CONTRACT.md",
    "webui/app.js",
    "webui/index.html",
    "webui/routes.js",
    "webui/style.css",
    "vendor/aion-hub-extension-schema-v0.json",
)
ARCHIVE_NAME = "pursers-aionui-0.1.0.zip"


def build(output: Path | None = None) -> Path:
    extension_root = Path(__file__).resolve().parent
    repository_root = extension_root.parents[1]
    destination = output or repository_root / "dist" / ARCHIVE_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(
        (extension_root / "aion-extension.json").read_text(encoding="utf-8")
    )
    expected_name = f"pursers-aionui-{manifest['version']}.zip"
    if destination.name != expected_name:
        raise ValueError(f"output filename must be {expected_name}")

    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in PACKAGE_FILES:
            source = extension_root / relative
            info = ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = build(args.output)
    try:
        display = destination.relative_to(Path.cwd())
    except ValueError:
        display = destination
    print(display)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
