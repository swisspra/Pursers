#!/usr/bin/env python3
"""Validate an exported Pursers extension in a scratch Zed registry clone."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_REGISTRY_REF = "21cd47e741cd80e0c1c574da0e00bec103c6e94d"
DEFAULT_REGISTRY_URL = "https://github.com/zed-industries/extensions.git"
DEFAULT_EXTENSION_ID = "pursers-mcp"
DEFAULT_EXTENSION_REPOSITORY = "https://github.com/swisspra/pursers-zed.git"
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
EXTENSION_ID = re.compile(r"[a-z0-9-]+\Z")


class RegistryCheckError(RuntimeError):
    """Raised when the local registry validation cannot complete."""


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    print(f"$ {shlex.join(command)}", flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.returncode != 0:
        raise RegistryCheckError(
            f"command exited {completed.returncode}: {shlex.join(command)}"
        )
    return completed


def require_empty_directory(path: Path, *, name: str) -> Path:
    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise RegistryCheckError(f"{name} is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_manifest(export_repo: Path) -> dict[str, object]:
    manifest_path = export_repo / "extension.toml"
    if not manifest_path.is_file():
        raise RegistryCheckError(f"missing extension manifest: {manifest_path}")
    with manifest_path.open("rb") as handle:
        return tomllib.load(handle)


def registry_stanza(extension_id: str, version: str) -> str:
    if not EXTENSION_ID.fullmatch(extension_id):
        raise RegistryCheckError(f"invalid extension ID: {extension_id!r}")
    if not SEMVER.fullmatch(version):
        raise RegistryCheckError(f"invalid extension version: {version!r}")
    return (
        f"[{extension_id}]\n"
        f'submodule = "extensions/{extension_id}"\n'
        f'version = "{version}"\n'
    )


def pr_body(
    *,
    source_commit: str,
    version: str,
    repository_url: str,
) -> str:
    return f"""# Add Pursers MCP

Adds `pursers-mcp` {version}, a Zed context-server extension for Pursers boards.

- Source: `{repository_url.removesuffix('.git')}@{source_commit}`
- Launches: `uvx --from pursers-client pursers-mcp`
- Tested: registry build, tests, exact validation library, and sort check
- License: Apache-2.0
"""


def write_operator_artifacts(
    output_dir: Path,
    *,
    extension_id: str,
    version: str,
    source_commit: str,
    repository_url: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "extensions.toml").write_text(
        registry_stanza(extension_id, version), encoding="utf-8"
    )
    (output_dir / "PR_BODY.md").write_text(
        pr_body(
            source_commit=source_commit,
            version=version,
            repository_url=repository_url,
        ),
        encoding="utf-8",
    )


def _local_validation_script(extension_id: str) -> str:
    return f"""import assert from "node:assert";
import {{ readTomlFile, retrieveLicenseCandidates }} from "./src/lib/fs.js";
import {{ readGitmodules }} from "./src/lib/git.js";
import {{
  validateExtensionsToml,
  validateGitmodules,
  validateGitmodulesLocations,
  validateLicense,
  validateManifest,
}} from "./src/lib/validation.js";

