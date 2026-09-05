"""Release and Operations manager for Pursers Fleet Dashboard.

Provides:
1. Release card telemetry (versions, origin git tags, CI status, PyPI status, Central versions).
2. Seat restart checklist (identifying bridge processes older than installed shim).
3. Guarded operations (publish from tag, stage central, kickstart central, restart dashboard).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

LOGGER = logging.getLogger("pursers.fleet.release_ops")

DEFAULT_CENTRAL_JOB = "com.onboard.central"
DEFAULT_DASHBOARD_JOB = "com.pursers.fleet-dashboard"
DEFAULT_CENTRAL_URL = "https://127.0.0.1:8766/mcp"
DEFAULT_REPO = "swisspra/Pursers"
CI_WORKFLOW_FILE = "ci.yml"
PUBLISH_WORKFLOW_FILE = "publish-pypi.yml"

DISTRIBUTION_MAP: dict[str, str] = {
    "pursers": "pursers",
    "central": "pursers-central",
    "client": "pursers-client",
    "personal": "pursers-personal",
    "import": "pursers-personal-import",
    "wait_bridge": "pursers-wait-bridge",
}

HOST_PROCESS_NAMES: dict[str, str] = {
    "codex": "Codex",
    "codex-cli": "codex",
    "goose": "goose",
    "claude-desktop": "Claude",
    "claude-code": "claude",
    "headless": "pursers",
}


def _clean_text(value: str) -> str:
    """Redact JWTs, tokens, and authorization secrets."""
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "[REDACTED JWT]",
        value,
    )
    sensitive = re.compile(
        r"(?im)^(\s*[^:=\n]*(?:token|authorization|secret|password|api[_-]?key|bearer)[^:=\n]*)(\s*[:=]\s*)(.*)$"
    )

    def redact(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if "file" in key or "path" in key or "env_var" in key:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    return sensitive.sub(redact, value)


def is_loopback_url(url: str) -> bool:
    """Check if URL targets loopback host."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
        if hostname == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def parse_elapsed_seconds(etime_str: str) -> int:
    """Parse ps etime string ([[dd-]hh:]mm:ss or seconds) into integer seconds."""
    etime_str = etime_str.strip()
    if not etime_str:
        return 0
    if etime_str.isdigit():
        return int(etime_str)
    days = 0
    if "-" in etime_str:
        parts = etime_str.split("-", 1)
        try:
            days = int(parts[0])
            etime_str = parts[1]
        except ValueError:
            return 0
    time_parts = etime_str.split(":")
    try:
        if len(time_parts) == 3:
            hours, minutes, seconds = (int(x) for x in time_parts)
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
        if len(time_parts) == 2:
            minutes, seconds = (int(x) for x in time_parts)
            return days * 86400 + minutes * 60 + seconds
        if len(time_parts) == 1:
            return days * 86400 + int(time_parts[0])
    except ValueError:
        return 0
    return 0


