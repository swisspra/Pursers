#!/usr/bin/env python3
"""Host configuration, seat upgrades, prompts, doctor, and inventory.

The fleet dashboard imports this module.  The small CLI is intentionally only
for headless doctor/fix use; the operator-facing setup surface remains the
loopback dashboard Config page.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


REPOSITORY = Path(__file__).resolve().parents[2]
WAIT_BRIDGE = REPOSITORY / "tools/wait-bridge"
SEAT_KIT = REPOSITORY / "tools/seat-kit/seat_new.py"
DEFAULT_STATE_DIR = Path("~/.pursers/fleet-dashboard").expanduser()
DEFAULT_INVENTORY = DEFAULT_STATE_DIR / "seats.json"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MANAGED_COMMENT = "# pursers-managed; edit through the fleet dashboard"
CAPABILITY_ENV = {
    "tier_max": "PURSERS_TIER_MAX",
    "skills": "PURSERS_SKILLS",
    "can_review": "PURSERS_CAN_REVIEW",
    "can_work": "PURSERS_CAN_WORK",
    "model": "PURSERS_MODEL",
    "provider": "PURSERS_PROVIDER",
}
CONNECTOR_SKILLS = {
    "github": "git",
    "gitlab": "git",
    "playwright": "browser",
    "chrome": "browser",
    "browser": "browser",
    "sharepoint": "documents",
    "onedrive": "documents",
    "pubmed": "research",
}


@dataclass(frozen=True)
class HostProfile:
    host_timeout_s: int
    block_s: int
    config_knob: str


HOST_PROFILES: dict[str, HostProfile] = {
    "codex": HostProfile(620, 560, "tool_timeout_sec"),
    "codex-cli": HostProfile(620, 560, "tool_timeout_sec"),
    "goose": HostProfile(300, 270, "timeout"),
    "claude-code": HostProfile(21_600, 21_540, "rotation_s"),
    "claude-desktop": HostProfile(240, 200, "host_deadline_s"),
    "headless": HostProfile(21_600, 21_540, "rotation_s"),
}


def wait_bridge_host_timeouts(path: Path | None = None) -> dict[str, int]:
    """Read the bridge table without importing its transport dependencies."""
    source = (path or WAIT_BRIDGE / "pursers_wait_server.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "HOST_TIMEOUTS_S"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            return {str(key): int(item) for key, item in value.items()}
    raise ValueError("wait bridge HOST_TIMEOUTS_S table is missing")


def _bridge_version() -> str:
    manifest_path = REPOSITORY / "tools/release_versions.toml"
    if manifest_path.is_file():
        try:
            document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            version = document.get("packages", {}).get("wait_bridge")
            if version:
                return str(version)
        except Exception:
            pass
    document = tomllib.loads((WAIT_BRIDGE / "pyproject.toml").read_text())
    return str(document["project"]["version"])


def _pinned_bridge_version() -> str:
    return _bridge_version()


def _package_version(project: str) -> str:
    manifest_path = REPOSITORY / "tools/release_versions.toml"
    if manifest_path.is_file():
        try:
            document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            version = document.get("packages", {}).get(project)
            if version:
                return str(version)
        except Exception:
            pass
    document = tomllib.loads(
        (REPOSITORY / f"packages/{project}/pyproject.toml").read_text()
    )
    return str(document["project"]["version"])


JWT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
NVM_NPX_PATTERN = re.compile(
    r"(/[^\"\'\n\)]*?\.nvm/versions/node/[^/\"\'\n\)]+/bin/npx\b)"
)


def _fetch_latest_pypi_version(
    package: str = "pursers-wait-bridge", timeout_s: float = 3.0
) -> str | None:
    """Query PyPI JSON API for latest package version. Returns None if unreachable."""
    try:
        import urllib.request

        req = urllib.request.Request(
            f"https://pypi.org/pypi/{package}/json",
            headers={"User-Agent": "pursers-fleet-dashboard"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                version = data.get("info", {}).get("version")
                return str(version) if version else None
    except Exception:
        return None
    return None


def _resolve_command_executable(command: str | Path | None) -> Path | None:
    if not command:
        return None
    path = Path(command).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return path
    found = shutil.which(str(command))
    if found:
        found_path = Path(found)
        if found_path.is_file() and os.access(found_path, os.X_OK):
            return found_path
    return None


def _default_bridge_configs() -> tuple[tuple[str, Path], ...]:
    return (
        ("codex", Path("~/.codex/config.toml").expanduser()),
        ("goose", Path("~/.config/goose/config.yaml").expanduser()),
        (
            "claude-desktop",
            Path(
                "~/Library/Application Support/Claude/claude_desktop_config.json"
            ).expanduser(),
        ),
    )


def _bridge_candidates_from_config(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    values: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            if key.casefold() in {"command", "cmd", "pursers_bridge_command"}:
                values.append(value)
            values.extend(
                token.strip("\"'")
                for token in shlex.split(value, posix=True)
                if "pursers-wait-bridge" in token
            )

    try:
        if path.suffix == ".toml":
            visit(tomllib.loads(text))
        elif path.suffix == ".json":
            visit(json.loads(text))
    except (ValueError, tomllib.TOMLDecodeError):
        pass
    for match in re.finditer(
        r"(?im)^\s*(?:command|cmd|PURSERS_BRIDGE_COMMAND)\s*[:=]\s*"
        r"(?:[\"']([^\"']+)[\"']|([^\s#,]+))",
        text,
    ):
        values.append(match.group(1) or match.group(2))
    candidates: list[str] = []
    for value in values:
        candidate = value.strip()
        if candidate and Path(candidate).name == "pursers-wait-bridge":
            expanded = str(Path(candidate).expanduser())
            if expanded not in candidates:
                candidates.append(expanded)
    return candidates


def _query_bridge_version(
    executable: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Deterministically read installed bridge version by running `<shim> --version`."""
    try:
        result = runner(
            [str(executable), "--version"],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0].strip().split()[-1]
    except Exception:
        pass
    return None


