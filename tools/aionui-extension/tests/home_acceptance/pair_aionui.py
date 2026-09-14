#!/usr/bin/env python3
"""Pair the verifier ego-browser task space with a running AionUi WebUI.

The pairing code exists only in the authenticated desktop renderer.  This
tool reads it transiently through macOS Accessibility, submits it through the
same ego-browser task space used by the Home observer, and accepts only the
known native AionUi confirmation dialog.  The code is never written or
printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

TOOL_VERSION = "1.0.0"
PAIRING_MODE = "automated_preauthorized"
PROOF_KEYS = {
    "paired",
    "pairing_mode",
    "tool_version",
    "tool_sha256",
    "core_port",
    "timestamp",
    "code_consumed",
}
PAIRING_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{2}$")
PAIRING_CODE_SEARCH = re.compile(
    r"(?<![A-Z0-9])[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{2}(?![A-Z0-9])"
)
FULL_SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXPECTED_DIALOG_TITLE = "WebUI Pairing Request"
EXPECTED_DIALOG_MESSAGE = "A browser is requesting access to the desktop WebUI."
EXPECTED_DIALOG_BUTTON = "Confirm"


class PairingError(RuntimeError):
    """A bounded pairing step could not establish the required proof."""


@dataclass(frozen=True)
class Runtime:
    origin: str
    core_port: int
    data_dir: Path
    observer_dir: Path
    ego_browser: Path
    task_space: str | int


class PairingDriver(Protocol):
    def get_pair_info(self, runtime: Runtime) -> dict[str, Any]: ...

    def read_pairing_code(self, timeout_s: float) -> str: ...

    def begin_browser_pairing(self, runtime: Runtime, code: str, timeout_s: float) -> Any: ...

    def approve_native_dialog(self, timeout_s: float) -> None: ...

    def finish_browser_pairing(self, handle: Any, timeout_s: float) -> dict[str, Any]: ...

    def cancel_browser_pairing(self, handle: Any) -> None: ...


def _json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PairingError(f"{label} is unavailable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise PairingError(f"{label} must be a JSON object")
    return value


def load_runtime(handoff_path: Path) -> Runtime:
    """Resolve port, data directory, and observer task space from one handoff."""
    handoff_path = handoff_path.expanduser().resolve()
    manifest = _json_file(handoff_path, "handoff runtime")
    if manifest.get("schema_version") != 1:
        raise PairingError("handoff runtime schema_version must be 1")
    origin = manifest.get("origin")
    parsed = urlsplit(origin if isinstance(origin, str) else "")
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PairingError("handoff origin must be an explicit loopback HTTP origin")

    data_dir = (handoff_path.parent / "aioncore-data").resolve()
    if not data_dir.is_dir() or data_dir.parent != handoff_path.parent:
        raise PairingError("handoff aioncore-data directory is unavailable")

    observer = manifest.get("approved_observer")
    install_dir = observer.get("install_dir") if isinstance(observer, dict) else None
    if not isinstance(install_dir, str):
        raise PairingError("handoff lacks the approved observer install_dir")
    observer_dir = Path(install_dir).expanduser().resolve()
    observer_config = _json_file(
        observer_dir / "observer.json",
        "approved observer configuration",
    )
    backend = observer_config.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") != "ego-browser":
        raise PairingError("approved observer must use the ego-browser backend")
    command = backend.get("command")
    task_space = backend.get("task_space")
    if not isinstance(command, str) or not Path(command).is_absolute():
        raise PairingError("approved observer lacks an absolute ego-browser command")
    ego_browser = Path(command).resolve()
    if not ego_browser.is_file() or not os.access(ego_browser, os.X_OK):
        raise PairingError("approved ego-browser command is unavailable")
    if (
        not isinstance(task_space, (str, int))
        or isinstance(task_space, bool)
        or (isinstance(task_space, str) and not task_space)
        or (isinstance(task_space, int) and task_space < 1)
    ):
        raise PairingError("approved observer task_space is invalid")
    return Runtime(
        origin=origin.rstrip("/"),
        core_port=parsed.port,
        data_dir=data_dir,
        observer_dir=observer_dir,
        ego_browser=ego_browser,
        task_space=task_space,
    )


def tool_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def validate_pairing_proof(
    value: Any,
    *,
    expected_port: int | None = None,
    expected_tool_sha256: str | None = None,
) -> dict[str, Any]:
    """Return an exact, normalized pairing proof or reject it."""
    if not isinstance(value, dict) or set(value) != PROOF_KEYS:
        raise ValueError("pairing proof fields do not match schema")
    if value["paired"] is not True or value["code_consumed"] is not True:
        raise ValueError("pairing proof must attest pairing and code consumption")
    if value["pairing_mode"] != PAIRING_MODE:
        raise ValueError("pairing proof mode is not pre-authorized automation")
    if not isinstance(value["tool_version"], str) or not VERSION.fullmatch(value["tool_version"]):
        raise ValueError("pairing proof tool_version is invalid")
    if not isinstance(value["tool_sha256"], str) or not FULL_SHA256.fullmatch(value["tool_sha256"]):
        raise ValueError("pairing proof tool_sha256 is invalid")
    if expected_tool_sha256 is not None and value["tool_sha256"] != expected_tool_sha256:
        raise ValueError("pairing proof tool_sha256 does not match this tool")
    port = value["core_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("pairing proof core_port is invalid")
    if expected_port is not None and port != expected_port:
        raise ValueError("pairing proof core_port does not match the target")
    timestamp = value["timestamp"]
    if not isinstance(timestamp, str):
        raise ValueError("pairing proof timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("pairing proof timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("pairing proof timestamp must include a timezone")
    return dict(value)


def _parse_ego_result(stdout: str, stderr: str) -> dict[str, Any]:
    for line in reversed((stderr + "\n" + stdout).splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "paired" in value:
            return value
    raise PairingError("ego-browser returned no pairing result")


def _ego_pair_script(runtime: Runtime, code: str, timeout_s: float) -> str:
    login_url = f"{runtime.origin}/#/login"
    wait_ms = max(1_000, int(timeout_s * 1_000))
    expression = f"""(async () => {{
      const visible = (node) => !!(node && (node.offsetWidth || node.offsetHeight || node.getClientRects().length));
      const deadline = Date.now() + {wait_ms};
      while (Date.now() < deadline) {{
        const status = await fetch('/auth/status', {{credentials: 'include'}}).then(r => r.json()).catch(() => null);
        if (status && status.authenticated === true) return {{paired: false, code_consumed: false, already_paired: true}};
        const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
        const input = inputs.find(node => /ABCD-12/i.test(node.placeholder || '')) ||
          inputs.find(node => (node.getAttribute('aria-label') || '').toLowerCase().includes('pair')) ||
          (inputs.length === 1 ? inputs[0] : null);
        if (input) {{
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, {json.dumps(code)});
          input.dispatchEvent(new Event('input', {{bubbles: true}}));
          input.dispatchEvent(new Event('change', {{bubbles: true}}));
          if (!input.form) throw new Error('pairing form is unavailable');
          input.form.requestSubmit();
          break;
        }}
        await new Promise(resolve => setTimeout(resolve, 100));
      }}
      while (Date.now() < deadline) {{
        const status = await fetch('/auth/status', {{credentials: 'include'}}).then(r => r.json()).catch(() => null);
        if (status && status.authenticated === true) return {{paired: true, code_consumed: true}};
        await new Promise(resolve => setTimeout(resolve, 100));
      }}
      return {{paired: false, code_consumed: false}};
    }})()"""
    return f"""const task = await useOrCreateTaskSpace({json.dumps(runtime.task_space)});
