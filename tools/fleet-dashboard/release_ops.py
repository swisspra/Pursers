"""Release and Operations manager for Pursers Fleet Dashboard.

Provides:
1. Release card telemetry (versions, git tags, CI status, PyPI status, Central versions).
2. Seat restart checklist (identifying bridge processes older than installed shim).
3. Guarded operations (publish from tag, stage central, kickstart central, restart dashboard).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shlex
import shutil
import ssl
import subprocess
import time
import urllib.error
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
    """Simple HTTP GET with timeout, ignoring SSL verification for loopback self-signed certs."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "pursers-release-ops", "Accept": "application/json"},
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
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

    def get_latest_tag(self) -> str | None:
        # Try git tags sorted by version
        try:
            proc = self.runner(
                ["git", "-C", str(self.root), "tag", "-l", "v*", "--sort=-v:refname"],
                check=False,
                text=True,
                capture_output=True,
                timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                if lines:
                    return lines[0]
        except Exception:
            pass
        # Fallback to manifest product version with 'v'
        versions = self.load_manifest_versions()
        prod = versions.get("product")
        if prod:
            return f"v{prod}"
        return None

    def get_ci_status(self, latest_tag: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {"main": None, "tag": None}
        # Check main branch CI
        try:
            proc_main = self.runner(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    self.repo,
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

        # Check tag CI
        if latest_tag:
            try:
                # Find commit sha for tag
                rev_proc = self.runner(
                    ["git", "-C", str(self.root), "rev-list", "-n", "1", latest_tag],
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=2,
                )
                commit_sha = rev_proc.stdout.strip() if rev_proc.returncode == 0 else ""
                if commit_sha:
                    proc_tag = self.runner(
                        [
                            "gh",
                            "run",
                            "list",
                            "--repo",
                            self.repo,
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
        # Probe live Central /healthz
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

        # Probe staged profile.env
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
        """Identify which hosts run bridge processes older than the installed shim version."""
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

        # Fetch process table
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

        # Parse running bridge processes
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

            # Check if this is a bridge process
            if "pursers-wait-bridge" in args_str or "pursers_wait_server" in args_str:
                running_bridges.append({
                    "pid": pid,
                    "elapsed_s": elapsed_s,
                    "command": args_str,
                })

            # Check host processes
            args_lower = args_str.lower()
            for host, proc_name in HOST_PROCESS_NAMES.items():
                if proc_name.lower() in args_lower:
                    host_processes.setdefault(host, []).append({
                        "pid": pid,
                        "elapsed_s": elapsed_s,
                        "command": args_str,
                    })

        # Check configured seats from inventory
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
            hosts_bridges = running_bridges  # Bridge processes are shared or spawned per seat
            host_procs = host_processes.get(host, [])
            needs_restart = False
            reasons = []
            stale_pids = []

            # Check if any bridge process started before shim was updated
            if shim_age is not None:
                for bp in hosts_bridges:
                    if bp["elapsed_s"] > shim_age:
                        needs_restart = True
                        stale_pids.append(bp["pid"])
                        reasons.append(
                            f"Bridge PID {bp['pid']} started before shim update"
                        )

            # Check if host process itself started before shim update
            if shim_age is not None and host_procs:
                for hp in host_procs:
                    if hp["elapsed_s"] > shim_age and not needs_restart:
                        needs_restart = True
                        stale_pids.append(hp["pid"])
                        reasons.append(
                            f"Host {host} PID {hp['pid']} started before shim update"
                        )

            # Check seat inventory doctor status
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
        }

    # ================= Operations with Explicit Confirmation =================

    def publish_from_tag(self, tag: str | None = None) -> dict[str, Any]:
        target_tag = tag or self.get_latest_tag()
        if not target_tag:
            return {
                "ok": False,
                "action": "publish_from_tag",
                "command": "gh workflow run publish-pypi.yml --ref <no-tag>",
                "output": "",
                "error": "No tag selected or available",
            }
        cmd = ["gh", "workflow", "run", "publish-pypi.yml", "--ref", target_tag]
        cmd_str = f"gh workflow run publish-pypi.yml --ref {shlex.quote(target_tag)}"
        try:
            proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=30)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            output = _clean_text((stdout + "\n" + stderr).strip())
            ok = proc.returncode == 0
            err = None if ok else f"Process exited with {proc.returncode}"
            self._journal("publish_from_tag", tag=target_tag, ok=ok, code=proc.returncode)
            LOGGER.info("ops publish_from_tag: tag=%s ok=%s", target_tag, ok)
            return {
                "ok": ok,
                "action": "publish_from_tag",
                "command": cmd_str,
                "output": output,
                "error": err,
            }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._journal("publish_from_tag", tag=target_tag, ok=False, error=err)
            LOGGER.error("ops publish_from_tag failed: %s", exc)
            return {
                "ok": False,
                "action": "publish_from_tag",
                "command": cmd_str,
                "output": "",
                "error": err,
            }

    def stage_central(
        self,
        *,
        profile_path: str | Path | None = None,
        venv_python: str | Path | None = None,
    ) -> dict[str, Any]:
        profile = Path(profile_path) if profile_path else self.profile_env_path
        python = Path(venv_python) if venv_python else self.central_venv_python

        if not profile or not profile.is_file():
            return {
                "ok": False,
                "action": "stage_central",
                "command": "stage_central (profile.env missing)",
                "output": "",
                "error": f"profile.env not found at {profile}",
            }
        if not python or not python.is_file():
            return {
                "ok": False,
                "action": "stage_central",
                "command": "stage_central (central python missing)",
                "output": "",
                "error": f"Central venv python not found at {python}",
            }

        cmd_str = (
            f'set -a; . {shlex.quote(str(profile))}; set +a; '
            'test "$(shasum -a 256 "$CENTRAL_WHEEL" | cut -d\' \' -f1)" = "$CENTRAL_WHEEL_SHA256" && '
            f'{shlex.quote(str(python))} -m pip install --no-deps "$CENTRAL_WHEEL"'
        )
        try:
            proc = self.runner(
                ["bash", "-c", cmd_str],
                check=False,
                text=True,
                capture_output=True,
                timeout=60,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            output = _clean_text((stdout + "\n" + stderr).strip())
            ok = proc.returncode == 0
            err = None if ok else f"Stage Central failed with exit code {proc.returncode}"
            self._journal("stage_central", ok=ok, code=proc.returncode)
            LOGGER.info("ops stage_central: ok=%s code=%s", ok, proc.returncode)
            return {
                "ok": ok,
                "action": "stage_central",
                "command": cmd_str,
                "output": output,
                "error": err,
            }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._journal("stage_central", ok=False, error=err)
            LOGGER.error("ops stage_central failed: %s", exc)
            return {
                "ok": False,
                "action": "stage_central",
                "command": cmd_str,
                "output": "",
                "error": err,
            }

    def kickstart_central(self, job_label: str | None = None) -> dict[str, Any]:
        label = job_label or self.central_job_label
        uid = os.getuid() if hasattr(os, "getuid") else 501
        target = f"gui/{uid}/{label}"
        cmd = ["launchctl", "kickstart", "-k", target]
        cmd_str = f"launchctl kickstart -k {target}"
        try:
            proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=10)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            output = _clean_text((stdout + "\n" + stderr).strip())
            ok = proc.returncode == 0
            err = None if ok else f"Kickstart Central failed with exit code {proc.returncode}"
            self._journal("kickstart_central", target=target, ok=ok, code=proc.returncode)
            LOGGER.info("ops kickstart_central: target=%s ok=%s", target, ok)
            return {
                "ok": ok,
                "action": "kickstart_central",
                "command": cmd_str,
                "output": output,
                "error": err,
            }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._journal("kickstart_central", target=target, ok=False, error=err)
            LOGGER.error("ops kickstart_central failed: %s", exc)
            return {
                "ok": False,
                "action": "kickstart_central",
                "command": cmd_str,
                "output": "",
                "error": err,
            }

    def restart_dashboard(self, job_label: str | None = None) -> dict[str, Any]:
        label = job_label or self.dashboard_job_label
        uid = os.getuid() if hasattr(os, "getuid") else 501
        target = f"gui/{uid}/{label}"
        cmd = ["launchctl", "kickstart", "-k", target]
        cmd_str = f"launchctl kickstart -k {target}"
        try:
            proc = self.runner(cmd, check=False, text=True, capture_output=True, timeout=10)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            output = _clean_text((stdout + "\n" + stderr).strip())
            ok = proc.returncode == 0
            err = None if ok else f"Restart dashboard failed with exit code {proc.returncode}"
            self._journal("restart_dashboard", target=target, ok=ok, code=proc.returncode)
            LOGGER.info("ops restart_dashboard: target=%s ok=%s", target, ok)
            return {
                "ok": ok,
                "action": "restart_dashboard",
                "command": cmd_str,
                "output": output,
                "error": err,
            }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._journal("restart_dashboard", target=target, ok=False, error=err)
            LOGGER.error("ops restart_dashboard failed: %s", exc)
            return {
                "ok": False,
                "action": "restart_dashboard",
                "command": cmd_str,
                "output": "",
                "error": err,
            }