def _query_bridge_distribution_version(
    executable: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    interpreters: list[Path] = []
    try:
        first_line = executable.read_text(encoding="utf-8").splitlines()[0]
        if first_line.startswith("#!"):
            shebang = first_line[2:].strip()
            if shebang and " " not in shebang:
                interpreters.append(Path(shebang))
    except (OSError, UnicodeError, IndexError):
        pass
    interpreters.extend((executable.parent / "python", executable.parent / "python3"))
    seen: set[Path] = set()
    for interpreter in interpreters:
        if interpreter in seen or not interpreter.is_file():
            continue
        seen.add(interpreter)
        try:
            result = runner(
                [
                    str(interpreter),
                    "-c",
                    (
                        "from importlib.metadata import version; "
                        "print(version('pursers-wait-bridge'))"
                    ),
                ],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except Exception:
            pass
    return None


def _validate_token_file(path_raw: str | Path) -> tuple[bool, str]:
    """Validate token file existence, readability, non-emptiness, and JWT shape.

    CRITICAL: Never print or log token content under any circumstances.
    """
    path = Path(path_raw).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        return False, "missing or unreadable"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception:
        return False, "missing or unreadable"
    if not content:
        return False, "token file is empty"
    if not JWT_PATTERN.fullmatch(content):
        return False, "invalid JWT format (must have three base64url parts)"
    return True, "readable; valid JWT"


def _inspect_env_var(
    var_name: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str]:
    """Inspect an environment variable without depending on a specific shell.

    launchctl setenv is the durable option for GUI apps on macOS.
    """
    if not ENV_NAME.fullmatch(var_name):
        return "FAIL", f"invalid environment variable name {var_name!r}"
    if os.environ.get(var_name):
        return "PASS", f"environment variable {var_name!r} defined; source=process"

    candidates = [os.environ.get("SHELL", "").strip(), "zsh", "bash"]
    searched: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in searched:
            continue
        searched.append(candidate)
        shell = shutil.which(candidate)
        if not shell:
            continue
        try:
            res = runner(
                [shell, "-lic", f'printf %s "${{{var_name}:+set}}"'],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            continue
        if res.returncode != 0:
            continue
        if res.stdout.strip() == "set":
            return (
                "PASS",
                f"environment variable {var_name!r} defined; "
                f"source=login-shell ({shell})",
            )
        return (
            "FAIL",
            f"environment variable {var_name!r} is not defined; "
            f"source=login-shell ({shell})",
        )
    return (
        "WARN",
        f"cannot inspect login shell for environment variable {var_name!r} "
        f"(searched {', '.join(searched)})",
    )


def _env_value_digest(
    var_name: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str | None, str]:
    """Read only a SHA-256 digest from process or login-shell state."""
    if not ENV_NAME.fullmatch(var_name):
        return "FAIL", None, f"invalid environment variable name {var_name!r}"
    value = os.environ.get(var_name)
    if value:
        return "PASS", hashlib.sha256(value.encode()).hexdigest(), "process"
    code = (
        "import hashlib,os,sys;v=os.environ.get(sys.argv[1],'');"
        "print(hashlib.sha256(v.encode()).hexdigest() if v else '')"
    )
    command = shlex.join([sys.executable, "-c", code, var_name])
    candidates = [os.environ.get("SHELL", "").strip(), "zsh", "bash"]
    searched: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in searched:
            continue
        searched.append(candidate)
        shell = shutil.which(candidate)
        if not shell:
            continue
        try:
            result = runner(
                [shell, "-lic", command],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            continue
        digest = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}", digest):
            return "PASS", digest, f"login-shell ({shell})"
        if result.returncode == 0:
            return "FAIL", None, f"login-shell ({shell})"
    return "FAIL", None, f"unavailable (searched {', '.join(searched)})"


CAT_COMMAND_PATTERN = re.compile(r'\$\(\s*(cat\s+[^)\n]+)\)')


def _system_trust_files() -> set[Path]:
    targets: set[Path] = set()
    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.openssl_cafile:
        targets.add(Path(verify_paths.openssl_cafile).expanduser())
    for standard in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/ssl/ca-bundle.pem",
    ):
        targets.add(Path(standard))
    resolved_targets: set[Path] = set()
    for target in targets:
        try:
            resolved_targets.add(target.resolve() if target.exists() else target)
        except Exception:
            resolved_targets.add(target)
    return resolved_targets | targets


def _is_private_ca(ca_raw: str | Path | None) -> bool:
    """Determine whether a CA path points to a private CA rather than an actual default trust target."""
    if not ca_raw:
        return False
    path = Path(ca_raw).expanduser()
    try:
        resolved = path.resolve() if path.exists() else path
    except Exception:
        resolved = path

    system_files = _system_trust_files()
    if path in system_files or resolved in system_files:
        return False

    return True


def _extract_token_file_references(
    desired: DesiredSeat, inspection: dict[str, Any]
) -> list[str]:
    files: list[str] = []
    if desired.token_file:
        files.append(desired.token_file)

    def scan_for_cat(text: str) -> None:
        if not text:
            return
        cleaned = text.replace('\\"', '"').replace("\\'", "'")
        for m in CAT_COMMAND_PATTERN.finditer(cleaned):
            cmd = m.group(1).strip()
            try:
                parts = shlex.split(cmd)
                if len(parts) >= 2 and parts[0] == "cat":
                    for p in parts[1:]:
                        if p and not p.startswith("-"):
                            files.append(p)
            except Exception:
                pass

    doc = inspection.get("document", {})
    if isinstance(doc, dict):
        for srv in doc.get("mcp_servers", {}).values():
            if isinstance(srv, dict):
                env = srv.get("env", {})
                if isinstance(env, dict):
                    if "ONBOARD_CENTRAL_TOKEN_FILE" in env:
                        files.append(str(env["ONBOARD_CENTRAL_TOKEN_FILE"]))
                    for v in env.values():
                        if isinstance(v, str):
                            scan_for_cat(v)
                raw_args = srv.get("args", [])
                if isinstance(raw_args, list):
                    for arg in raw_args:
                        if isinstance(arg, str):
                            scan_for_cat(arg)
                cmd = srv.get("command", "")
                if isinstance(cmd, str):
                    scan_for_cat(cmd)
        for srv in doc.get("mcpServers", {}).values():
            if isinstance(srv, dict):
                env = srv.get("env", {})
                if isinstance(env, dict):
                    if "ONBOARD_CENTRAL_TOKEN_FILE" in env:
                        files.append(str(env["ONBOARD_CENTRAL_TOKEN_FILE"]))
                    for v in env.values():
                        if isinstance(v, str):
                            scan_for_cat(v)
                raw_args = srv.get("args", [])
                if isinstance(raw_args, list):
                    for arg in raw_args:
                        if isinstance(arg, str):
                            scan_for_cat(arg)
                cmd = srv.get("command", "")
                if isinstance(cmd, str):
                    scan_for_cat(cmd)

    text = inspection.get("text", "")
    if isinstance(text, str) and text:
        for m in re.finditer(
            r"ONBOARD_CENTRAL_TOKEN_FILE:\s*[\"']?([^\"'\n]+)[\"']?", text
        ):
            files.append(m.group(1).strip())
        scan_for_cat(text)

    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            result.append(f)
    return result


def _extract_env_token_references(
    desired: DesiredSeat, inspection: dict[str, Any]
) -> list[str]:
    vars_: list[str] = []
    if desired.host in {"codex", "codex-cli"}:
        doc = inspection.get("document", {})
        bearer_var = (
            doc.get("mcp_servers", {})
            .get("pursers-dev", {})
            .get("bearer_token_env_var")
        )
        vars_.append(bearer_var or desired.token_env_var)

    text = inspection.get("text", "")
    if isinstance(text, str) and text:
        for m in re.finditer(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*TOKEN[A-Za-z0-9_]*)\s*:", text, re.M
        ):
            var_name = m.group(1).strip()
            if not var_name.endswith("_FILE"):
                vars_.append(var_name)

    doc = inspection.get("document", {})
    if isinstance(doc, dict):
        for srv in doc.get("mcpServers", {}).values():
            if isinstance(srv, dict):
                env = srv.get("env", {})
                if isinstance(env, dict):
                    for k in env:
                        if "TOKEN" in k and not k.endswith("_FILE"):
                            vars_.append(k)

    seen: set[str] = set()
    result: list[str] = []
    for v in vars_:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _extract_nvm_npx_paths(inspection: dict[str, Any]) -> list[str]:
    found: list[str] = []
    doc = inspection.get("document", {})
    if isinstance(doc, dict):
        serialized = json.dumps(doc)
        found.extend(NVM_NPX_PATTERN.findall(serialized))
    text = inspection.get("text", "")
    if isinstance(text, str):
        found.extend(NVM_NPX_PATTERN.findall(text))
    seen: set[str] = set()
    result: list[str] = []
    for p in found:
        path_str = p[0] if isinstance(p, tuple) else p
        if path_str and path_str not in seen:
            seen.add(path_str)
            result.append(path_str)
    return result


def _load_seat_new() -> Any:
    spec = importlib.util.spec_from_file_location("pursers_seat_new", SEAT_KIT)
    if spec is None or spec.loader is None:
        raise RuntimeError("seat-kit cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class DesiredSeat:
    host: str
    role: str
    name: str
    central_url: str
    home_board: str
    token_file: str
    ca_file: str
    bridge_command: str
    config_path: str
    seat_dir: str | None = None
    repository: str | None = None
    personal_command: str = "pursers-personal"
    token_env_var: str = "ONBOARD_CENTRAL_TOKEN"
    bridge_name: str | None = None
    tier_max: int = 2
    skills: tuple[str, ...] = ()
    can_review: bool | None = None
    can_work: bool = True
    model: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        if self.host not in HOST_PROFILES:
            raise ValueError(f"unsupported host: {self.host}")
        if self.role not in {"worker", "reviewer", "orchestrator", "coordinator"}:
            raise ValueError(
                "role must be worker, reviewer, orchestrator, or coordinator"
            )
        if not SAFE_NAME.fullmatch(self.name):
            raise ValueError("seat name must be a safe 1-80 character identifier")
        if not SAFE_NAME.fullmatch(self.home_board):
            raise ValueError("home board must be a safe 1-80 character identifier")
        if not ENV_NAME.fullmatch(self.token_env_var):
            raise ValueError("token env var must be a safe identifier")
        if isinstance(self.tier_max, bool) or self.tier_max not in {1, 2, 3}:
            raise ValueError("tier_max must be 1, 2, or 3")
        normalized_skills = tuple(
            sorted({str(skill).strip().lower() for skill in self.skills if str(skill).strip()})
        )
        if any(not SAFE_NAME.fullmatch(skill) for skill in normalized_skills):
            raise ValueError("skills must contain safe identifiers")
        object.__setattr__(self, "skills", normalized_skills)
        if self.can_review is None:
            object.__setattr__(
                self,
                "can_review",
                self.role == "reviewer" and self.tier_max > 1,
            )
        if not isinstance(self.can_review, bool) or not isinstance(self.can_work, bool):
            raise ValueError("can_review and can_work must be boolean")
        for field_name in ("model", "provider"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError(f"{field_name} must be a string up to 200 characters")

    @property
    def connector_name(self) -> str:
        return self.bridge_name or f"pursers-wait-{self.name}"

    @property
    def profile(self) -> HostProfile:
        return HOST_PROFILES[self.host]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DesiredSeat":
        allowed = {item.name for item in fields(cls)}
        selected = {key: item for key, item in value.items() if key in allowed}
        skills = selected.get("skills", ())
        if isinstance(skills, list):
            selected["skills"] = tuple(skills)
        return cls(**selected)

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "tier_max": self.tier_max,
            "skills": list(self.skills),
            "can_review": self.can_review,
            "can_work": self.can_work,
            "model": self.model,
            "provider": self.provider,
            "host": self.host,
        }


def capability_env(desired: DesiredSeat) -> dict[str, str]:
    values = {
        "tier_max": str(desired.tier_max),
        "skills": ",".join(desired.skills),
        "can_review": str(desired.can_review).lower(),
        "can_work": str(desired.can_work).lower(),
        "model": desired.model or "",
        "provider": desired.provider or "",
    }
    return {CAPABILITY_ENV[key]: value for key, value in values.items()}


def connector_skill_suggestions(inspection: dict[str, Any]) -> list[str]:
    """Map configured connector names to editable, non-secret skill hints."""
    names: list[str] = []
    document = inspection.get("document")
    if isinstance(document, dict):
        for key in ("mcp_servers", "mcpServers"):
            servers = document.get(key)
            if isinstance(servers, dict):
                names.extend(str(name).casefold() for name in servers)
    text = inspection.get("text")
    if isinstance(text, str):
        names.extend(
            match.group(1).casefold()
            for match in re.finditer(r"^\s{2}([A-Za-z0-9._-]+):\s*$", text, re.M)
        )
    suggestions = {
        skill
        for name in names
        for marker, skill in CONNECTOR_SKILLS.items()
        if marker in name
    }
    return sorted(suggestions)


@dataclass(frozen=True)
class Change:
    path: Path
    description: str
    before: str | None
    after: str | None
    mode: int = 0o600
    action: str = "write"

    def summary(self) -> str:
        state = "create" if self.before is None else "update"
        return f"{state} {self.path}: {self.description}"


@dataclass(frozen=True)
class ApplyResult:
    changed: tuple[str, ...]
    backups: tuple[str, ...]


@dataclass(frozen=True)
class DoctorCheck:
    seat: str
    check: str
    status: str
    message: str


class HostAdapter(Protocol):
    def inspect(self) -> dict[str, Any]: ...

    def plan(self, desired: DesiredSeat) -> list[Change]: ...

    def apply(self, plan: Sequence[Change]) -> ApplyResult: ...


def _backup_name(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    return candidate


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FileAdapter:
    def apply(self, plan: Sequence[Change]) -> ApplyResult:
        changed: list[str] = []
        backups: list[str] = []
        for change in plan:
            if change.action == "git-ff":
                subprocess.run(
                    ["git", "pull", "--ff-only"], cwd=change.path, check=True
                )
                changed.append(str(change.path))
                continue
            if change.action == "personal-install":
                command = change.path
                python = command.parent / "python"
                if not python.exists():
                    subprocess.run(
                        [sys.executable, "-m", "venv", str(command.parent.parent)],
                        check=True,
                    )
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        f"pursers-personal=={_package_version('personal')}",
                        f"pursers-central=={_package_version('central')}",
                        f"pursers-client=={_package_version('client')}",
                    ],
                    check=True,
                )
                changed.append(str(command))
                continue
            if change.after is None:
                continue
            path = change.path
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current == change.after:
                continue
            if current is not None:
                backup = _backup_name(path)
                shutil.copy2(path, backup)
                backups.append(str(backup))
            _atomic_write(path, change.after, change.mode)
            changed.append(str(path))
        return ApplyResult(tuple(changed), tuple(backups))


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _remove_toml_tables(text: str, names: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropping = False
    heading = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
    for line in lines:
        match = heading.match(line.rstrip("\n"))
        if match:
            dropping = match.group(1) in names
        if not dropping:
            kept.append(line)
    return "".join(kept).rstrip() + "\n"


def _bridge_shell_args() -> list[str]:
    script = (
        'connector_token=${ONBOARD_CENTRAL_TOKEN-}; '
        'token=$(tr -d "\\r\\n" < "$ONBOARD_CENTRAL_TOKEN_FILE"); '
        'export PURSERS_BOARD_CONNECTOR_TOKEN="$connector_token"; '
        'export ONBOARD_CENTRAL_TOKEN="$token"; exec "$PURSERS_BRIDGE_COMMAND"'
    )
    return ["-c", script]


class CodexAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        try:
            document = tomllib.loads(text) if text.strip() else {}
            error = None
        except tomllib.TOMLDecodeError as exc:
            document, error = {}, str(exc)
        return {"path": str(self.path), "document": document, "error": error}

    def _render(self, desired: DesiredSeat) -> str:
        name = desired.connector_name
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("bridge connector name is unsafe")
        current = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        tables = {
            f"mcp_servers.{name}",
            f"mcp_servers.{name}.env",
            "mcp_servers.pursers-dev",
            "mcp_servers.pursers-dev.env_http_headers",
        }
        prefix = _remove_toml_tables(current, tables)
        prefix = "".join(
            line
            for line in prefix.splitlines(keepends=True)
            if line.strip() != MANAGED_COMMENT
        )
        block = f"""
[{f'mcp_servers.{name}'}]
command = "/bin/sh"
args = {_toml_array(_bridge_shell_args())}
tool_timeout_sec = {desired.profile.host_timeout_s}

[{f'mcp_servers.{name}.env'}]
PURSERS_BRIDGE_COMMAND = {_toml_string(desired.bridge_command)}
ONBOARD_CENTRAL_TOKEN_FILE = {_toml_string(desired.token_file)}
ONBOARD_CENTRAL_URL = {_toml_string(desired.central_url)}
ONBOARD_BOARD_ID = {_toml_string(desired.home_board)}
ONBOARD_AGENT_NAME = {_toml_string(desired.name)}
PURSERS_HOST = {_toml_string(desired.host)}
PURSERS_ROLE = {_toml_string(desired.role)}
PURSERS_TIER_MAX = {_toml_string(str(desired.tier_max))}
PURSERS_SKILLS = {_toml_string(','.join(desired.skills))}
PURSERS_CAN_REVIEW = {_toml_string(str(desired.can_review).lower())}
PURSERS_CAN_WORK = {_toml_string(str(desired.can_work).lower())}
PURSERS_MODEL = {_toml_string(desired.model or '')}
PURSERS_PROVIDER = {_toml_string(desired.provider or '')}
PURSERS_REQUIRE_TOKEN_MATCH = "1"
SSL_CERT_FILE = {_toml_string(desired.ca_file)}

[mcp_servers.pursers-dev]
url = {_toml_string(desired.central_url)}
bearer_token_env_var = {_toml_string(desired.token_env_var)}

[mcp_servers.pursers-dev.env_http_headers]
ONBOARD_BOARD_ID = {_toml_string(desired.home_board)}
"""
        result = prefix.rstrip() + "\n\n" + MANAGED_COMMENT + block
        tomllib.loads(result)
        return result

    def plan(self, desired: DesiredSeat) -> list[Change]:
        after = self._render(desired)
        before = self.path.read_text(encoding="utf-8") if self.path.exists() else None
        if before == after:
            return []
        return [Change(self.path, "Codex wait and board connectors", before, after)]


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _goose_block(desired: DesiredSeat) -> list[str]:
    name = desired.connector_name
    return [
        f"  {name}:\n",
        f"    {MANAGED_COMMENT}\n",
        f"    name: {_yaml_quote(name)}\n",
        "    type: stdio\n",
        "    enabled: true\n",
        f"    timeout: {desired.profile.host_timeout_s}\n",
        f"    cmd: {_yaml_quote('/bin/sh')}\n",
        "    args:\n",
        *[f"      - {_yaml_quote(item)}\n" for item in _bridge_shell_args()],
        "    envs:\n",
        f"      PURSERS_BRIDGE_COMMAND: {_yaml_quote(desired.bridge_command)}\n",
        f"      ONBOARD_CENTRAL_TOKEN_FILE: {_yaml_quote(desired.token_file)}\n",
        f"      ONBOARD_CENTRAL_URL: {_yaml_quote(desired.central_url)}\n",
        f"      ONBOARD_BOARD_ID: {_yaml_quote(desired.home_board)}\n",
        f"      ONBOARD_AGENT_NAME: {_yaml_quote(desired.name)}\n",
        "      PURSERS_HOST: goose\n",
        f"      PURSERS_ROLE: {_yaml_quote(desired.role)}\n",
        *[
            f"      {name}: {_yaml_quote(value)}\n"
            for name, value in capability_env(desired).items()
        ],
        f"      SSL_CERT_FILE: {_yaml_quote(desired.ca_file)}\n",
    ]


def _replace_goose_extension(text: str, desired: DesiredSeat) -> str:
    lines = text.splitlines(keepends=True)
    name = desired.connector_name
    extension_start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == "extensions:"),
        None,
    )
    block = _goose_block(desired)
    if extension_start is None:
        prefix = text.rstrip()
        return (prefix + "\n\n" if prefix else "") + "extensions:\n" + "".join(block)
    target = re.compile(rf"^  {re.escape(name)}:\s*(?:#.*)?$")
    start = next(
        (
            index
            for index in range(extension_start + 1, len(lines))
            if target.match(lines[index].rstrip("\n"))
        ),
        None,
    )
    if start is None:
        lines[extension_start + 1 : extension_start + 1] = block
        return "".join(lines)
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip(" ")) < 4:
            break
        end += 1
    lines[start:end] = block
    return "".join(lines)