await openOrReuseTab({json.dumps(login_url)}, {{wait: true, timeout: 25}});
await waitForLoad();
const info = await pageInfo();
if (!info.url.startsWith({json.dumps(runtime.origin + '/')})) throw new Error('unexpected WebUI origin');
const frameTree = await cdp('Page.getFrameTree');
const frameId = frameTree.frameTree.frame.id;
const world = await cdp('Page.createIsolatedWorld', {{frameId, worldName: 'pursers-pairing', grantUniveralAccess: false}});
const evaluated = await cdp('Runtime.evaluate', {{contextId: world.executionContextId, expression: {json.dumps(expression)}, awaitPromise: true, returnByValue: true}});
cliLog(JSON.stringify(evaluated.result.value));
"""


class MacOSPairingDriver:
    """Production adapter; tests use a fake implementation of PairingDriver."""

    _READ_CODE_SCRIPT = r'''
tell application "AionUi" to activate
tell application "System Events"
  if UI elements enabled is false then error "Accessibility permission is required"
  tell process "AionUi"
    set frontmost to true
    keystroke "," using command down
    delay 0.4
    repeat with itemRef in (entire contents of front window)
      try
        set itemName to name of itemRef as text
        if itemName is "Remote" or itemName is "WebUI" then
          perform action "AXPress" of itemRef
          delay 0.2
          exit repeat
        end if
      end try
    end repeat
    set collected to {}
    repeat with itemRef in (entire contents of front window)
      try
        if role of itemRef is "AXStaticText" then
          set end of collected to (value of itemRef as text)
        end if
      end try
    end repeat
    return collected
  end tell
end tell
'''

    _APPROVE_SCRIPT = f'''
tell application "System Events"
  tell process "AionUi"
    if (count of windows) is 0 then return "NO_DIALOG"
    set dialogWindow to front window
    set collected to {{name of dialogWindow as text}}
    repeat with itemRef in (entire contents of dialogWindow)
      try
        if role of itemRef is "AXStaticText" then set end of collected to (value of itemRef as text)
      end try
    end repeat
    set hasTitle to collected contains "{EXPECTED_DIALOG_TITLE}"
    set hasMessage to collected contains "{EXPECTED_DIALOG_MESSAGE}"
    if hasTitle and hasMessage then
      repeat with buttonRef in (buttons of dialogWindow)
        try
          if (name of buttonRef as text) is "{EXPECTED_DIALOG_BUTTON}" then
            click buttonRef
            return "APPROVED"
          end if
        end try
      end repeat
      return "NO_CONFIRM_BUTTON"
    end if
    return "NO_MATCH"
  end tell
end tell
'''

    @staticmethod
    def _osascript(script: str, timeout_s: float) -> str:
        if sys.platform != "darwin":
            raise PairingError("native pairing automation is currently supported only on macOS")
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                text=True,
                capture_output=True,
                check=False,
                timeout=max(0.5, timeout_s),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PairingError("macOS Accessibility automation failed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1:] or ["unknown error"]
            raise PairingError(f"macOS Accessibility automation failed: {detail[0][:180]}")
        return completed.stdout.strip()

    def get_pair_info(self, runtime: Runtime) -> dict[str, Any]:
        request = urllib.request.Request(f"{runtime.origin}/auth/pair-info", method="GET")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                value = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise PairingError("AionUi pairing endpoint is unavailable") from exc
        if not isinstance(value, dict):
            raise PairingError("AionUi pairing endpoint returned an invalid response")
        return value

    def read_pairing_code(self, timeout_s: float) -> str:
        output = self._osascript(self._READ_CODE_SCRIPT, timeout_s)
        matches = set(PAIRING_CODE_SEARCH.findall(output.upper()))
        if len(matches) != 1:
            raise PairingError("expected exactly one active pairing code in AionUi Settings > Remote")
        return matches.pop()

    def begin_browser_pairing(self, runtime: Runtime, code: str, timeout_s: float) -> subprocess.Popen[str]:
        script = _ego_pair_script(runtime, code, timeout_s)
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [str(runtime.ego_browser), "nodejs"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=runtime.observer_dir,
            )
            assert process.stdin is not None
            process.stdin.write(script)
            process.stdin.close()
            process.stdin = None
            return process
        except OSError as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            raise PairingError("ego-browser pairing process could not start") from exc

    def approve_native_dialog(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self._osascript(self._APPROVE_SCRIPT, min(2, timeout_s))
            if result == "APPROVED":
                return
            time.sleep(0.1)
        raise PairingError("exact AionUi WebUI pairing confirmation dialog did not appear")

    def finish_browser_pairing(
        self, handle: subprocess.Popen[str], timeout_s: float
    ) -> dict[str, Any]:
        try:
            stdout, stderr = handle.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            handle.kill()
            handle.communicate()
            raise PairingError("ego-browser pairing timed out") from exc
        if handle.returncode != 0:
            raise PairingError(f"ego-browser pairing failed with exit {handle.returncode}")
        return _parse_ego_result(stdout, stderr)

    def cancel_browser_pairing(self, handle: subprocess.Popen[str]) -> None:
        if handle.poll() is None:
            handle.kill()
            handle.communicate()


def pair(runtime: Runtime, driver: PairingDriver, *, timeout_s: float = 20) -> dict[str, Any]:
    info = driver.get_pair_info(runtime)
    if info.get("desktop_authenticated") is not True:
        raise PairingError("AionUi desktop must be authenticated before pairing")
    if info.get("has_active_code") is not True:
        raise PairingError("AionUi desktop has no active pairing code")
    code = driver.read_pairing_code(timeout_s)
    if not PAIRING_CODE.fullmatch(code):
        raise PairingError("AionUi Accessibility returned an invalid pairing code shape")
    handle = driver.begin_browser_pairing(runtime, code, timeout_s)
    code = ""  # Minimize the lifetime of the transient secret in this process.
    try:
        driver.approve_native_dialog(timeout_s)
        result = driver.finish_browser_pairing(handle, timeout_s)
    except BaseException:
        try:
            driver.cancel_browser_pairing(handle)
        except Exception:
            pass
        raise
    if result.get("paired") is not True or result.get("code_consumed") is not True:
        raise PairingError("browser did not prove an authenticated, consumed-code pairing")
    proof = {
        "paired": True,
        "pairing_mode": PAIRING_MODE,
        "tool_version": TOOL_VERSION,
        "tool_sha256": tool_sha256(),
        "core_port": runtime.core_port,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_consumed": True,
    }
    return validate_pairing_proof(
        proof,
        expected_port=runtime.core_port,
        expected_tool_sha256=tool_sha256(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff-runtime", required=True, help="private handoff.json path")
    parser.add_argument("--timeout-s", type=float, default=20)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    if not 1 <= args.timeout_s <= 60:
        sys.stderr.write("pair_aionui: --timeout-s must be between 1 and 60\n")
        return 2
    try:
        runtime = load_runtime(Path(args.handoff_runtime))
        proof = pair(runtime, MacOSPairingDriver(), timeout_s=args.timeout_s)
    except (PairingError, ValueError) as exc:
        sys.stderr.write(f"pair_aionui: {exc}\n")
        return 7
    json.dump(proof, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
