#!/usr/bin/env python3
"""Load one owner-only environment file and exec an approved seat command."""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_environment(path: Path) -> dict[str, str]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_size > 1024 * 1024
    ):
        raise RuntimeError("credential_environment_untrusted")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, encoded = line.partition("=")
        if not separator or not ENV_NAME.fullmatch(name) or name in values:
            raise RuntimeError("credential_environment_invalid")
        parsed = shlex.split(encoded, posix=True) if encoded else [""]
        if len(parsed) != 1 or "\0" in parsed[0]:
            raise RuntimeError("credential_environment_invalid")
        values[name] = parsed[0]
    return values


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 2:
        raise SystemExit("usage: launchd_env_exec.py ENV_FILE COMMAND [ARG ...]")
    environment = os.environ.copy()
    environment.update(load_environment(Path(arguments[0])))
    os.execvpe(arguments[1], arguments[1:], environment)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