def _seat_python(bridge_command: str) -> Path:
    command = Path(bridge_command).expanduser()
    resolved = command.resolve() if command.exists() else command
    candidate = resolved.parent / "python"
    return candidate if candidate.exists() else Path(sys.executable)


class GooseAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return {
            "path": str(self.path),
            "text": text,
            "managed": MANAGED_COMMENT in text,
        }

    def plan(self, desired: DesiredSeat) -> list[Change]:
        current = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        rendered = _replace_goose_extension(current, desired)
        changes: list[Change] = []
        if current != rendered:
            changes.append(
                Change(self.path, "Goose stdio extension", current or None, rendered)
            )
        if desired.seat_dir:
            seat_new = _load_seat_new()
            root = Path(desired.seat_dir).expanduser()
            repo_leaf = seat_new._repo_leaf(desired.repository) if desired.repository else None
            python = _seat_python(desired.bridge_command)
            files = {
                root / "bin/board.sh": (
                    seat_new._board_shell(
                        name=desired.name,
                        board=desired.home_board,
                        central_url=desired.central_url,
                        token_file=Path(desired.token_file).expanduser(),
                        ca_file=Path(desired.ca_file).expanduser(),
                        python=python,
                        tier_max=2,
                        skills="",
                        can_review=desired.role == "reviewer",
                        can_work=desired.role == "worker",
                        host="goose",
                    ),
                    0o755,
                ),
                root / "bin/board.py": (
                    seat_new._board_python(
                        desired.role,
                        repo_leaf,
                        desired.profile.block_s,
                    ),
                    0o644,
                ),
            }
            guidance = (
                PromptRenderer().render(desired)
                + "\n## Local seat CLI\n\n"
                + seat_new._instructions(
                    role=desired.role,
                    name=desired.name,
                    client="goose",
                )
            )
            files[root / "AGENTS.md"] = (guidance, 0o644)
            files[root / ".goosehints"] = (guidance, 0o644)
            for path, (after, mode) in files.items():
                before = path.read_text(encoding="utf-8") if path.exists() else None
                if before != after:
                    changes.append(
                        Change(path, "managed Goose seat file", before, after, mode)
                    )
            if repo_leaf:
                clone = root / repo_leaf
                status = (
                    subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=clone,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    if clone.is_dir()
                    else None
                )
                if (
                    status is not None
                    and status.returncode == 0
                    and not status.stdout.strip()
                ):
                    behind = subprocess.run(
                        ["git", "rev-list", "--count", "HEAD..@{upstream}"],
                        cwd=clone,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                    if (
                        behind.returncode == 0
                        and int(behind.stdout.strip() or "0") > 0
                    ):
                        changes.append(
                            Change(
                                clone,
                                "fast-forward clean clone",
                                None,
                                None,
                                action="git-ff",
                            )
                        )
        return changes

    def apply(self, plan: Sequence[Change]) -> ApplyResult:
        result = super().apply(plan)
        for changed in result.changed:
            path = Path(changed)
            if path.name == "board.sh":
                path.chmod(0o755)
        return result


def _json_document(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration root must be an object: {path}")
    return value


def _bridge_json(desired: DesiredSeat) -> dict[str, Any]:
    env = {
        "PURSERS_BRIDGE_COMMAND": desired.bridge_command,
        "ONBOARD_CENTRAL_TOKEN_FILE": desired.token_file,
        "ONBOARD_CENTRAL_URL": desired.central_url,
        "ONBOARD_BOARD_ID": desired.home_board,
        "ONBOARD_AGENT_NAME": desired.name,
        "PURSERS_HOST": desired.host,
        "PURSERS_ROLE": desired.role,
        "SSL_CERT_FILE": desired.ca_file,
        **capability_env(desired),
    }
    return {
        "command": "/bin/sh",
        "args": _bridge_shell_args(),
        "env": env,
    }


class JsonHostAdapter(FileAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def inspect(self) -> dict[str, Any]:
        try:
            return {"path": str(self.path), "document": _json_document(self.path), "error": None}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"path": str(self.path), "document": {}, "error": str(exc)}

    def _document(self, desired: DesiredSeat) -> dict[str, Any]:
        document = _json_document(self.path)
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object")
        servers[desired.connector_name] = _bridge_json(desired)
        return document

    def plan(self, desired: DesiredSeat) -> list[Change]:
        document = self._document(desired)
        rendered = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        before = self.path.read_text(encoding="utf-8") if self.path.exists() else None
        if before == rendered:
            return []
        return [Change(self.path, "MCP server entries", before, rendered)]


class ClaudeDesktopAdapter(JsonHostAdapter):
    def _document(self, desired: DesiredSeat) -> dict[str, Any]:
        document = super()._document(desired)
        document["mcpServers"]["pursers-personal"] = {
            "command": desired.personal_command,
            "args": ["mcp", "--host-id", "claude-desktop", "--session", desired.name],
        }
        return document

    def plan(self, desired: DesiredSeat) -> list[Change]:
        changes = super().plan(desired)
        command = Path(desired.personal_command).expanduser()
        if command.is_absolute():
            actual = "missing"
            if command.is_file() and os.access(command, os.X_OK):
                result = subprocess.run(
                    [str(command), "--version"],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    actual = result.stdout.strip()
            expected = _package_version("personal")
            if actual != expected:
                changes.append(
                    Change(
                        command,
                        "install pinned Personal dashboard venv",
                        actual,
                        expected,
                        action="personal-install",
                    )
                )
        return changes


class ClaudeCodeAdapter(JsonHostAdapter):
    def __init__(self, path: str | Path):
        self.raw_path = str(path)
        super().__init__(path or ".mcp.json")

    def command(self, desired: DesiredSeat) -> str:
        payload = json.dumps(_bridge_json(desired), separators=(",", ":"))
        return (
            "claude mcp add-json "
            + shlex.quote(desired.connector_name)
            + " "
            + shlex.quote(payload)
        )

    def inspect(self) -> dict[str, Any]:
        if not self.raw_path:
            return {
                "path": None,
                "document": {},
                "error": None,
                "write_enabled": False,
            }
        result = super().inspect()
        result["write_enabled"] = True
        return result

    def plan(self, desired: DesiredSeat) -> list[Change]:
        if not self.raw_path:
            return []
        return super().plan(desired)


class BridgeInstaller:
    """Installs, upgrades, and inspects the pursers-wait-bridge tool.

    Bridge version inspection deterministically executes the resolved bridge
    shim or binary with `--version` and, when possible, reads the distribution
    metadata from that shim's interpreter.
    """

    def __init__(
        self,
        version: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        command: str | Path | None = None,
        pypi_fetcher: Callable[[], str | None] | None = None,
        discovered_configs: Sequence[tuple[str, str | Path]] | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.version = version or _pinned_bridge_version()
        self.runner = runner
        self.command = command
        self.pypi_fetcher = pypi_fetcher
        self.home = Path(home).expanduser() if home is not None else Path.home()
        self.discovered_configs = tuple(
            (host, Path(path).expanduser())
            for host, path in (
                _default_bridge_configs()
                if discovered_configs is None
                else discovered_configs
            )
        )

    def _resolve(
        self, command: str | Path | None = None
    ) -> tuple[Path | None, str | None, list[str]]:
        searched: list[str] = []

        def resolve(candidate: str | Path | None, source: str) -> tuple[Path, str] | None:
            if not candidate:
                return None
            searched.append(f"{source}:{candidate}")
            executable = _resolve_command_executable(candidate)
            return (executable, source) if executable else None

        explicit = command or self.command
        found = resolve(explicit, "explicit")
        if found:
            return found[0], found[1], searched
        for host, config_path in self.discovered_configs:
            searched.append(f"config:{host}:{config_path}")
            for candidate in _bridge_candidates_from_config(config_path):
                found = resolve(candidate, f"config:{host}")
                if found:
                    return found[0], found[1], searched
        found = resolve("pursers-wait-bridge", "PATH")
        if found:
            return found[0], found[1], searched
        for label, candidate in (
            ("well-known:uv-bin", self.home / ".local/bin/pursers-wait-bridge"),
            (
                "well-known:uv-tool",
                self.home
                / ".local/share/uv/tools/pursers-wait-bridge/bin/"
                "pursers-wait-bridge",
            ),
        ):
            found = resolve(candidate, label)
            if found:
                return found[0], found[1], searched
        uv = shutil.which("uv")
        if uv:
            searched.append("uv-tool-dir")
            try:
                result = self.runner(
                    [uv, "tool", "dir"],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    candidate = (
                        Path(result.stdout.strip())
                        / "pursers-wait-bridge/bin/pursers-wait-bridge"
                    )
                    found = resolve(candidate, "uv-tool-dir")
                    if found:
                        return found[0], found[1], searched
            except Exception:
                pass
        return None, None, searched

    def inspect(self, command: str | Path | None = None) -> dict[str, Any]:
        resolved, resolution_source, searched = self._resolve(command)
        reported_version = (
            _query_bridge_version(resolved, self.runner) if resolved else None
        )
        metadata_version = (
            _query_bridge_distribution_version(resolved, self.runner)
            if resolved else None
        )
        installed_version = metadata_version or reported_version
        pinned_version = self.version

        pypi_version = (
            self.pypi_fetcher()
            if self.pypi_fetcher is not None
            else _fetch_latest_pypi_version()
        )

        if resolved is None or reported_version is None:
            status = "FAIL"
            message = "bridge command does not resolve or failed to report version"
            if resolved is None:
                message += "; searched " + ", ".join(searched)
        elif installed_version != pinned_version:
            status = "FAIL"
            message = f"installed={installed_version}; pinned={pinned_version}"
        elif metadata_version and reported_version != metadata_version:
            status = "WARN"
            message = (
                "version string stale; "
                f"reported={reported_version}; package={metadata_version}"
            )
        elif pypi_version is None:
            status = "WARN"
            message = (
                f"installed={installed_version}; pinned={pinned_version}; PyPI unreachable"
            )
        else:
            status = "PASS"
            message = f"version {installed_version}"

        return {
            "command": str(resolved) if resolved else None,
            "installed": installed_version is not None,
            "installed_version": installed_version,
            "reported_version": reported_version,
            "package_metadata_version": metadata_version,
            "pinned_version": pinned_version,
            "latest_pypi_version": pypi_version,
            "resolution_source": resolution_source,
            "searched": searched,
            "version": installed_version or pinned_version,
            "status": status,
            "message": message,
            "private_ca_active": _is_private_ca(os.environ.get("SSL_CERT_FILE")),
        }

    def install(self) -> str:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required to install the wait bridge")
        environment = os.environ.copy()
        environment.pop("SSL_CERT_FILE", None)
        self.runner(
            [
                uv,
                "tool",
                "install",
                "--force",
                f"pursers-wait-bridge=={self.version}",
            ],
            check=True,
            env=environment,
            text=True,
            capture_output=True,
        )
        command, _source, searched = self._resolve()
        if command is None:
            raise RuntimeError(
                "bridge installed but its shim does not resolve; searched "
                + ", ".join(searched)
            )
        return str(command)


class PromptRenderer:
    def render(self, desired: DesiredSeat) -> str:
        host_note = {
            "codex": "This name is bound to this Codex window; never reuse it in another window.",
            "codex-cli": "This name is bound to this Codex CLI session.",
            "goose": "Run from the generated Goose seat folder and use its pinned interpreter.",
            "claude-desktop": (
                "Claude Desktop uses a 200s bridge block under its 240s host deadline."
            ),
            "claude-code": "Claude Code emits progress every 300s during its long rotation.",
            "headless": "Invoke a model only after an actionable cue.",
        }[desired.host]
        capability_note = (
            f"Declared capabilities: tier_max={desired.tier_max}; "
            f"skills={','.join(desired.skills) or 'none'}; "
            f"can_work={str(desired.can_work).lower()}; "
            f"can_review={str(desired.can_review).lower()}; "
            f"model={desired.model or 'unspecified'}; "
            f"provider={desired.provider or 'unspecified'}."
        )
        if desired.role == "orchestrator":
            return (
                f"You are Pursers seat {desired.name} ({desired.role}).\n"
                f"Pass agent_name={json.dumps(desired.name)} on every board call that accepts it. Never use another name.\n"
                "start every turn with board_digest; act on closed tickets (merge/verify), file follow-ups, then board_digest_ack; never a2a_wait; never claim.\n"
                "Never review your own work, never push main, stay in the registered work_dir for the event's board_id, and report evidence faithfully.\n"
                f"{host_note}\n{capability_note}"
            )
        action = (
            "claim, implement, test, commit, push, and submit one ticket"
            if desired.role == "worker"
            else "review submitted work independently; never claim, edit, commit, or push"
        )
        template = (Path(__file__).with_name("seat_prompt_template.txt")).read_text(
            encoding="utf-8"
        )
        return template.format(
            name=desired.name,
            role=desired.role,
            quoted_name=json.dumps(desired.name),
            timeout_s=desired.profile.block_s,
            action=action,
            wait_for=("claimable" if desired.role == "worker" else "submitted"),
            host_note=f"{host_note}\n{capability_note}",
        )


class SeatInventory:
    def __init__(self, path: str | Path = DEFAULT_INVENTORY):
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "seats": []}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("seats"), list):
            raise ValueError("seats.json must be a schema 1 inventory")
        return document

    def save(self, document: dict[str, Any]) -> None:
        _atomic_write(
            self.path,
            json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            0o600,
        )

    def upsert(
        self,
        desired: DesiredSeat,
        *,
        bridge_version: str,
        doctor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = self.load()
        record = {
            **asdict(desired),
            "bridge_version": bridge_version,
            "last_doctor": doctor,
        }
        seats = [row for row in document["seats"] if row.get("name") != desired.name]
        seats.append(record)
        document["seats"] = sorted(seats, key=lambda row: row["name"])
        self.save(document)
        return record


def adapter_for(desired: DesiredSeat) -> HostAdapter:
    if desired.host in {"codex", "codex-cli"}:
        return CodexAdapter(desired.config_path)
    if desired.host == "goose":
        return GooseAdapter(desired.config_path)
    if desired.host == "claude-desktop":
        return ClaudeDesktopAdapter(desired.config_path)
    if desired.host == "claude-code":
        return ClaudeCodeAdapter(desired.config_path)
    return ClaudeCodeAdapter(desired.config_path)


LiveProbe = Callable[[DesiredSeat, float], dict[str, Any]]


def _default_live_probe(desired: DesiredSeat, timeout_s: float) -> dict[str, Any]:
    """Probe board_status plus one real subscription handshake without leaking JWTs."""
    client_src = REPOSITORY / "packages/client/src"
    if str(client_src) not in sys.path:
        sys.path.insert(0, str(client_src))
    from pursers_client import BoardClient

    token = Path(desired.token_file).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("token file is empty")

    async def probe() -> dict[str, Any]:
        subscribed = asyncio.Event()
        async with BoardClient(
            desired.central_url,
            token,
            desired.home_board,
            agent_name=desired.name,
            role=desired.role,
        ) as client:
            status = await client.board_status()
            state = await client.board_state_get("project_registry")
            registry_boards = [desired.home_board]
            raw = state.get("state", {}).get("value") if isinstance(state, dict) else None
            if isinstance(raw, str):
                try:
                    registry = json.loads(raw)
                    for row in registry.get("projects", {}).values():
                        board = row.get("board_id") if isinstance(row, dict) else None
                        if isinstance(board, str) and board not in registry_boards:
                            registry_boards.append(board)
                except (AttributeError, json.JSONDecodeError):
                    pass
            identity = client.identity
            if identity is None:
                raise RuntimeError("board join returned no identity")
            central_agent = next(
                (
                    row
                    for row in status.get("agents", [])
                    if isinstance(row, dict)
                    and row.get("agent_id") == identity.agent_id
                ),
                None,
            )
            resources = (
                f"board://{desired.home_board}/journal",
                f"board://{desired.home_board}/agent/{identity.agent_id}",
            )

            async def listen() -> None:
                async for _event in client.events(
                    from_cursor=status.get("latest_seq"),
                    only_mine=False,
                    resource_subscriptions=resources,
                    acknowledge=False,
                    touch=False,
                    subscription_callback=subscribed.set,
                ):
                    break

            task = asyncio.create_task(listen())
            try:
                await subscribed.wait()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            available = [desired.home_board]
            skipped: dict[str, str] = {}
            for board in registry_boards[1:]:
                try:
                    async with BoardClient(
                        desired.central_url,
                        token,
                        board,
                        agent_name=desired.name,
                        role=desired.role,
                    ) as board_client:
                        await board_client.board_status()
                    available.append(board)
                except Exception as exc:
                    skipped[board] = type(exc).__name__
            result = {
                "mode": "push",
                "registry_boards": available,
                "skipped_boards": skipped,
            }
            if central_agent is not None:
                result["central_agent"] = central_agent
            return result

    return asyncio.run(asyncio.wait_for(probe(), timeout=timeout_s))


class Doctor:
    def __init__(
        self,
        *,
        live_probe: LiveProbe | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.time,
        pypi_fetcher: Callable[[], str | None] | None = None,
    ) -> None:
        self.live_probe = live_probe or _default_live_probe
        self.runner = runner
        self.clock = clock
        self.pypi_fetcher = pypi_fetcher

    def _check(
        self,
        desired: DesiredSeat,
        name: str,
        status: str,
        message: str,
    ) -> DoctorCheck:
        return DoctorCheck(desired.name, name, status, message)

    def run(self, desired: DesiredSeat) -> list[DoctorCheck]:
        rows: list[DoctorCheck] = []
        adapter = adapter_for(desired)
        inspection = adapter.inspect()
        if inspection.get("error"):
            rows.append(
                self._check(desired, "config", "FAIL", "configuration is invalid")
            )
        else:
            drift = adapter.plan(desired)
            config_drift = [change for change in drift if change.action == "write"]
            rows.append(
                self._check(
                    desired,
                    "config",
                    "PASS" if not config_drift else "FAIL",
                    (
                        "configuration matches"
                        if not config_drift
                        else f"{len(config_drift)} change(s) required"
                    ),
                )
            )
            personal_repairs = [
                change for change in drift if change.action == "personal-install"
            ]
            if desired.host == "claude-desktop" and Path(
                desired.personal_command
            ).is_absolute():
                rows.append(
                    self._check(
                        desired,
                        "personal-venv",
                        "FAIL" if personal_repairs else "PASS",
                        (
                            "pinned install required"
                            if personal_repairs
                            else f"version {_package_version('personal')}"
                        ),
                    )
                )
        timeout = desired.profile.host_timeout_s
        if desired.host in {"codex", "codex-cli"}:
            timeout = (
                inspection.get("document", {})
                .get("mcp_servers", {})
                .get(desired.connector_name, {})
                .get("tool_timeout_sec", 0)
            )
        elif desired.host == "goose":
            match = re.search(
                rf"(?ms)^  {re.escape(desired.connector_name)}:\s*$.*?^    timeout:\s*(\d+)\s*$",
                inspection.get("text", ""),
            )
            timeout = int(match.group(1)) if match else 0
        timeout_ok = isinstance(timeout, int) and timeout >= desired.profile.host_timeout_s
        rows.append(
            self._check(
                desired,
                "host-timeout",
                "PASS" if timeout_ok else "FAIL",
                f"configured={timeout}; required>={desired.profile.host_timeout_s}",
            )
        )
        token_files = _extract_token_file_references(desired, inspection)
        token_file_ok = True
        token_file_msg = "readable; valid JWT"
        for tf in token_files:
            ok, msg = _validate_token_file(tf)
            if not ok:
                token_file_ok = False
                token_file_msg = msg
                break
        rows.append(
            self._check(
                desired,
                "token-file",
                "PASS" if token_file_ok else "FAIL",
                token_file_msg,
            )
        )

        env_token_vars = _extract_env_token_references(desired, inspection)
        if env_token_vars:
            env_status = "PASS"
            env_messages: list[str] = []
            for var_name in env_token_vars:
                status, message = _inspect_env_var(var_name, self.runner)
                env_messages.append(message)
                if status == "FAIL":
                    env_status = "FAIL"
                    break
                if status == "WARN":
                    env_status = "WARN"
            rows.append(
                self._check(
                    desired,
                    "token-env",
                    env_status,
                    "; ".join(env_messages),
                )
            )

        if desired.host in {"codex", "codex-cli"}:
            try:
                bridge_token = Path(desired.token_file).expanduser().read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                bridge_token = ""
            status, connector_digest, source = _env_value_digest(
                desired.token_env_var, self.runner
            )
            bridge_digest = (
                hashlib.sha256(bridge_token.encode()).hexdigest()
                if bridge_token
                else None
            )
            identity_ok = (
                status == "PASS"
                and bridge_digest is not None
                and connector_digest is not None
                and hmac.compare_digest(bridge_digest, connector_digest)
            )
            rows.append(
                self._check(
                    desired,
                    "split-identity",
                    "PASS" if identity_ok else "FAIL",
                    (
                        f"one seat token shared; source={source}"
                        if identity_ok
                        else "split identity: bridge and connector token mismatch; "
                        f"source={source}"
                    ),
                )
            )

        ca_path = Path(desired.ca_file).expanduser()
        ca_ok = ca_path.is_file() and os.access(ca_path, os.R_OK)
        rows.append(
            self._check(
                desired,
                "ca-file",
                "PASS" if ca_ok else "FAIL",
                "readable" if ca_ok else "missing or unreadable",
            )
        )

        bridge_cmd = desired.bridge_command
        has_uvx_from = "uvx" in bridge_cmd and "--from" in bridge_cmd
        effective_ca = os.environ.get("SSL_CERT_FILE") or desired.ca_file
        is_private = _is_private_ca(effective_ca)
        if has_uvx_from and is_private:
            rows.append(
                self._check(
                    desired,
                    "bridge",
                    "FAIL",
                    "uvx --from fails under private CA (use BridgeInstaller / pre-installed bridge)",
                )
            )
        else:
            installer = BridgeInstaller(
                version=_pinned_bridge_version(),
                runner=self.runner,
                pypi_fetcher=self.pypi_fetcher,
            )
            bridge_report = installer.inspect(bridge_cmd)
            rows.append(
                self._check(
                    desired,
                    "bridge",
                    bridge_report["status"],
                    bridge_report["message"],
                )
            )

        nvm_paths = _extract_nvm_npx_paths(inspection)
        if nvm_paths:
            dead_paths = [p for p in nvm_paths if not Path(p).exists()]
            if dead_paths:
                rows.append(
                    self._check(
                        desired,
                        "connector-npx",
                        "WARN",
                        f"dead nvm npx path: {dead_paths[0]}",
                    )
                )
            else:
                rows.append(
                    self._check(
                        desired,
                        "connector-npx",
                        "PASS",
                        f"nvm npx path resolves: {nvm_paths[0]}",
                    )
                )
        if desired.seat_dir:
            shell = Path(desired.seat_dir).expanduser() / "bin/board.sh"
            hints = Path(desired.seat_dir).expanduser() / ".goosehints"
            shell_text = shell.read_text(encoding="utf-8") if shell.is_file() else ""
            match = re.search(
                r"^exec\s+([^ ]+)\s+\"\$SCRIPT_DIR/board.py\"",
                shell_text,
                re.M,
            )
            interpreter = shlex.split(match.group(1))[0] if match else ""
            import_result = (
                self.runner(
                    [interpreter, "-c", "import httpx, mcp, pursers_client"],
                    check=False,
                    text=True,
                    capture_output=True,
                )
                if interpreter
                else None
            )
            ok = bool(import_result and import_result.returncode == 0)
            rows.append(
                self._check(
                    desired,
                    "seat-interpreter",
                    "PASS" if ok else "FAIL",
                    (
                        "required imports available"
                        if ok
                        else "required imports unavailable"
                    ),
                )
            )
            hint_text = hints.read_text(encoding="utf-8") if hints.is_file() else ""
            ok = (
                "sleep" not in hint_text.lower()
                and (
                    "a2a_wait" in hint_text
                    or ("wait --since" in hint_text and "Never poll" in hint_text)
                )
            )
            rows.append(
                self._check(
                    desired,
                    "seat-hints",
                    "PASS" if ok else "FAIL",
                    "push-wait guidance present" if ok else "wait guidance missing",
                )
            )
            if desired.repository:
                seat_new = _load_seat_new()
                clone = Path(desired.seat_dir).expanduser() / seat_new._repo_leaf(
                    desired.repository
                )
                clean = self.runner(
                    ["git", "status", "--porcelain"],
                    cwd=clone,
                    check=False,
                    text=True,
                    capture_output=True,
                )
                behind = self.runner(
                    ["git", "rev-list", "--count", "HEAD..@{upstream}"],
                    cwd=clone,
                    check=False,
                    text=True,
                    capture_output=True,
                )
                ok = (
                    clean.returncode == 0
                    and not clean.stdout.strip()
                    and behind.returncode == 0
                    and int(behind.stdout.strip() or "0") == 0
                )
                rows.append(
                    self._check(
                        desired,
                        "seat-clone",
                        "PASS" if ok else "WARN",
                        "clean and up to date" if ok else "dirty, missing, or behind",
                    )
                )
        try:
            result = self.live_probe(desired, 5.0)
            mode = result.get("mode")
            boards = result.get("registry_boards", [])
            skipped = result.get("skipped_boards", {})
            status = (
                "PASS"
                if mode == "push" and not skipped
                else "WARN"
                if mode in {"push", "poll"}
                else "FAIL"
            )
            rows.append(
                self._check(
                    desired,
                    "live-smoke",
                    status,
                    f"mode={mode}; boards={len(boards)}; skipped={len(skipped)}",
                )
            )
            central_agent = result.get("central_agent")
            if isinstance(central_agent, dict):
                actual = central_agent.get("capabilities")
                actual = actual if isinstance(actual, dict) else {}
                expected = desired.capabilities
                fields_to_check = [
                    "tier_max",
                    "skills",
                    "can_review",
                    "can_work",
                    "host",
                ]
                fields_to_check.extend(
                    field for field in ("model", "provider") if expected.get(field)
                )
                drift = [
                    field
                    for field in fields_to_check
                    if (
                        sorted(actual.get(field, []))
                        if field == "skills"
                        and isinstance(actual.get(field), list)
                        else actual.get(field)
                    )
                    != expected.get(field)
                ]
                rows.append(
                    self._check(
                        desired,
                        "capability-drift",
                        "WARN" if drift else "PASS",
                        (
                            "Central differs: " + ", ".join(drift)
                            if drift
                            else "Central capabilities match"
                        ),
                    )
                )
        except Exception as exc:  # Redacted boundary: never include arguments/tokens.
            rows.append(
                self._check(desired, "live-smoke", "FAIL", type(exc).__name__)
            )
        process_names = {
            "codex": "Codex",
            "codex-cli": "codex",
            "goose": "goose",
            "claude-desktop": "Claude",
            "claude-code": "claude",
            "headless": "pursers",
        }
        process = self.runner(
            ["ps", "-axo", "etimes=,comm="],
            check=False,
            text=True,
            capture_output=True,
        )
        ages = []
        for line in process.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if (
                len(parts) == 2
                and process_names[desired.host].lower() in parts[1].lower()
            ):
                try:
                    ages.append(int(parts[0]))
                except ValueError:
                    pass
        config = Path(desired.config_path).expanduser()
        needs_restart = bool(
            ages
            and config.exists()
            and self.clock() - config.stat().st_mtime < max(ages)
        )
        rows.append(
            self._check(
                desired,
                "restart",
                "WARN" if needs_restart else "PASS",
                (
                    "restart required"
                    if needs_restart
                    else "no stale host process detected"
                ),
            )
        )
        return rows


def _doctor_document(rows: Sequence[DoctorCheck]) -> dict[str, Any]:
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    overall = max((row.status for row in rows), key=order.get, default="PASS")
    return {
        "schema_version": 1,
        "overall": overall,
        "checks": [asdict(row) for row in rows],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="inspect configured seats")
    doctor.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    doctor.add_argument("--fix", action="store_true")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inventory = SeatInventory(args.inventory)
    document = inventory.load()
    outputs: list[dict[str, Any]] = []
    for record in document["seats"]:
        desired = DesiredSeat.from_dict(record)
        adapter = adapter_for(desired)
        if args.fix:
            adapter.apply(adapter.plan(desired))
        report = _doctor_document(Doctor().run(desired))
        installer = BridgeInstaller(runner=subprocess.run)
        bridge_info = installer.inspect(desired.bridge_command)
        actual_bridge_ver = (
            bridge_info.get("installed_version") or _pinned_bridge_version()
        )
        inventory.upsert(desired, bridge_version=actual_bridge_ver, doctor=report)
        outputs.append({"seat": desired.name, **report})
    payload = {"schema_version": 1, "seats": outputs}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for seat in outputs:
            print(f"{seat['overall']:4} {seat['seat']}")
            for row in seat["checks"]:
                print(f"  {row['status']:4} {row['check']}: {row['message']}")
    return 1 if any(seat["overall"] == "FAIL" for seat in outputs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