const id = {extension_id!r};
const registry = await readTomlFile("extensions.toml");
const gitmodules = await readGitmodules(".gitmodules");
validateExtensionsToml(registry);
validateGitmodules(gitmodules);
validateGitmodulesLocations(registry, gitmodules);
const manifest = await readTomlFile(`extensions/${{id}}/extension.toml`);
assert.equal(manifest.id, id, "registry and manifest IDs differ");
assert.equal(manifest.version, registry[id].version, "registry and manifest versions differ");
validateManifest(manifest);
validateLicense(await retrieveLicenseCandidates(`extensions/${{id}}`));
console.log(`VALIDATION PASS: ${{id}}@${{manifest.version}}`);
"""


def clone_registry(scratch: Path, registry_url: str, registry_ref: str) -> None:
    _run(["git", "init", "--initial-branch=validation"], cwd=scratch)
    _run(["git", "remote", "add", "origin", registry_url], cwd=scratch)
    _run(
        [
            "git",
            "fetch",
            "--depth=1",
            "origin",
            f"{registry_ref}:refs/remotes/origin/main",
        ],
        cwd=scratch,
    )
    _run(["git", "reset", "--hard", "origin/main"], cwd=scratch)


def add_local_submodule(
    scratch: Path,
    export_repo: Path,
    *,
    extension_id: str,
    repository_url: str,
) -> None:
    path = f"extensions/{extension_id}"
    _run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "-c",
            f"url.{export_repo.as_uri()}.insteadOf={repository_url}",
            "submodule",
            "add",
            "--name",
            path,
            repository_url,
            path,
        ],
        cwd=scratch,
    )


def check_registry(
    *,
    export_repo: Path,
    scratch: Path,
    registry_url: str,
    registry_ref: str,
    extension_id: str,
    repository_url: str,
    artifacts_dir: Path,
    pnpm: str,
    zed_extension_cli: Path | None,
) -> dict[str, str]:
    export_repo = export_repo.resolve()
    if not (export_repo / ".git").exists():
        raise RegistryCheckError(f"export is not a Git repository: {export_repo}")
    if not (export_repo / "LICENSE").is_file():
        raise RegistryCheckError("exported repository must contain LICENSE at root")

    manifest = load_manifest(export_repo)
    manifest_id = manifest.get("id")
    version = manifest.get("version")
    if manifest_id != extension_id:
        raise RegistryCheckError(
            f"manifest ID {manifest_id!r} does not match {extension_id!r}"
        )
    if not isinstance(version, str):
        raise RegistryCheckError("extension manifest version must be a string")
    stanza = registry_stanza(extension_id, version)
    source_commit = _run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=export_repo
    ).stdout.strip()

    scratch = require_empty_directory(scratch, name="scratch directory")
    clone_registry(scratch, registry_url, registry_ref)
    if f"[{extension_id}]" in (scratch / "extensions.toml").read_text(
        encoding="utf-8"
    ):
        raise RegistryCheckError(f"registry already contains [{extension_id}]")
    add_local_submodule(
        scratch,
        export_repo,
        extension_id=extension_id,
        repository_url=repository_url,
    )
    with (scratch / "extensions.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n" + stanza)

    _run(["git", "config", "user.name", "Pursers registry validator"], cwd=scratch)
    _run(
        ["git", "config", "user.email", "registry-validator@invalid"], cwd=scratch
    )
    _run([pnpm, "install", "--frozen-lockfile"], cwd=scratch)
    _run([pnpm, "build"], cwd=scratch)
    _run([pnpm, "test"], cwd=scratch)

    validation_script = scratch / ".pursers-registry-check.mjs"
    validation_script.write_text(
        _local_validation_script(extension_id), encoding="utf-8"
    )
    _run(["node", validation_script.name], cwd=scratch)

    _run([pnpm, "sort-extensions"], cwd=scratch)
    _run(
        ["git", "add", ".gitmodules", "extensions.toml", f"extensions/{extension_id}"],
        cwd=scratch,
    )
    _run(["git", "commit", "-m", f"Validate {extension_id}"], cwd=scratch)
    _run([pnpm, "sort-extensions"], cwd=scratch)
    _run(
        [
            "git",
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            "extensions.toml",
            ".gitmodules",
        ],
        cwd=scratch,
    )
    _run(["git", "diff", "--check", "HEAD^", "HEAD"], cwd=scratch)

    if zed_extension_cli is not None:
        cli = zed_extension_cli.resolve()
        if not cli.is_file():
            raise RegistryCheckError(f"zed-extension CLI not found: {cli}")
        destination = scratch / "zed-extension"
        shutil.copy2(cli, destination)
        destination.chmod(destination.stat().st_mode | 0o111)
        _run(
            [pnpm, "package-extensions", extension_id],
            cwd=scratch,
            env={"REF_NAME": "validation", "SHOULD_PUBLISH": "false"},
        )
    else:
        print(
            "PACKAGE SKIP: pass --zed-extension-cli to run the CI-pinned packager",
            flush=True,
        )

    write_operator_artifacts(
        artifacts_dir.resolve(),
        extension_id=extension_id,
        version=version,
        source_commit=source_commit,
        repository_url=repository_url,
    )
    return {
        "registry_ref": registry_ref,
        "source_commit": source_commit,
        "version": version,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-repo", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY_URL)
    parser.add_argument("--registry-ref", default=DEFAULT_REGISTRY_REF)
    parser.add_argument("--extension-id", default=DEFAULT_EXTENSION_ID)
    parser.add_argument("--repository-url", default=DEFAULT_EXTENSION_REPOSITORY)
    parser.add_argument(
        "--artifacts-dir", type=Path, default=Path("dist/zed-registry")
    )
    parser.add_argument("--pnpm", default="pnpm")
    parser.add_argument("--zed-extension-cli", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = check_registry(
            export_repo=args.export_repo,
            scratch=args.scratch,
            registry_url=args.registry_url,
            registry_ref=args.registry_ref,
            extension_id=args.extension_id,
            repository_url=args.repository_url,
            artifacts_dir=args.artifacts_dir,
            pnpm=args.pnpm,
            zed_extension_cli=args.zed_extension_cli,
        )
    except (OSError, RegistryCheckError, tomllib.TOMLDecodeError) as exc:
        print(f"REGISTRY_CHECK FAIL: {exc}")
        return 1

    print(f"REGISTRY_REF={result['registry_ref']}")
    print(f"SOURCE_COMMIT={result['source_commit']}")
    print(f"VERSION={result['version']}")
    print("REGISTRY_CHECK PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