def default_http_get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    """HTTP GET with strict standard verification for external hosts and scoped loopback self-signed support."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "pursers-release-ops", "Accept": "application/json"},
    )
    if is_loopback_url(url):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = ssl.create_default_context()

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise OSError(f"HTTP request failed: {exc}") from exc


class ReleaseOpsManager:
    """Secret-safe release monitoring and operations executor."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        manifest_path: str | Path | None = None,
        component_lock_path: str | Path | None = None,
        staging_root: str | Path | None = None,
        profile_env_path: str | Path | None = None,
        central_url: str | None = None,
        central_job_label: str = DEFAULT_CENTRAL_JOB,
        dashboard_job_label: str = DEFAULT_DASHBOARD_JOB,
        central_venv_python: str | Path | None = None,
        state_dir: str | Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
        clock: Callable[[], float] | None = None,
        bridge_installer: Any = None,
        inventory: Any = None,
        repo: str = DEFAULT_REPO,
    ) -> None:
        self.root = Path(root).resolve() if root else Path(__file__).resolve().parents[2]
        self.manifest_path = (
            Path(manifest_path).resolve()
            if manifest_path
            else self.root / "tools" / "release_versions.toml"
        )
        self.component_lock_path = (
            Path(component_lock_path).resolve()
            if component_lock_path
            else self.root
            / "packages"
            / "personal"
            / "src"
            / "pursers_personal"
            / "resources"
            / "component-lock.json"
        )
        self.staging_root = (
            Path(staging_root).resolve()
            if staging_root
            else self.root / "packages" / "central" / "dist"
        )
        self.profile_env_path = (
            Path(profile_env_path).resolve()
            if profile_env_path
            else self._discover_profile_env()
        )
        self.central_url = central_url or DEFAULT_CENTRAL_URL
        self.central_job_label = central_job_label
        self.dashboard_job_label = dashboard_job_label
        self.central_venv_python = (
            Path(central_venv_python).resolve()
            if central_venv_python
            else self._discover_central_python()
        )
        self.state_dir = (
            Path(state_dir).resolve()
            if state_dir
            else Path.home() / ".pursers" / "fleet-dashboard"
        )
        self.runner = runner
        self.http_get = http_get or default_http_get
        self.clock = clock or time.time
        self.bridge_installer = bridge_installer
        self.inventory = inventory
        self.repo = repo

    def _discover_profile_env(self) -> Path | None:
        env_val = os.environ.get("PURSERS_CENTRAL_PROFILE_ENV")
        if env_val:
            candidate = Path(env_val).expanduser()
            if candidate.is_file():
                return candidate
        candidates = [
            Path.home() / "Desktop/Claude/Claude-tech-default/onboard-cutover/.private-arm/profile.env",
            Path.home() / ".pursers/central/profile.env",
            self.root / "profile.env",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _discover_central_python(self) -> Path | None:
        env_val = os.environ.get("PURSERS_CENTRAL_PYTHON")
        if env_val:
            candidate = Path(env_val).expanduser()
            if candidate.is_file():
                return candidate
        candidates = [
            Path.home() / "Desktop/Claude/Claude-tech-default/onboard-cutover/.private-arm/venv/bin/python",
            Path.home() / ".pursers/central/.venv/bin/python",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _journal(self, action: str, **fields: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "config-actions.jsonl"
        cleaned_fields = {
            key: _clean_text(str(val)) if isinstance(val, str) else val
            for key, val in fields.items()
        }
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": f"ops:{action}",
            **cleaned_fields,
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception as exc:
            LOGGER.warning("failed to write journal: %s", exc)

    def load_manifest_versions(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {}
        try:
            return tomllib.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("failed to load release_versions.toml: %s", exc)
            return {}

    def get_trusted_central_sha(self, central_version: str) -> str | None:
        """Obtain trusted wheel SHA-256 digest from release/build metadata (component-lock.json)."""
        if self.component_lock_path.is_file():
            try:
                data = json.loads(self.component_lock_path.read_text(encoding="utf-8"))
                comp = data.get("components", {}).get("pursers-central", {})
                if comp.get("version") == central_version:
                    sha = comp.get("wheel_sha256")
                    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
                        return sha
            except Exception as exc:
                LOGGER.debug("failed reading component-lock.json: %s", exc)
        return None

    def resolve_staging_central_wheel(self, central_version: str) -> Path:
        """Resolve ONLY the exact manifest Central version from an explicitly trusted/configured staging root."""
        expected_wheel_name = f"pursers_central-{central_version}-py3-none-any.whl"
        trusted_root = self.staging_root.resolve()
        target = (trusted_root / expected_wheel_name).resolve()
        if target.parent == trusted_root and target.is_file():
            return target
        raise RuntimeError(
            f"Could not find exact wheel '{expected_wheel_name}' in trusted staging root {trusted_root}"
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_replace_bytes(
        cls,
        path: Path,
        content: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        """Durably replace a file while preserving its security metadata."""
        temporary = path.with_name(f".{path.name}.tmp.{time.time_ns()}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, gid)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def _resolve_stage_plan(self) -> dict[str, Any]:
        """Resolve every concrete Stage input before confirmation or mutation."""
        profile = self.profile_env_path
        python = self.central_venv_python
        if not profile or not profile.is_file():
            raise RuntimeError(f"Configured profile.env not found at {profile}")
        if not python or not python.is_file():
            raise RuntimeError(f"Configured Central venv python not found at {python}")

        manifest = self.load_manifest_versions()
        central_version = manifest.get("packages", {}).get("central")
        if not isinstance(central_version, str) or not central_version:
            raise RuntimeError("release_versions.toml missing [packages].central version")
        source_wheel = self.resolve_staging_central_wheel(central_version)
        expected_sha = self.get_trusted_central_sha(central_version)
        if not expected_sha:
            raise RuntimeError(
                "Trusted build metadata (component-lock.json) missing valid "
                f"wheel_sha256 for pursers-central {central_version}"
            )
        computed_sha = hashlib.sha256(source_wheel.read_bytes()).hexdigest()
        if computed_sha != expected_sha:
            raise ValueError(
                f"Preflight digest mismatch for {source_wheel.name}: "
                f"candidate={computed_sha} != trusted={expected_sha}"
            )

        destination = profile.parent / "wheels" / source_wheel.name
        profile_stat = profile.stat()
        return {
            "central_version": central_version,
            "source_wheel": source_wheel,
            "destination_wheel": destination,
            "expected_sha": expected_sha,
            "profile": profile,
            "profile_mode": stat.S_IMODE(profile_stat.st_mode),
            "profile_uid": profile_stat.st_uid,
            "profile_gid": profile_stat.st_gid,
            "python": python,
        }

    @staticmethod
    def _format_stage_plan(plan: dict[str, Any]) -> str:
        return (
            f"copy {shlex.quote(str(plan['source_wheel']))} -> "
            f"{shlex.quote(str(plan['destination_wheel']))}; "
            f"verify sha256={plan['expected_sha']}; atomically update "
            f"{shlex.quote(str(plan['profile']))} preserving "
            f"mode={plan['profile_mode']:04o} uid={plan['profile_uid']} gid={plan['profile_gid']}; "
            f"{shlex.quote(str(plan['python']))} -m pip install --no-deps "
            f"{shlex.quote(str(plan['destination_wheel']))}"
        )

    def get_origin_tags(self) -> list[str]:
        """Fetch real tag list from origin."""
        try:
            proc = self.runner(
                ["git", "-C", str(self.root), "ls-remote", "--tags", "origin"],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return []
            tags: list[str] = []
            for line in proc.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1].startswith("refs/tags/"):
                    tag_ref = parts[1][len("refs/tags/"):]
                    if not tag_ref.endswith("^{}"):
                        tags.append(tag_ref)
            return tags
        except Exception as exc:
            LOGGER.debug("ls-remote failed: %s", exc)
            return []

    def get_origin_tag_commit(self, tag: str) -> str | None:
        """Resolve the tag commit directly from origin."""
        try:
            proc = self.runner(
                ["git", "-C", str(self.root), "ls-remote", "--tags", "origin", f"refs/tags/{tag}*", f"refs/tags/{tag}"],
                check=False,
                text=True,
                capture_output=True,
                timeout=4,
            )
            if proc.returncode == 0:
                exact_peeled = f"refs/tags/{tag}^{{}}"
                exact_ref = f"refs/tags/{tag}"
                peeled_sha = None
                direct_sha = None
                for line in proc.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        if parts[1] == exact_peeled:
                            peeled_sha = parts[0]
                        elif parts[1] == exact_ref:
                            direct_sha = parts[0]
                return peeled_sha or direct_sha
        except Exception as exc:
            LOGGER.debug("ls-remote tag commit failed: %s", exc)
        return None

    def get_latest_tag(self) -> str | None:
        """Resolve the latest release tag from origin (not local tag state)."""
        tags = self.get_origin_tags()
        if not tags:
            return None

        def tag_sort_key(tag_name: str) -> tuple[int, ...]:
            clean = tag_name.lstrip("v")
            match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:a(\d+))?", clean)
            if match:
                major, minor, patch, alpha = match.groups()
                return (int(major), int(minor), int(patch), int(alpha or 999999))
            return (0, 0, 0, 0)

        sorted_tags = sorted(tags, key=tag_sort_key, reverse=True)
        return sorted_tags[0]

    def validate_publish_tag(self, tag: str) -> bool:
        """Validate that publish tag exists on origin. Origin lookup failure is fatal."""
        origin_tags = self.get_origin_tags()
        if not origin_tags:
            return False
        return tag in origin_tags

    def get_ci_status(self, latest_tag: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {"main": None, "tag": None}
        try:
            proc_main = self.runner(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    self.repo,
                    "--workflow",
                    CI_WORKFLOW_FILE,
                    "--branch",
                    "main",
                    "--limit",
                    "1",
                    "--json",
                    "status,conclusion,url,headSha,createdAt",
                ],
                check=False,
                text=True,
                capture_output=True,
                timeout=4,
            )
            if proc_main.returncode == 0:
                data = json.loads(proc_main.stdout)
                if data and isinstance(data, list):
                    result["main"] = data[0]
        except Exception as exc:
            result["main"] = {"status": "unavailable", "error": type(exc).__name__}

        if latest_tag:
            try:
                commit_sha = self.get_origin_tag_commit(latest_tag)
                if commit_sha:
                    proc_tag = self.runner(
                        [
                            "gh",
                            "run",
                            "list",
                            "--repo",
                            self.repo,
                            "--workflow",
                            CI_WORKFLOW_FILE,
                            "--commit",
                            commit_sha,
                            "--limit",
                            "1",
                            "--json",
                            "status,conclusion,url,headSha,createdAt",
                        ],
                        check=False,
                        text=True,
                        capture_output=True,
                        timeout=4,
                    )
                    if proc_tag.returncode == 0:
                        data = json.loads(proc_tag.stdout)
                        if data and isinstance(data, list):
                            result["tag"] = data[0]
            except Exception as exc:
                result["tag"] = {"status": "unavailable", "error": type(exc).__name__}
        return result

    def get_pypi_status(self, packages: dict[str, str]) -> dict[str, Any]:
        results: dict[str, Any] = {}

        def check_package(pkg_key: str, version: str) -> tuple[str, dict[str, Any]]:
            dist_name = DISTRIBUTION_MAP.get(pkg_key, pkg_key)
            url = f"https://pypi.org/pypi/{dist_name}/{version}/json"
            try:
                status, _ = self.http_get(url, 2.5)
                present = (status == 200)
                return pkg_key, {
                    "distribution": dist_name,
                    "version": version,
                    "present": present,
                    "status_code": status,
                }
            except Exception as exc:
                return pkg_key, {
                    "distribution": dist_name,
                    "version": version,
                    "present": None,
                    "error": type(exc).__name__,
                }

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [
                executor.submit(check_package, k, v)
                for k, v in packages.items()
                if isinstance(v, str)
            ]
            for future in concurrent.futures.as_completed(futures):
                key, val = future.result()
                results[key] = val
        return results

    def get_github_release(self, tag: str | None) -> dict[str, Any]:
        if not tag:
            return {"present": False, "tag": None}
        try:
            proc = self.runner(
                [
                    "gh",
                    "release",
                    "view",
                    tag,
                    "--repo",
                    self.repo,
                    "--json",
                    "tagName,name,url,isDraft,isPrerelease,publishedAt",
                ],
                check=False,
                text=True,
                capture_output=True,
                timeout=4,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                return {"present": True, "tag": tag, **data}
            return {
                "present": False,
                "tag": tag,
                "error": _clean_text(proc.stderr.strip() or "Not found"),
            }
        except Exception as exc:
            return {"present": False, "tag": tag, "error": type(exc).__name__}

    def get_central_version_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "live_version": None,
            "live_wheel_sha256": None,
            "staged_version": None,
            "staged_wheel_sha256": None,
            "status": "unknown",
        }
        healthz_url = self.central_url.rstrip("/")
        if healthz_url.endswith("/mcp"):
            healthz_url = healthz_url[:-4] + "/healthz"
        else:
            healthz_url += "/healthz"
        try:
            status, body = self.http_get(healthz_url, 3.0)
            if status == 200:
                data = json.loads(body.decode("utf-8"))
                info["live_version"] = data.get("version")
                info["live_wheel_sha256"] = data.get("build", {}).get("wheel_sha256")
            else:
                info["live_error"] = f"HTTP {status}"
        except Exception as exc:
            info["live_error"] = type(exc).__name__

        if self.profile_env_path and self.profile_env_path.is_file():
            try:
                content = self.profile_env_path.read_text(encoding="utf-8")
                match_wheel = re.search(r"CENTRAL_WHEEL=(.+)", content)
                match_sha = re.search(r"CENTRAL_WHEEL_SHA256=(.+)", content)
                if match_wheel:
                    wheel_val = match_wheel.group(1).strip().strip("'\"")
                    info["staged_wheel"] = wheel_val
                    match_ver = re.search(r"pursers_central-([0-9a-zA-Z._-]+)-py3", wheel_val)
                    if match_ver:
                        info["staged_version"] = match_ver.group(1)
                if match_sha:
                    info["staged_wheel_sha256"] = match_sha.group(1).strip().strip("'\"")
            except Exception as exc:
                info["staged_error"] = type(exc).__name__

        live = info.get("live_version")
        staged = info.get("staged_version")
        if live is None and "live_error" in info:
            info["status"] = "unreachable"
        elif live and staged:
            info["status"] = "matches" if live == staged else "drift"
        elif live and not staged:
            info["status"] = "live_only"
        elif staged and not live:
            info["status"] = "staged_only"
        return info

    def get_restart_checklist(self) -> list[dict[str, Any]]:
        installed_version = None
        shim_mtime = None
        if self.bridge_installer:
            try:
                inspection = self.bridge_installer.inspect()
                installed_version = inspection.get("installed_version")
                resolved, _, _ = self.bridge_installer._resolve()
                if resolved and Path(resolved).exists():
                    shim_mtime = Path(resolved).stat().st_mtime
            except Exception as exc:
                LOGGER.debug("bridge installer resolution failed: %s", exc)

        now = self.clock()
        shim_age = (now - shim_mtime) if shim_mtime else None

        proc_output = ""
        try:
            proc = self.runner(
                ["ps", "-axo", "etime,pid,args"],
                check=False,
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                proc_output = proc.stdout
        except Exception as exc:
            LOGGER.debug("ps execution failed: %s", exc)

        running_bridges: list[dict[str, Any]] = []
        host_processes: dict[str, list[dict[str, Any]]] = {}

        for line in proc_output.splitlines():
            line = line.strip()
            if not line or line.startswith("ELAPSED"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            etime_str, pid_str, args_str = parts[0], parts[1], parts[2]
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            elapsed_s = parse_elapsed_seconds(etime_str)

            if "pursers-wait-bridge" in args_str or "pursers_wait_server" in args_str:
                running_bridges.append({
                    "pid": pid,
                    "elapsed_s": elapsed_s,
                    "command": args_str,
                })

            args_lower = args_str.lower()
            for host, proc_name in HOST_PROCESS_NAMES.items():
                if proc_name.lower() in args_lower:
                    host_processes.setdefault(host, []).append({
                        "pid": pid,
                        "elapsed_s": elapsed_s,
                        "command": args_str,
                    })

        configured_seats = []
        if self.inventory:
            try:
                configured_seats = self.inventory.load().get("seats", [])
            except Exception:
                pass

        known_hosts = set(HOST_PROCESS_NAMES.keys())
        if configured_seats:
            known_hosts = {s.get("host") for s in configured_seats if s.get("host")}

        checklist: list[dict[str, Any]] = []
        for host in sorted(known_hosts):
            hosts_bridges = running_bridges
            host_procs = host_processes.get(host, [])
            needs_restart = False
            reasons = []
            stale_pids = []

            if shim_age is not None:
                for bp in hosts_bridges:
                    if bp["elapsed_s"] > shim_age:
                        needs_restart = True
                        stale_pids.append(bp["pid"])
                        reasons.append(
                            f"Bridge PID {bp['pid']} started before shim update"
                        )

            if shim_age is not None and host_procs:
                for hp in host_procs:
                    if hp["elapsed_s"] > shim_age and not needs_restart:
                        needs_restart = True
                        stale_pids.append(hp["pid"])
                        reasons.append(
                            f"Host {host} PID {hp['pid']} started before shim update"
                        )

            seat_records = [s for s in configured_seats if s.get("host") == host]
            for seat_rec in seat_records:
                doc = seat_rec.get("last_doctor")
                if isinstance(doc, dict):
                    restart_check = next(
                        (c for c in doc.get("checks", []) if c.get("check") == "restart"),
                        None,
                    )
                    if restart_check and restart_check.get("status") == "WARN":
                        needs_restart = True
                        reasons.append(f"Doctor flagged restart required for seat {seat_rec.get('name')}")

            checklist.append({
                "host": host,
                "needs_restart": needs_restart,
                "installed_bridge_version": installed_version,
                "running_pids": stale_pids or [p["pid"] for p in host_procs],
                "reason": "; ".join(reasons) if reasons else "Running current bridge shim",
            })

        return checklist

    def get_preview_commands(self, tag: str | None = None) -> dict[str, str | None]:
        """Generate previews only when every action input resolves concretely."""
        latest_tag = tag or self.get_latest_tag()
        uid = os.getuid() if hasattr(os, "getuid") else 501
        try:
            stage_preview: str | None = self._format_stage_plan(
                self._resolve_stage_plan()
            )
        except Exception:
            stage_preview = None

        return {
            "publish_from_tag": (
                f"gh workflow run {PUBLISH_WORKFLOW_FILE} --ref {shlex.quote(latest_tag)}"
                if latest_tag
                else None
            ),
            "stage_central": stage_preview,
            "kickstart_central": f"launchctl kickstart -k gui/{uid}/{self.central_job_label}",
            "restart_dashboard": f"launchctl kickstart -k gui/{uid}/{self.dashboard_job_label}",
        }

    def release_card_status(self) -> dict[str, Any]:
        manifest = self.load_manifest_versions()
        packages = manifest.get("packages", {})
        latest_tag = self.get_latest_tag()

        return {
            "schema_version": 1,
            "versions": manifest,
            "latest_tag": latest_tag,
            "ci_status": self.get_ci_status(latest_tag),
            "pypi": self.get_pypi_status(packages),
            "github_release": self.get_github_release(latest_tag),
            "central_version": self.get_central_version_info(),
            "restart_checklist": self.get_restart_checklist(),
            "commands": self.get_preview_commands(latest_tag),
        }

    # ================= Operations with Explicit Confirmation =================

    def publish_from_tag(
        self,
        tag: str | None = None,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        def emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        # Fatal origin tag lookup
        origin_tags = self.get_origin_tags()
        if not origin_tags:
            emit("Publish failed: Could not retrieve tags from origin.")
            raise RuntimeError("Origin tags could not be retrieved from origin; publish refused.")

        target_tag = tag or self.get_latest_tag()
        if not target_tag:
            emit("Publish failed: No tag available to publish.")
            raise ValueError("No tag selected or available to publish")

        if target_tag not in origin_tags:
            emit(f"Publish failed: Tag '{target_tag}' does not exist on origin.")
            raise ValueError(f"Tag '{target_tag}' does not exist on origin; refused to publish.")

        cmd = ["gh", "workflow", "run", PUBLISH_WORKFLOW_FILE, "--ref", target_tag]
        cmd_str = f"gh workflow run {PUBLISH_WORKFLOW_FILE} --ref {shlex.quote(target_tag)}"
        emit(f"Running command: {cmd_str}")

        proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=30)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = _clean_text((stdout + "\n" + stderr).strip())
        if proc.returncode != 0:
            emit(f"Publish workflow failed (code {proc.returncode}):\n{output}")
            self._journal("publish_from_tag", tag=target_tag, ok=False, code=proc.returncode)
            raise RuntimeError(f"Workflow trigger failed (exit {proc.returncode}):\n{output}")

        emit(f"Publish workflow successfully triggered for tag {target_tag}")
        self._journal("publish_from_tag", tag=target_tag, ok=True, code=proc.returncode)
        LOGGER.info("ops publish_from_tag: tag=%s ok=True", target_tag)
        return {
            "ok": True,
            "action": "publish_from_tag",
            "command": cmd_str,
            "output": output,
        }

    def stage_central(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Full fail-closed Stage Central transaction: copy wheel, verify SHA-256 against trusted build metadata, atomically update profile.env pins with 0600 preservation, pip install --no-deps, rollback on failure."""
        def emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        plan = self._resolve_stage_plan()
        profile = plan["profile"]
        python = plan["python"]
        central_version = plan["central_version"]
        expected_sha = plan["expected_sha"]
        source_wheel = plan["source_wheel"]
        dest_wheel = plan["destination_wheel"]

        emit(f"Step 1: Manifest central version: {central_version}")
        emit(f"Step 1: Trusted expected SHA-256 from build metadata: {expected_sha}")
        emit(f"Step 2: Resolved trusted source wheel: {source_wheel}")
        emit("Step 2: Preflight SHA-256 verified successfully against trusted build metadata.")

        # Prepare for transaction and rollback
        profile_backup = profile.read_bytes()
        dest_wheel_backup: bytes | None = dest_wheel.read_bytes() if dest_wheel.is_file() else None
        destination_stat = dest_wheel.stat() if dest_wheel.is_file() else None
        original_mode = plan["profile_mode"]
        original_uid = plan["profile_uid"]
        original_gid = plan["profile_gid"]
        destination_mode = (
            stat.S_IMODE(destination_stat.st_mode) if destination_stat else 0o600
        )
        destination_uid = destination_stat.st_uid if destination_stat else original_uid
        destination_gid = destination_stat.st_gid if destination_stat else original_gid
        dest_wheel.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Copy and verify to a temporary file, then durably install it.
            emit(f"Step 3: Copying wheel to staging location {dest_wheel}")
            dest_bytes = source_wheel.read_bytes()
            dest_sha = hashlib.sha256(dest_bytes).hexdigest()
            if dest_sha != expected_sha:
                raise RuntimeError(
                    f"Destination wheel hash mismatch after copy: {dest_sha} != {expected_sha}"
                )
            self._atomic_replace_bytes(
                dest_wheel,
                dest_bytes,
                mode=destination_mode,
                uid=destination_uid,
                gid=destination_gid,
            )

            # Atomically update profile.env pins while preserving mode and ownership.
            emit("Step 4: Atomically updating profile.env pins with permission preservation")
            new_lines = []
            wheel_set = False
            sha_set = False
            for line in profile_backup.decode("utf-8").splitlines():
                if line.startswith("CENTRAL_WHEEL="):
                    new_lines.append(f"CENTRAL_WHEEL={shlex.quote(str(dest_wheel))}")
                    wheel_set = True
                elif line.startswith("CENTRAL_WHEEL_SHA256="):
                    new_lines.append(f"CENTRAL_WHEEL_SHA256={shlex.quote(dest_sha)}")
                    sha_set = True
                else:
                    new_lines.append(line)
            if not wheel_set:
                new_lines.append(f"CENTRAL_WHEEL={shlex.quote(str(dest_wheel))}")
            if not sha_set:
                new_lines.append(f"CENTRAL_WHEEL_SHA256={shlex.quote(dest_sha)}")

            self._atomic_replace_bytes(
                profile,
                ("\n".join(new_lines) + "\n").encode("utf-8"),
                mode=original_mode,
                uid=original_uid,
                gid=original_gid,
            )

            # Install copied wheel into Central venv with --no-deps
            emit(f"Step 5: Installing {dest_wheel.name} into Central venv with --no-deps")
            install_cmd = [str(python), "-m", "pip", "install", "--no-deps", str(dest_wheel)]
            cmd_str = f"{shlex.quote(str(python))} -m pip install --no-deps {shlex.quote(str(dest_wheel))}"
            proc = self.runner(install_cmd, check=False, text=True, capture_output=True, timeout=60)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            output = (stdout + "\n" + stderr).strip()
            if proc.returncode != 0:
                raise RuntimeError(f"Pip install failed with code {proc.returncode}:\n{output}")

            emit("Step 5: Successfully installed Central wheel.")
            self._journal("stage_central", wheel=str(dest_wheel), sha256=dest_sha, ok=True)
            LOGGER.info("ops stage_central succeeded: %s", dest_wheel)
            return {
                "ok": True,
                "action": "stage_central",
                "command": cmd_str,
                "wheel": str(dest_wheel),
                "sha256": dest_sha,
                "output": output,
            }

        except Exception as exc:
            emit(f"ERROR during Stage Central: {exc}")
            emit("Rolling back profile.env and staged wheel...")
            # Roll back profile.env
            try:
                self._atomic_replace_bytes(
                    profile,
                    profile_backup,
                    mode=original_mode,
                    uid=original_uid,
                    gid=original_gid,
                )
            except Exception as rb_exc:
                LOGGER.error("Failed rolling back profile.env: %s", rb_exc)

            # Roll back destination wheel
            try:
                if dest_wheel_backup is None:
                    if dest_wheel.is_file():
                        dest_wheel.unlink()
                        self._fsync_directory(dest_wheel.parent)
                else:
                    self._atomic_replace_bytes(
                        dest_wheel,
                        dest_wheel_backup,
                        mode=destination_mode,
                        uid=destination_uid,
                        gid=destination_gid,
                    )
            except Exception as rb_exc:
                LOGGER.error("Failed restoring dest wheel: %s", rb_exc)

            self._journal("stage_central", ok=False, error=str(exc))
            raise

    def kickstart_central(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        def emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        uid = os.getuid() if hasattr(os, "getuid") else 501
        target = f"gui/{uid}/{self.central_job_label}"
        cmd = ["launchctl", "kickstart", "-k", target]
        cmd_str = f"launchctl kickstart -k {target}"
        emit(f"Running command: {cmd_str}")

        proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=10)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = _clean_text((stdout + "\n" + stderr).strip())
        if proc.returncode != 0:
            emit(f"Kickstart failed with exit code {proc.returncode}:\n{output}")
            self._journal("kickstart_central", target=target, ok=False, code=proc.returncode)
            raise RuntimeError(f"Kickstart Central failed (exit {proc.returncode}):\n{output}")

        emit(f"Central job {self.central_job_label} successfully kickstarted.")
        self._journal("kickstart_central", target=target, ok=True, code=proc.returncode)
        LOGGER.info("ops kickstart_central: target=%s ok=True", target)
        return {
            "ok": True,
            "action": "kickstart_central",
            "command": cmd_str,
            "output": output,
        }

    def restart_dashboard(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        def emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        uid = os.getuid() if hasattr(os, "getuid") else 501
        target = f"gui/{uid}/{self.dashboard_job_label}"
        cmd = ["launchctl", "kickstart", "-k", target]
        cmd_str = f"launchctl kickstart -k {target}"
        emit(f"Running command: {cmd_str}")

        proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=10)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = _clean_text((stdout + "\n" + stderr).strip())
        if proc.returncode != 0:
            emit(f"Restart dashboard failed with exit code {proc.returncode}:\n{output}")
            self._journal("restart_dashboard", target=target, ok=False, code=proc.returncode)
            raise RuntimeError(f"Restart dashboard failed (exit {proc.returncode}):\n{output}")

        emit(f"Dashboard job {self.dashboard_job_label} successfully kickstarted.")
        self._journal("restart_dashboard", target=target, ok=True, code=proc.returncode)
        LOGGER.info("ops restart_dashboard: target=%s ok=True", target)
        return {
            "ok": True,
            "action": "restart_dashboard",
            "command": cmd_str,
            "output": output,
        }
