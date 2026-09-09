#!/usr/bin/env python3
"""Build the deterministic Pursers AionUi extension archive."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PACKAGE_FILES = (
    "aion-extension.json",
    "IMPORT_PROVENANCE.md",
    "README.md",
    "contexts/reviewer.md",
    "contexts/worker.md",
    "door/adapter.cjs",
    "door/DOOR_ONBOARDING_CONTRACT.md",
    "host/helper.cjs",
    "host/HELPER_CONTRACT.md",
    "result_visibility/adapter.cjs",
    "result_visibility/FEATURE_CONTRACT.md",
    "security/loopback.cjs",
    "team/adapter.cjs",
    "team/TEAM_ADAPTER_CONTRACT.md",
    "webui/app.js",
    "webui/candidate.json",
    "webui/index.html",
    "webui/routes.js",
    "webui/style.css",
    "vendor/aion-hub-extension-schema-v0.json",
)
ARCHIVE_NAME = "pursers-aionui-0.1.0.zip"
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def candidate_manifest(repository_root: Path) -> bytes:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    candidate_commit = completed.stdout.strip()
    if completed.returncode != 0 or not FULL_SHA.fullmatch(candidate_commit):
        raise ValueError("candidate commit must resolve to exact git HEAD")
    return (
        json.dumps(
            {"candidate_commit": candidate_commit, "schema_version": 1},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


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

    candidate = candidate_manifest(repository_root)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in PACKAGE_FILES:
            info = ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            data = candidate if relative == "webui/candidate.json" else (extension_root / relative).read_bytes()
            archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
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
