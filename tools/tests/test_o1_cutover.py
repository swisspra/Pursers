"""Contract tests for the O1 staged cutover toolkit.

Every test builds a synthetic tree under ``tmp_path``: no live service, no real
credential, no private path, and no secret-shaped literal is committed. Token
fixtures are assembled at runtime from claim dictionaries so the repository only
ever contains obviously synthetic placeholder text.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import o1_cutover as cut

TARGET = cut.TARGET_URL
PREVIOUS = "https://127.0.0.1:8766/mcp"
SANDBOX = "http://127.0.0.1:8799/mcp"
SEAT_NAMES = tuple(
    ["seat-alpha-1", "seat-alpha-2", "seat-alpha-3", "seat-beta-1", "seat-beta-2",
     "seat-beta-3", "seat-review-1", "seat-review-2"]
)
SEAT_ROLES = ("worker", "worker", "worker", "worker", "worker", "worker",
              "reviewer", "reviewer")
SEAT_TIERS = (1, 1, 2, 2, 2, 2, 2, 2)
NAMED = tuple(cut.REQUIRED_NAMED_CREDENTIALS)


def _b64(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token(url: str, kid: str, scopes: tuple[str, ...], identifier: str = "synthetic") -> str:
    parsed = cut.parse_loopback_url(url)
    header = _b64({"alg": "none", "typ": "JWT", "kid": kid})
    claims = _b64({
        "iss": parsed.issuer,
        "aud": url,
        "resource": url,
        "scope": " ".join(scopes),
        "client_id": f"client-{identifier}",
        "sub": f"subject-{identifier}",
        "exp": 1_900_000_000,
    })
    return f"{header}.{claims}.synthetic-signature-block"


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    os.chmod(path.parent, 0o700)
    return path


def _iso(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat(
        timespec="seconds")


@dataclass
class Layout:
    tmp: Path
    repo: Path
    live: Path
    staging: Path
    backup: Path
    wheels: Path
    toolkit: Path
    config_path: Path
    old_tokens: dict
    new_tokens: dict

    def load(self) -> cut.CutoverConfig:
        return cut.load_config(self.config_path, repo_root=self.repo)


def build_layout(tmp_path: Path, *, seats: int = 8, team_adapter: bool = False,
                 markers: bool = True) -> Layout:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    live = tmp_path / "live"
    staging = tmp_path / "staging"
    backup = staging / "backup"
    wheels = tmp_path / "release-a25"
    toolkit = tmp_path / "operator-private"

    # --- live (current HTTPS a25) state -----------------------------------
    _write(live / "launch/profile.env",
           f"CENTRAL_JWT_ISSUER=https://127.0.0.1:8766\n"
           f"CENTRAL_JWT_AUDIENCE={PREVIOUS}\n"
           f"CENTRAL_JWKS_PATH={live}/jwks/jwks.json\n")
    _write(live / "launch/launch-central.sh",
           "#!/bin/sh\nexec python -m serve_tls --host 127.0.0.1 --port 8766\n", 0o700)
    _write(live / "jwks/jwks.json", json.dumps({"keys": [{"kid": "kid-old", "kty": "RSA"}]}))
    old_tokens = {}
    for name in NAMED:
        scopes = tuple(sorted(cut.REQUIRED_NAMED_CREDENTIALS[name]))
        old_tokens[name] = _token(PREVIOUS, "kid-old", scopes, name)
        _write(live / f"creds/{name}.jwt", old_tokens[name] + "\n")
    _write(live / "doors/worker.door", "synthetic-door-bundle worker\n")
    _write(live / "doors/reviewer.door", "synthetic-door-bundle reviewer\n")
    _write(live / "configs/coordinator.env",
           f"CENTRAL_URL={PREVIOUS}\nCOORDINATOR_MODE=active\n")
    _write(live / "configs/dashboard.env",
           f"CENTRAL_URL={PREVIOUS}\nDASHBOARD_PORT=8899\n")
    _write(live / "configs/bridge.env", "WAIT_MODE=subscription\n")
    if markers:
        _write(live / "markers/goose-cli.disabled", "legacy goose cli disabled\n", 0o644)

    seat_roots = []
    for index in range(seats):
        root = live / "seats" / f"{index:02d}"
        _write(root / "bin/board.sh",
               f"export PURSERS_BOARDS=registry\nCENTRAL_URL={PREVIOUS}\n", 0o700)
        _write(root / "bin/board.py", "# synthetic seat connector\n")
        _write(root / "AGENTS.md", "# synthetic seat instructions\n")
        _write(root / ".goosehints", "# synthetic seat hints\n")
        seat_roots.append(root)

    # --- staged (target HTTP) state ---------------------------------------
    _write(staging / "staged-configs/profile.env",
           f"CENTRAL_JWT_ISSUER={cut.TARGET_ISSUER}\n"
           f"CENTRAL_JWT_AUDIENCE={cut.TARGET_AUDIENCE}\n"
           f"CENTRAL_JWKS_PATH={staging}/target-jwt/jwks.json\n")
    _write(staging / "staged-configs/launch-central.sh",
           "#!/bin/sh\nexec /PATH/TO/instance/.venv/bin/python "
           "-m pursers_central.pursers_central_runtime "
           f"--host {cut.LOOPBACK_HOST} --port {cut.EXPECTED_PORT} "
           "--data-dir /PATH/TO/instance/data\n", 0o700)
    _write(staging / "target-jwt/jwks.json",
           json.dumps({"keys": [{"kid": "kid-target", "kty": "RSA"}]}))
    new_tokens = {}
    for name in NAMED:
        scopes = tuple(sorted(cut.REQUIRED_NAMED_CREDENTIALS[name]))
        new_tokens[name] = _token(TARGET, "kid-target", scopes, name)
        _write(staging / f"target-jwt/{name}.jwt", new_tokens[name] + "\n")
    _write(staging / "target-jwt/worker.door",
           _token(TARGET, "kid-target", ("board:read",), "worker-door") + "\n")
    _write(staging / "target-jwt/reviewer.door", "synthetic-door-bundle reviewer-target\n")
    _write(staging / "staged-configs/coordinator.env",
           f"CENTRAL_URL={TARGET}\nCOORDINATOR_MODE=active\n"
           f"TOKEN_PATH={staging}/target-jwt/coordinator-main.jwt\n")
    _write(staging / "staged-configs/dashboard.env",
           f"CENTRAL_URL={TARGET}\nDASHBOARD_URL={cut.DASHBOARD_UI_URL}\n")
    _write(staging / "staged-configs/bridge.env", "WAIT_MODE=subscription\n")

    for index in range(seats):
        staged = staging / "staged-seats" / f"{index:02d}"
        _write(staged / "bin/board.sh",
               f"export PURSERS_BOARDS=registry\nCENTRAL_URL={TARGET}\n", 0o700)
        _write(staged / "bin/board.py", "# synthetic staged seat connector\n")
        _write(staged / "AGENTS.md", "# synthetic staged seat instructions\n")
        _write(staged / ".goosehints", "# synthetic staged seat hints\n")

    # --- synthetic release wheels with pinned digests ----------------------
    digests = {}
    for filename in cut.A25_WHEEL_DIGESTS:
        wheel = wheels / filename
        wheel.parent.mkdir(parents=True, exist_ok=True)
        wheel.write_bytes(b"synthetic wheel payload for " + filename.encode("ascii"))
        digests[filename] = cut.sha256_file(wheel)

    _write(toolkit / "jwt_provision.py", "# synthetic operator-private provisioner\n", 0o600)

    # --- snapshots ---------------------------------------------------------
    boards = {board: {state: 0 for state in cut.HELD_TICKET_STATES} | {"open": 3}
              for board in cut.DEFAULT_ACTIVE_BOARDS}
    _write(staging / "cutover-evidence/board-snapshot.json", json.dumps({
        "schema_version": 1, "recorded_at": _iso(), "dispatch_paused": True,
        "teams_paused": True, "boards": boards}))
    memberships = []
    membership_snapshot: dict[str, list] = {board: [] for board in cut.DEFAULT_ACTIVE_BOARDS}
    for board in cut.DEFAULT_ACTIVE_BOARDS:
        for identifier, role in (("coordinator-main", "admin"),
                                 ("coordinator-intake", "intake"),
                                 ("dashboard-admin", "admin"),
                                 ("worker-door", "member"),
                                 ("reviewer-door", "reviewer")):
            principal = f"PR-synthetic-{board}-{identifier}"
            memberships.append({"board": board, "identifier": identifier,
                                "principal_id": principal, "role": role})
            membership_snapshot[board].append({"principal_id": principal, "role": role})
    _write(staging / "cutover-evidence/membership-snapshot.json", json.dumps({
        "schema_version": 1, "recorded_at": _iso(), "url": TARGET,
        "boards": membership_snapshot}))
    _write(staging / "cutover-evidence/seats-baseline.json", json.dumps([
        {"name": SEAT_NAMES[i], "role": SEAT_ROLES[i], "tier": SEAT_TIERS[i]}
        for i in range(seats)]))
    for directory in ("cutover-evidence", "journal", "staged-configs", "staged-seats",
                      "target-jwt", "target-jwt/door-keys", "backup"):
        path = staging / directory
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    os.chmod(staging, 0o700)

    config = {
        "schema_version": 1,
        "repo_root": str(repo),
        "staging_root": str(staging),
        "live_root": str(live),
        "backup_root": str(backup),
        "target_url": TARGET,
        "previous_url": PREVIOUS,
        "active_boards": list(cut.DEFAULT_ACTIVE_BOARDS),
        "launcher": {
            "live_profile": str(live / "launch/profile.env"),
            "staged_profile": str(staging / "staged-configs/profile.env"),
            "live_script": str(live / "launch/launch-central.sh"),
            "staged_script": str(staging / "staged-configs/launch-central.sh"),
            "venv_python": str(staging / "venv/bin/python"),
            "expected_digests": {},
        },
        "jwks": {"live": str(live / "jwks/jwks.json"),
                 "staged": str(staging / "target-jwt/jwks.json")},
        "credentials": [
            {"identifier": name, "live": str(live / f"creds/{name}.jwt"),
             "staged": str(staging / f"target-jwt/{name}.jwt")} for name in NAMED],
        "doors": [
            {"role": "worker", "live": str(live / "doors/worker.door"),
             "staged": str(staging / "target-jwt/worker.door")},
            {"role": "reviewer", "live": str(live / "doors/reviewer.door"),
             "staged": str(staging / "target-jwt/reviewer.door")}],
        "seats": [
            {"name": SEAT_NAMES[i], "host": "synthetic-host", "role": SEAT_ROLES[i],
             "tier": SEAT_TIERS[i], "root": str(seat_roots[i])} for i in range(seats)],
        "seats_baseline": str(staging / "cutover-evidence/seats-baseline.json"),
        "configs": [
            {"identifier": "coordinator.env",
             "live": str(live / "configs/coordinator.env"),
             "staged": str(staging / "staged-configs/coordinator.env"),
             "required_substrings": [TARGET, "COORDINATOR_MODE=active"],
             "forbidden_substrings": []},
            {"identifier": "dashboard.env",
             "live": str(live / "configs/dashboard.env"),
             "staged": str(staging / "staged-configs/dashboard.env"),
             "required_substrings": [TARGET],
             "forbidden_substrings": []},
            {"identifier": "bridge.env",
             "live": str(live / "configs/bridge.env"),
             "staged": str(staging / "staged-configs/bridge.env"),
             "required_substrings": ["WAIT_MODE=subscription"],
             "forbidden_substrings": []}],
        "board_snapshot": str(staging / "cutover-evidence/board-snapshot.json"),
        "membership_snapshot": str(staging / "cutover-evidence/membership-snapshot.json"),
        "memberships": memberships,
        "legacy_markers": [str(live / "markers/goose-cli.disabled")] if markers else [],
        "extra_backup_paths": [{"identifier": "central-data/boards.sqlite",
                                "path": str(live / "data/boards.sqlite")}],
        "wheels_dir": str(wheels),
        "wheel_digests": digests,
        "operator_toolkit": str(toolkit),
        "team_adapter": str(repo / "tools/aionui-extension/team/adapter.cjs") if team_adapter else None,
        "helper_bin": None,
        "snapshot_max_age_s": 900,
        "evidence_max_age_s": 900,
    }
    _write(live / "data/boards.sqlite", "synthetic sqlite payload\n", 0o600)
    _write(staging / "venv/bin/python", "#!/bin/sh\nexit 0\n", 0o700)
    config_path = staging / "o1-config.json"
    _write(config_path, json.dumps(config, indent=2, sort_keys=True))
    if team_adapter:
        _write(repo / "tools/aionui-extension/team/adapter.cjs",
               "// synthetic team lifecycle adapter\n", 0o600)
    return Layout(tmp_path, repo, live, staging, backup, wheels, toolkit, config_path,
                  old_tokens, new_tokens)


def _prepare_and_backup(layout: Layout) -> cut.CutoverConfig:
    config = layout.load()
    cut.run_prepare(config)
    cut.run_backup(config)
    return config


def _dry_run_ok(config: cut.CutoverConfig) -> Path:
    findings, _extra, evidence = cut.run_dry_run(config)
    assert cut.findings_ok(findings), [f.as_dict() for f in findings if not f.ok]
    return evidence


# ---------------------------------------------------------------------------
# Plan shape
# ---------------------------------------------------------------------------


def test_plan_orders_phases_and_separates_operator_lifecycle():
    phases = [step.phase for step in cut.PLAN]
    assert phases.index("prepare") < phases.index("preflight") < phases.index("dry-run")
    assert phases.index("dry-run") < phases.index("activate") < phases.index("rollback")
    assert cut.TOOLKIT_ACTIVATION_STEPS == (
        "swap-launcher", "swap-jwks", "swap-credentials", "swap-configs", "swap-seats")
    operator_steps = {s.step_id for s in cut.PLAN if s.execution == "operator"}
    assert {"declare-freeze", "stop-consumers", "start-central", "start-consumers",
            "doctor-and-resume", "rollback-restart"} <= operator_steps
    assert not any("start-all" in step.step_id or "start-all" in step.summary
                   for step in cut.PLAN)


def test_dry_run_step_requires_every_gate():
    step = next(s for s in cut.PLAN if s.step_id == "dry-run")
    assert set(step.requires) == set(cut.GATES)


def test_swaps_are_covered_by_the_rollback_unit(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    unit = {identifier for identifier, _ in config.rollback_unit()}
    swaps = {swap.identifier for swap in config.swaps()}
    assert swaps <= unit
    assert len(config.swaps()) > 0


# ---------------------------------------------------------------------------
# Loopback URL binding
# ---------------------------------------------------------------------------


def test_parse_loopback_url_accepts_exact_target():
    parsed = cut.require_target_url(TARGET)
    assert parsed.normalized == TARGET
    assert parsed.issuer == cut.TARGET_ISSUER


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8766",
    "http://localhost:8766/mcp",
    "http://0.0.0.0:8766/mcp",
    "http://127.0.0.1/mcp",
    "http://user:pass@127.0.0.1:8766/mcp",
    "http://127.0.0.1:8766/mcp?x=1",
    "http://127.0.0.1:8766/mcp#frag",
    "http://127.0.0.1:notaport/mcp",
    "http://127.0.0.1:99999/mcp",
    "",
    "   ",
    "127.0.0.1:8766/mcp",
    "ftp://127.0.0.1:8766/mcp",
])
def test_parse_loopback_url_refuses_malformed_resources(url):
    with pytest.raises(cut.GateFailure):
        cut.parse_loopback_url(url)


def test_parse_loopback_url_accepts_the_previous_https_resource():
    parsed = cut.parse_loopback_url(PREVIOUS)
    assert parsed.scheme == "https"
    assert parsed.issuer == "https://127.0.0.1:8766"


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8766/mcp",
    "http://127.0.0.1:8767/mcp",
    "http://127.0.0.1:8766/mcp/",
    "http://127.0.0.1:8766/",
    "http://127.0.0.1:8766/board",
])
def test_require_target_url_refuses_near_misses(url):
    with pytest.raises(cut.GateFailure):
        cut.require_target_url(url)


def test_evidence_url_acceptable_refuses_other_port_sandbox():
    assert cut.evidence_url_acceptable(TARGET, TARGET) is True
    assert cut.evidence_url_acceptable(SANDBOX, TARGET) is False
    assert cut.evidence_url_acceptable(PREVIOUS, TARGET) is False
    assert cut.evidence_url_acceptable("not-a-url", TARGET) is False


def test_config_rejects_previous_url_equal_to_target(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["previous_url"] = TARGET
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError):
        layout.load()


# ---------------------------------------------------------------------------
# Credential coherence
# ---------------------------------------------------------------------------


def test_credential_binding_requires_strict_aud_resource_issuer():
    token = _token(TARGET, "kid-target", ("board:read", "board:write"), "x")
    findings = cut.credential_binding_findings("x", token, TARGET,
                                               frozenset({"board:read"}), frozenset())
    assert cut.findings_ok(findings)


def test_credential_binding_refuses_wrong_issuer_and_resource():
    parsed = cut.parse_loopback_url(TARGET)
    bad_iss = f"{_b64({'alg': 'none'})}.{_b64({'iss': 'https://127.0.0.1:8766', 'aud': TARGET, 'resource': TARGET})}.synthetic-signature-block"
    findings = cut.credential_binding_findings("bad-iss", bad_iss, TARGET)
    assert not cut.findings_ok(findings)
    assert parsed.issuer in json.dumps([f.as_dict() for f in findings])

    bad_resource = f"{_b64({'alg': 'none'})}.{_b64({'iss': cut.TARGET_ISSUER, 'aud': TARGET, 'resource': SANDBOX})}.synthetic-signature-block"
    assert not cut.findings_ok(cut.credential_binding_findings("bad-res", bad_resource, TARGET))

    multi_aud = f"{_b64({'alg': 'none'})}.{_b64({'iss': cut.TARGET_ISSUER, 'aud': [TARGET, SANDBOX], 'resource': TARGET})}.synthetic-signature-block"
    assert not cut.findings_ok(cut.credential_binding_findings("multi", multi_aud, TARGET))

    missing = f"{_b64({'alg': 'none'})}.{_b64({'iss': cut.TARGET_ISSUER, 'aud': TARGET})}.synthetic-signature-block"
    assert not cut.findings_ok(cut.credential_binding_findings("missing", missing, TARGET))


def test_credential_binding_refuses_scope_violations():
    intake = _token(TARGET, "kid-target", ("board:read", "board:coordinate",
                                           "board:intake", "board:write"), "intake")
    findings = cut.credential_binding_findings(
        "coordinator-intake", intake, TARGET,
        cut.REQUIRED_NAMED_CREDENTIALS["coordinator-intake"],
        cut.FORBIDDEN_CREDENTIAL_SCOPES["coordinator-intake"])
    assert not cut.findings_ok(findings)

    thin = _token(TARGET, "kid-target", ("board:read",), "main")
    assert not cut.findings_ok(cut.credential_binding_findings(
        "coordinator-main", thin, TARGET,
        cut.REQUIRED_NAMED_CREDENTIALS["coordinator-main"], frozenset()))


def test_decode_token_claims_rejects_malformed_and_never_echoes_the_token():
    with pytest.raises(cut.GateFailure):
        cut.decode_token_claims("only.two", "broken")
    token = _token(TARGET, "kid-target", ("board:read",), "echo")
    findings = cut.credential_binding_findings("echo", token, TARGET)
    blob = json.dumps([finding.as_dict() for finding in findings])
    assert token not in blob
    assert token.split(".")[1] not in blob


def test_token_kid_comes_only_from_nonempty_jose_header():
    header_only = _token(TARGET, "kid-header", ("board:read",), "header-only")
    assert cut.token_kid(header_only, "header-only") == "kid-header"

    conflicting = (
        f"{_b64({'alg': 'none', 'kid': 'kid-header'})}."
        f"{_b64({'iss': cut.TARGET_ISSUER, 'aud': TARGET, 'resource': TARGET, 'kid': 'kid-payload'})}."
        "synthetic-signature-block"
    )
    assert cut.token_kid(conflicting, "conflicting") == "kid-header"

    missing = (
        f"{_b64({'alg': 'none'})}."
        f"{_b64({'iss': cut.TARGET_ISSUER, 'aud': TARGET, 'resource': TARGET})}."
        "synthetic-signature-block"
    )
    assert cut.token_kid(missing, "missing") is None
    findings = cut.credential_binding_findings("missing", missing, TARGET)
    assert not cut.findings_ok(findings)
    assert "JOSE header kid" in " ".join(f.detail for f in findings if not f.ok)


def test_jwks_kid_coherence_is_fail_closed():
    ok = cut.kid_coherence_finding("credentials", "set", ["kid-a"], {"kid-a", "kid-b"})
    assert ok.ok
    bad = cut.kid_coherence_finding("credentials", "set", ["kid-a", "kid-c"], {"kid-a"})
    assert not bad.ok
    assert not cut.kid_coherence_finding("credentials", "set", [], {"kid-a"}).ok
    assert not cut.kid_coherence_finding("credentials", "set", [None], {"kid-a"}).ok
    with pytest.raises(cut.GateFailure):
        cut.jwks_kids({"keys": "not-a-list"})


# ---------------------------------------------------------------------------
# Safe path discovery and configuration refusals
# ---------------------------------------------------------------------------


def test_staging_root_inside_repository_is_refused(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["staging_root"] = str(layout.repo / "staging")
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="repository"):
        layout.load()


def test_operator_config_outside_staging_is_refused_without_mutation(tmp_path):
    layout = build_layout(tmp_path)
    outside = tmp_path / "outside-config.json"
    original = layout.config_path.read_bytes()
    outside.write_bytes(original)
    with pytest.raises(cut.ConfigError, match="operator config"):
        cut.load_config(outside, repo_root=layout.repo)
    assert outside.read_bytes() == original


@pytest.mark.parametrize("field", [
    "board_snapshot", "membership_snapshot", "seats_baseline",
])
def test_evidence_outside_staging_is_refused_without_mutation(tmp_path, field):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    outside = tmp_path / f"outside-{field}.json"
    original = b"outside evidence remains unchanged\n"
    outside.write_bytes(original)
    raw[field] = str(outside)
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="snapshot|baseline"):
        layout.load()
    assert outside.read_bytes() == original


@pytest.mark.parametrize("section,key", [
    ("launcher", "staged_profile"),
    ("jwks", "staged"),
])
def test_staged_swap_source_outside_staging_is_refused_without_mutation(
        tmp_path, section, key):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside-staged"
    original = b"outside staged bytes remain unchanged\n"
    outside.write_bytes(original)
    raw[section][key] = str(outside)
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="staged swap source"):
        layout.load()
    assert outside.read_bytes() == original


def test_live_target_outside_live_root_is_refused_without_mutation(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside-live"
    original = b"outside live bytes remain unchanged\n"
    outside.write_bytes(original)
    raw["configs"][0]["live"] = str(outside)
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="live mutation target"):
        layout.load()
    assert outside.read_bytes() == original


def test_preflight_refuses_staged_symlink_parent_without_mutation(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    staged_configs = layout.staging / "staged-configs"
    saved = layout.staging / "saved-staged-configs"
    staged_configs.rename(saved)
    outside = tmp_path / "outside-staged-parent"
    outside.mkdir()
    sentinel = outside / "profile.env"
    original = b"outside symlink target remains unchanged\n"
    sentinel.write_bytes(original)
    staged_configs.symlink_to(outside, target_is_directory=True)
    with pytest.raises(cut.ConfigError, match="staged swap source"):
        cut.run_preflight(config)
    assert sentinel.read_bytes() == original


def test_generated_output_outside_staging_is_refused_without_mutation(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    outside = tmp_path / "outside-output.json"
    original = b"outside output remains unchanged\n"
    outside.write_bytes(original)
    with pytest.raises(cut.ConfigError, match="beneath staging_root"):
        cut.run_dry_run(config, outside)
    assert outside.read_bytes() == original


def test_rollback_unit_inside_repository_is_refused(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    inside = layout.repo / "leaked-live.env"
    _write(inside, "synthetic\n")
    raw["configs"][0]["live"] = str(inside)
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="rollback unit"):
        layout.load()


def test_relative_paths_are_refused(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["live_root"] = "relative/live"
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="absolute"):
        layout.load()


def test_backup_root_must_be_a_distinct_descendant_of_staging(tmp_path):
    template = cut.config_template()
    assert Path(template["backup_root"]).is_relative_to(Path(template["staging_root"]))
    for name, backup in (
        ("outside", tmp_path / "outside-backup"),
        ("same", tmp_path / "same" / "staging"),
    ):
        layout = build_layout(tmp_path / name)
        raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
        raw["backup_root"] = str(backup if name == "outside" else layout.staging)
        layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(cut.ConfigError, match="backup_root"):
            layout.load()
        assert not (backup / "manifest.json").exists()


@pytest.mark.parametrize("identifier", [
    "../escape", "/absolute", "configs/../escape", r"configs\\escape",
])
def test_rollback_identifiers_refuse_traversal_and_absolute_paths(tmp_path, identifier):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["extra_backup_paths"][0]["identifier"] = identifier
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="identifier"):
        layout.load()
    assert not config_manifest_exists(layout)


@pytest.mark.parametrize("identifier", ["jwks/jwks.json", "jwks"])
def test_colliding_rollback_identifiers_are_refused(tmp_path, identifier):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["extra_backup_paths"][0]["identifier"] = identifier
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="collide"):
        layout.load()
    assert not config_manifest_exists(layout)


def test_backup_identifier_symlink_is_refused_before_copy(tmp_path):
    layout = build_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (layout.backup / "configs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(cut.ConfigError, match="symlink"):
        layout.load()
    assert list(outside.iterdir()) == []


def config_manifest_exists(layout: Layout) -> bool:
    return (layout.backup / "manifest.json").exists()


def test_config_requires_named_credentials_and_both_doors(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["credentials"] = raw["credentials"][:2]
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="named control-plane credential"):
        layout.load()

    door_layout = build_layout(tmp_path / "doors")
    raw = json.loads(door_layout.config_path.read_text(encoding="utf-8"))
    raw["doors"] = [raw["doors"][0]]
    door_layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="worker and reviewer doors"):
        door_layout.load()


def test_config_rejects_out_of_range_seat_tier(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["seats"][0]["tier"] = 3
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="tier"):
        layout.load()


def test_config_rejects_unknown_schema_version(tmp_path):
    layout = build_layout(tmp_path)
    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    layout.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cut.ConfigError, match="schema_version"):
        layout.load()


def test_operator_toolkit_discovery_is_reported_not_embedded(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    assert config.operator_toolkit == layout.toolkit
    prepare_sheet = "\n".join(cut.command_sheet(config, "prepare"))
    sheet = "\n".join(cut.command_sheet(config, "preflight"))
    assert str(layout.toolkit) in sheet  # private local handoff keeps real paths
    assert str(layout.staging) in prepare_sheet
    report = json.dumps(cut.build_report("prepare", config, [], {"sheet": sheet}))
    assert str(layout.toolkit) not in report
    assert "/PATH/TO/operator-private" in report


# ---------------------------------------------------------------------------
# Prepare, backup and integrity
# ---------------------------------------------------------------------------


def test_prepare_creates_private_skeleton_and_command_sheets(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    result = cut.run_prepare(config)
    for relative in cut.STAGING_SUBDIRS:
        path = config.staging_root / relative
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for phase, sheet in result["command_sheets"].items():
        assert Path(sheet).is_file()
        assert stat.S_IMODE(Path(sheet).stat().st_mode) == 0o600
    assert result["plan_hash"] == cut.plan_hash()
    assert result["activation_steps"] == list(cut.TOOLKIT_ACTIVATION_STEPS)


def test_every_generated_toolkit_command_parses_and_snapshots_name_sources(tmp_path):
    layout = build_layout(tmp_path / "private paths with spaces")
    config = layout.load()
    parser = cut.build_parser()
    emitted = []
    for phase in ("prepare", "preflight", "activate", "rollback"):
        emitted.extend(cut.command_sheet(config, phase))
    toolkit_lines = [
        line for line in emitted
        if line.startswith("python ") and "o1_cutover.py" in line
    ]
    assert toolkit_lines
    for line in toolkit_lines:
        argv = shlex.split(line)
        parsed = parser.parse_args(argv[2:])
        assert parsed.command
        assert parsed.config == config.path
        if parsed.command in ("snapshot-boards", "snapshot-memberships"):
            assert parsed.from_json is not None


def test_backup_manifest_hashes_every_rollback_unit_member(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    manifest = json.loads(config.backup_manifest_path.read_text(encoding="utf-8"))
    identifiers = {item["identifier"] for item in manifest["artifacts"]}
    assert {identifier for identifier, _ in config.rollback_unit()} <= identifiers
    findings = cut.verify_backup_integrity(config)
    assert cut.findings_ok(findings), [f.as_dict() for f in findings if not f.ok]


def test_backup_integrity_refuses_tampered_or_missing_members(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    member = config.backup_root / "jwks/jwks.json"
    member.write_text(json.dumps({"keys": [{"kid": "kid-tampered"}]}), encoding="utf-8")
    os.chmod(member, 0o600)
    findings = cut.verify_backup_integrity(config)
    assert not cut.findings_ok(findings)

    member.unlink()
    assert not cut.findings_ok(cut.verify_backup_integrity(config))


def test_backup_integrity_refuses_incomplete_unit(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    manifest = json.loads(config.backup_manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = [item for item in manifest["artifacts"]
                             if not item["identifier"].startswith("seats/")]
    cut.write_json_atomic(config.backup_manifest_path, manifest)
    findings = cut.verify_backup_integrity(config)
    assert not cut.findings_ok(findings)
    assert any("incomplete" in f.detail for f in findings if not f.ok)


def test_backup_currency_detects_live_drift(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.backup_is_current(config))
    _write(layout.live / "configs/coordinator.env", "CENTRAL_URL=drifted\n")
    findings = cut.backup_is_current(config)
    assert not cut.findings_ok(findings)


# ---------------------------------------------------------------------------
# Dry run: prove targets, change nothing
# ---------------------------------------------------------------------------


def test_dry_run_passes_and_mutates_nothing(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    before = cut.live_state_fingerprint(config)
    findings, extra, evidence = cut.run_dry_run(config)
    refusals = [f.as_dict() for f in findings if not f.ok]
    assert cut.findings_ok(findings), refusals
    assert extra["mutations"] == []
    assert cut.live_state_fingerprint(config) == before
    assert extra["targets_proven"] == len(config.swaps())
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["ok"] is True
    assert record["plan_hash"] == cut.plan_hash()
    assert record["target_url"] == TARGET
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert len(record["targets"]) == len(config.swaps())


def test_dry_run_report_is_sanitized(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    findings, extra, _evidence = cut.run_dry_run(config)
    report = json.dumps(cut.build_report("dry-run", config, findings, extra))
    for private in (str(layout.live), str(layout.staging), str(layout.repo),
                    str(layout.wheels), str(layout.toolkit)):
        assert private not in report
    assert "/PATH/TO/staging" in report
    for token in list(layout.old_tokens.values()) + list(layout.new_tokens.values()):
        assert token not in report


def test_dry_run_refuses_held_tickets(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    snapshot = json.loads(config.board_snapshot.read_text(encoding="utf-8"))
    snapshot["boards"]["pursers"]["submitted"] = 1
    snapshot["recorded_at"] = _iso()
    cut.write_json_atomic(config.board_snapshot, snapshot)
    findings = cut.gate_drain(config)
    assert not cut.findings_ok(findings)
    assert any("holds 1 ticket" in f.detail for f in findings if not f.ok)
    dry = cut.run_dry_run(config)[0]
    assert not cut.findings_ok(dry)


def test_dry_run_refuses_unpaused_or_stale_snapshot(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    snapshot = json.loads(config.board_snapshot.read_text(encoding="utf-8"))
    snapshot["dispatch_paused"] = False
    cut.write_json_atomic(config.board_snapshot, snapshot)
    assert not cut.findings_ok(cut.gate_drain(config))

    snapshot = json.loads(config.board_snapshot.read_text(encoding="utf-8"))
    snapshot["dispatch_paused"] = True
    snapshot["teams_paused"] = False
    cut.write_json_atomic(config.board_snapshot, snapshot)
    assert not cut.findings_ok(cut.gate_drain(config))

    snapshot = json.loads(config.board_snapshot.read_text(encoding="utf-8"))
    snapshot["teams_paused"] = True
    snapshot["recorded_at"] = _iso(-7200)
    cut.write_json_atomic(config.board_snapshot, snapshot)
    findings = cut.gate_drain(config)
    assert not cut.findings_ok(findings)
    assert any("stale" in f.detail for f in findings if not f.ok)


def test_dry_run_refuses_missing_board_snapshot(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    config.board_snapshot.unlink()
    assert not cut.findings_ok(cut.gate_drain(config))


def test_membership_gate_refuses_sandbox_evidence(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_membership(config))
    snapshot = json.loads(config.membership_snapshot.read_text(encoding="utf-8"))
    snapshot["url"] = SANDBOX
    cut.write_json_atomic(config.membership_snapshot, snapshot)
    findings = cut.gate_membership(config)
    assert not cut.findings_ok(findings)
    assert any("sandbox" in f.detail for f in findings if not f.ok)


def test_membership_gate_requires_every_expected_principal(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    snapshot = json.loads(config.membership_snapshot.read_text(encoding="utf-8"))
    removed = snapshot["boards"]["pursers"].pop()
    cut.write_json_atomic(config.membership_snapshot, snapshot)
    findings = cut.gate_membership(config)
    assert not cut.findings_ok(findings)
    assert any(removed["principal_id"] in json.dumps(f.as_dict()) or "not a member" in f.detail
               for f in findings if not f.ok)


def test_seat_gate_preserves_names_roles_and_tiers(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_seats(config))

    baseline = json.loads(config.seats_baseline.read_text(encoding="utf-8"))
    baseline[0]["tier"] = 2
    cut.write_json_atomic(config.seats_baseline, baseline)
    findings = cut.gate_seats(config)
    assert not cut.findings_ok(findings)
    assert any("role or tier" in f.detail for f in findings if not f.ok)


def test_seat_gate_refuses_wrong_count_and_duplicate_names(tmp_path):
    layout = build_layout(tmp_path, seats=7)
    config = layout.load()
    findings = cut.gate_seats(config)
    assert not cut.findings_ok(findings)
    assert any("exactly 8" in f.detail for f in findings if not f.ok)

    layout = build_layout(tmp_path / "dup")
    (tmp_path / "dup").mkdir(exist_ok=True)
    config = layout.load()
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["seats"][1]["name"] = raw["seats"][0]["name"]
    cut.write_json_atomic(config.path, raw)
    config = layout.load()
    findings = cut.gate_seats(config)
    assert not cut.findings_ok(findings)
    assert any("unique" in f.detail for f in findings if not f.ok)


def test_seat_gate_refuses_missing_staged_wrapper_or_registry_scope(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    wrapper = config.staged_seat_root / "03" / "bin/board.sh"
    wrapper.write_text("export PURSERS_BOARDS=home\n", encoding="utf-8")
    findings = cut.gate_seats(config)
    assert not cut.findings_ok(findings)
    assert any("registry board scope" in f.detail for f in findings if not f.ok)

    wrapper.write_text(f"export PURSERS_BOARDS=registry\nCENTRAL_URL={SANDBOX}\n",
                       encoding="utf-8")
    findings = cut.gate_seats(config)
    assert not cut.findings_ok(findings)
    assert any("target resource" in f.detail for f in findings if not f.ok)


def test_config_gate_refuses_ca_override_start_all_and_poll(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_configs(config))
    staged = Path(next(s.staged for s in config.config_files if s.identifier == "bridge.env"))
    staged.write_text("WAIT_MODE=subscription\nPURSERS_CA_FILE=/PATH/TO/ca.pem\n",
                      encoding="utf-8")
    findings = cut.gate_configs(config)
    assert not cut.findings_ok(findings)

    staged.write_text("WAIT_MODE=subscription\nbin/board.sh wait --poll\n", encoding="utf-8")
    assert not cut.findings_ok(cut.gate_configs(config))

    staged.write_text("WAIT_MODE=subscription\nstart-all\n", encoding="utf-8")
    assert not cut.findings_ok(cut.gate_configs(config))


def test_config_gate_requires_coordinator_dashboard_and_bridge(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["configs"] = [entry for entry in raw["configs"] if entry["identifier"] != "bridge.env"]
    cut.write_json_atomic(config.path, raw)
    config = layout.load()
    findings = cut.gate_configs(config)
    assert not cut.findings_ok(findings)
    assert any("bridge" in f.detail for f in findings if not f.ok)


def test_legacy_gate_requires_disabled_markers(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_legacy(config))
    (layout.live / "markers/goose-cli.disabled").unlink()
    findings = cut.gate_legacy(config)
    assert not cut.findings_ok(findings)

    layout = build_layout(tmp_path / "nomarker", markers=False)
    (tmp_path / "nomarker").mkdir(exist_ok=True)
    config = layout.load()
    assert not cut.findings_ok(cut.gate_legacy(config))


def test_artifact_gate_verifies_pinned_digests_and_launcher(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_artifacts(config))

    (layout.wheels / next(iter(cut.A25_WHEEL_DIGESTS))).write_bytes(b"corrupted wheel")
    assert not cut.findings_ok(cut.gate_artifacts(config))


def test_artifact_gate_refuses_tls_shim_or_missing_runtime_module(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    script = config.launcher.staged_script
    script.write_text("#!/bin/sh\nexec python -m serve_tls --port 8766\n", encoding="utf-8")
    findings = cut.gate_artifacts(config)
    assert not cut.findings_ok(findings)
    details = " ".join(f.detail for f in findings if not f.ok)
    assert "released runtime module" in details or "TLS shim" in details


def test_launcher_hash_preflight_is_enforced(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["launcher"]["expected_digests"] = {"profile": "0" * 64}
    cut.write_json_atomic(config.path, raw)
    config = layout.load()
    findings = cut.gate_artifacts(config)
    assert not cut.findings_ok(findings)
    assert any("hash preflight mismatch" in f.detail for f in findings if not f.ok)

    raw["launcher"]["expected_digests"] = {
        "profile": cut.sha256_file(config.launcher.staged_profile)}
    cut.write_json_atomic(config.path, raw)
    config = layout.load()
    assert cut.findings_ok(cut.gate_artifacts(config))


def test_credential_gate_refuses_mode_and_kid_drift(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    assert cut.findings_ok(cut.gate_credentials(config))

    staged = config.credentials[0].staged
    os.chmod(staged, 0o644)
    findings = cut.gate_credentials(config)
    assert not cut.findings_ok(findings)
    assert any("mode must be 0o600" in f.detail for f in findings if not f.ok)
    os.chmod(staged, 0o600)

    cut.write_json_atomic(config.jwks_staged, {"keys": [{"kid": "kid-other"}]})
    findings = cut.gate_credentials(config)
    assert not cut.findings_ok(findings)
    assert any("JWKS" in f.detail for f in findings if not f.ok)


def test_credential_gate_detects_active_jwks_rotation(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    _write(layout.live / "jwks/jwks.json",
           json.dumps({"keys": [{"kid": "kid-rotated", "kty": "RSA"}]}))
    findings = cut.gate_credentials(config)
    assert not cut.findings_ok(findings)
    assert any("never be rotated" in f.detail for f in findings if not f.ok)


def test_host_capability_reports_honest_gap_and_refuses_missing_adapter(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    findings = cut.gate_host_capability(config)
    assert cut.findings_ok(findings)
    assert any(f.evidence.get("capability") == "operator_manual" for f in findings)

    layout = build_layout(tmp_path / "adapter", team_adapter=True)
    config = layout.load()
    findings = cut.gate_host_capability(config)
    assert cut.findings_ok(findings)
    assert any(f.evidence.get("capability") == "team_adapter" for f in findings)

    (layout.repo / "tools/aionui-extension/team/adapter.cjs").unlink()
    findings = cut.gate_host_capability(config)
    assert not cut.findings_ok(findings)
    assert any("hot-reload" in f.detail for f in findings if not f.ok)


# ---------------------------------------------------------------------------
# Activation, interruption and rollback
# ---------------------------------------------------------------------------


def test_activate_requires_operator_confirmation_and_matching_evidence(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    with pytest.raises(cut.GateFailure, match="operator confirmation"):
        cut.run_activate(config, evidence, False)

    record = json.loads(evidence.read_text(encoding="utf-8"))
    record["plan_hash"] = "0" * 64
    other = config.evidence_dir / "tampered.json"
    cut.write_json_atomic(other, record)
    with pytest.raises(cut.GateFailure, match="plan_hash"):
        cut.run_activate(config, other, True)

    record = json.loads(evidence.read_text(encoding="utf-8"))
    record["recorded_at"] = _iso(-7200)
    stale = config.evidence_dir / "stale.json"
    cut.write_json_atomic(stale, record)
    with pytest.raises(cut.GateFailure, match="stale"):
        cut.run_activate(config, stale, True)

    record = json.loads(evidence.read_text(encoding="utf-8"))
    record["target_url"] = SANDBOX
    wrong = config.evidence_dir / "wrong-url.json"
    cut.write_json_atomic(wrong, record)
    with pytest.raises(cut.GateFailure, match="exact target resource"):
        cut.run_activate(config, wrong, True)

    record = json.loads(evidence.read_text(encoding="utf-8"))
    record["ok"] = False
    refused = config.evidence_dir / "refused.json"
    cut.write_json_atomic(refused, record)
    with pytest.raises(cut.GateFailure, match="refused dry run"):
        cut.run_activate(config, refused, True)

    record = json.loads(evidence.read_text(encoding="utf-8"))
    record["mutations"] = ["configs/coordinator.env"]
    mutated = config.evidence_dir / "mutated.json"
    cut.write_json_atomic(mutated, record)
    with pytest.raises(cut.GateFailure, match="live mutations"):
        cut.run_activate(config, mutated, True)


def test_activate_refuses_when_a_gate_is_refused(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    snapshot = json.loads(config.board_snapshot.read_text(encoding="utf-8"))
    snapshot["boards"]["fullplatts"]["claimed"] = 2
    snapshot["recorded_at"] = _iso()
    cut.write_json_atomic(config.board_snapshot, snapshot)
    with pytest.raises(cut.GateFailure, match="gate refusal"):
        cut.run_activate(config, evidence, True)


def test_activate_swaps_every_target_atomically_and_journals(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    original_profile = (layout.live / "launch/profile.env").read_text(encoding="utf-8")
    result = cut.run_activate(config, evidence, True)

    assert len(result["performed"]) == len(config.swaps())
    assert (layout.live / "launch/profile.env").read_text(encoding="utf-8") == \
        (layout.staging / "staged-configs/profile.env").read_text(encoding="utf-8")
    assert original_profile != (layout.live / "launch/profile.env").read_text(encoding="utf-8")
    assert json.loads((layout.live / "jwks/jwks.json").read_text(encoding="utf-8")) == \
        {"keys": [{"kid": "kid-target", "kty": "RSA"}]}
    for name in NAMED:
        live_text = (layout.live / f"creds/{name}.jwt").read_text(encoding="utf-8")
        assert layout.new_tokens[name] in live_text
        assert stat.S_IMODE((layout.live / f"creds/{name}.jwt").stat().st_mode) == 0o600
    for index in range(8):
        wrapper = layout.live / "seats" / f"{index:02d}" / "bin/board.sh"
        assert TARGET in wrapper.read_text(encoding="utf-8")
    assert "stop-consumers" in result["pending_operator_steps"]
    assert "start-central" in result["pending_operator_steps"]

    entries = cut.journal_load(config.journal_path)
    states = [(entry.step_id, entry.state) for entry in entries]
    for step_id in cut.TOOLKIT_ACTIVATION_STEPS:
        assert (step_id, "started") in states
        assert (step_id, "done") in states
    assert cut.interrupted_steps(entries) == []
    assert not (layout.staging / "staged-configs/launch-central.sh.tmp-1").exists()
    leftovers = [p for p in layout.live.rglob("*.tmp-*")]
    assert leftovers == []


def test_activate_refuses_unknown_rehearsal_step(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    with pytest.raises(cut.GateFailure, match="not a toolkit activation step"):
        cut.run_activate(config, evidence, True, rehearse_fail_at="start-central")


def test_interrupted_activation_is_detected_and_repaired_in_reverse_order(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    original = {swap.identifier: (layout.live / _relative(layout.live, swap.live_path)).read_bytes()
                for swap in config.swaps()}

    with pytest.raises(cut.GateFailure, match="rehearsal interruption"):
        cut.run_activate(config, evidence, True, rehearse_fail_at="swap-configs")

    entries = cut.journal_load(config.journal_path)
    assert cut.interrupted_steps(entries) == ["swap-configs"]
    # The interrupted activation is visible: launcher and JWKS already moved.
    assert "kid-target" in (layout.live / "jwks/jwks.json").read_text(encoding="utf-8")
    # A forward dry run must refuse until the partial activation is repaired.
    findings, _extra, _evidence = cut.run_dry_run(config)
    assert not cut.findings_ok(findings)
    assert any("interrupted" in f.detail for f in findings if not f.ok)

    result = cut.run_rollback(config, True)
    step_order = [item["step_id"] for item in result["restored"]]
    assert step_order[0] == "swap-configs"
    assert step_order[-1] == "swap-launcher"
    assert result["coherence_ok"] is True
    assert "rollback-restart" in result["pending_operator_steps"]

    for swap in config.swaps():
        if swap.step_id in ("swap-launcher", "swap-jwks", "swap-credentials", "swap-configs"):
            assert swap.live_path.read_bytes() == original[swap.identifier]
    assert "kid-old" in (layout.live / "jwks/jwks.json").read_text(encoding="utf-8")
    for name in NAMED:
        text = (layout.live / f"creds/{name}.jwt").read_text(encoding="utf-8")
        assert layout.old_tokens[name] in text
        assert layout.new_tokens[name] not in text
    assert cut.interrupted_steps(cut.journal_load(config.journal_path)) == []
    # Seats were never reached, so they stay untouched.
    assert PREVIOUS in (layout.live / "seats/00/bin/board.sh").read_text(encoding="utf-8")


def test_rollback_refuses_out_of_root_journal_target_before_any_write(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    outside = tmp_path / "outside-rollback-target"
    original = b"outside rollback bytes remain unchanged\n"
    outside.write_bytes(original)
    journal = json.loads(config.journal_path.read_text(encoding="utf-8"))
    for entry in journal["entries"]:
        if entry["step_id"] == "swap-launcher":
            entry["targets"][0]["live"] = str(outside)
    cut.write_json_atomic(config.journal_path, journal)
    live_before = {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    }
    journal_before = config.journal_path.read_bytes()

    with pytest.raises(cut.GateFailure, match="differs from the configured target"):
        cut.run_rollback(config, True)
    assert outside.read_bytes() == original
    assert {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    } == live_before
    assert config.journal_path.read_bytes() == journal_before


def test_rollback_refuses_missing_target_in_every_step_entry_before_any_write(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)

    missing = "credentials/dashboard-admin.jwt"
    journal = json.loads(config.journal_path.read_text(encoding="utf-8"))
    changed = 0
    for entry in journal["entries"]:
        if entry["step_id"] == "swap-credentials":
            before = len(entry["targets"])
            entry["targets"] = [
                target for target in entry["targets"]
                if target.get("identifier") != missing
            ]
            changed += before - len(entry["targets"])
    assert changed == 2
    cut.write_json_atomic(config.journal_path, journal)
    live_before = {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    }
    journal_before = config.journal_path.read_bytes()

    with pytest.raises(cut.GateFailure, match="missing configured targets"):
        cut.run_rollback(config, True)

    assert {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    } == live_before
    assert config.journal_path.read_bytes() == journal_before


def _rollback_bytes(config):
    swaps = config.swaps()
    assert len(swaps) == 43
    return (
        {swap.identifier: swap.live_path.read_bytes() for swap in swaps},
        config.journal_path.read_bytes(),
    )


def _assert_rollback_state_unchanged(config, live_before, journal_before):
    assert {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    } == live_before
    assert config.journal_path.read_bytes() == journal_before


@pytest.mark.parametrize(
    "missing_step",
    ("swap-launcher", "swap-credentials", "swap-seats"),
)
def test_rollback_refuses_missing_whole_activation_step_before_any_write(
    tmp_path, missing_step
):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    journal = json.loads(config.journal_path.read_text(encoding="utf-8"))
    journal["entries"] = [
        entry for entry in journal["entries"]
        if entry["step_id"] != missing_step
    ]
    cut.write_json_atomic(config.journal_path, journal)
    live_before, journal_before = _rollback_bytes(config)

    with pytest.raises(
        cut.GateFailure,
        match="missing or reordered|absent activation step",
    ):
        cut.run_rollback(config, True)

    _assert_rollback_state_unchanged(config, live_before, journal_before)


@pytest.mark.parametrize(
    ("corruption", "error"),
    (
        ("duplicate-state", "invalid state progression"),
        ("reordered", "invalid state progression|interleaved or reordered"),
        ("malformed-state", "invalid state"),
        ("advance-after-incomplete", "advances past incomplete"),
    ),
)
def test_rollback_refuses_invalid_step_progression_before_any_write(
    tmp_path, corruption, error
):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    journal = json.loads(config.journal_path.read_text(encoding="utf-8"))
    entries = journal["entries"]
    if corruption == "duplicate-state":
        entries.insert(1, dict(entries[0]))
    elif corruption == "reordered":
        entries[0], entries[1] = entries[1], entries[0]
    elif corruption == "malformed-state":
        entries[0]["state"] = "complete"
    else:
        entries[:] = [
            entry for entry in entries
            if not (
                entry["step_id"] == "swap-jwks"
                and entry["state"] == "done"
            )
        ]
    cut.write_json_atomic(config.journal_path, journal)
    live_before, journal_before = _rollback_bytes(config)

    with pytest.raises(cut.GateFailure, match=error):
        cut.run_rollback(config, True)

    _assert_rollback_state_unchanged(config, live_before, journal_before)


def test_rollback_refuses_drift_when_whole_journal_is_absent(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    cut.write_json_atomic(config.journal_path, {"entries": []})
    live_before, journal_before = _rollback_bytes(config)

    with pytest.raises(cut.GateFailure, match="absent activation step"):
        cut.run_rollback(config, True)

    _assert_rollback_state_unchanged(config, live_before, journal_before)


@pytest.mark.parametrize(
    ("corruption", "error"),
    [
        ("duplicate", "duplicate target identifiers"),
        ("extra", "unconfigured targets"),
        ("missing-hash", "pre-activation hash"),
        ("wrong-backup-ref", "backup reference"),
    ],
)
def test_rollback_refuses_malformed_target_sets_before_any_write(
    tmp_path, corruption, error
):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    journal = json.loads(config.journal_path.read_text(encoding="utf-8"))
    entry = next(
        item for item in journal["entries"]
        if item["step_id"] == "swap-credentials" and item["state"] == "done"
    )
    if corruption == "duplicate":
        entry["targets"].append(dict(entry["targets"][0]))
    elif corruption == "extra":
        target = dict(entry["targets"][0])
        target["identifier"] = "credentials/unconfigured.jwt"
        entry["targets"].append(target)
    elif corruption == "missing-hash":
        entry["targets"][0].pop("pre_sha256")
    else:
        entry["targets"][0]["backup_ref"] = "credentials/other.jwt"
    cut.write_json_atomic(config.journal_path, journal)
    live_before = {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    }
    journal_before = config.journal_path.read_bytes()

    with pytest.raises(cut.GateFailure, match=error):
        cut.run_rollback(config, True)

    assert {
        swap.identifier: swap.live_path.read_bytes() for swap in config.swaps()
    } == live_before
    assert config.journal_path.read_bytes() == journal_before


def _relative(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def test_rollback_refuses_mixed_token_and_jwks_restore(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)

    # Simulate the classic partial restore: old JWKS back, new tokens in place.
    _write(layout.live / "jwks/jwks.json",
           json.dumps({"keys": [{"kid": "kid-old", "kty": "RSA"}]}))
    findings = cut.rollback_coherence_findings(config)
    assert not cut.findings_ok(findings)
    details = " ".join(f.detail for f in findings if not f.ok)
    assert "JWKS" in details or "issuer" in details or "aud" in details

    # Restoring the matching credential set makes the pair coherent again.
    for name in NAMED:
        _write(layout.live / f"creds/{name}.jwt", layout.old_tokens[name] + "\n")
    assert cut.findings_ok(cut.rollback_coherence_findings(config))


def test_rollback_refuses_incoherent_backup_before_any_live_write(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)

    live_before = {
        swap.identifier: swap.live_path.read_bytes()
        for swap in config.swaps() if swap.live_path.is_file()
    }
    journal_before = config.journal_path.read_bytes()
    backup_jwks = config.backup_root / "jwks/jwks.json"
    _write(backup_jwks, json.dumps({"keys": [{"kid": "kid-wrong", "kty": "RSA"}]}))
    manifest = json.loads(config.backup_manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact["rel_path"] == "jwks/jwks.json":
            artifact["sha256"] = cut.sha256_file(backup_jwks)
            artifact["size"] = backup_jwks.stat().st_size
    cut.write_json_atomic(config.backup_manifest_path, manifest)

    with pytest.raises(cut.GateFailure, match="JWKS|signing keys"):
        cut.run_rollback(config, True)

    assert {
        swap.identifier: swap.live_path.read_bytes()
        for swap in config.swaps() if swap.live_path.is_file()
    } == live_before
    assert config.journal_path.read_bytes() == journal_before


def test_rollback_after_full_activation_restores_the_previous_binding(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    result = cut.run_rollback(config, True)
    assert result["coherence_ok"] is True
    assert len(result["restored"]) == len(config.swaps())
    assert (layout.live / "launch/profile.env").read_text(encoding="utf-8").startswith(
        "CENTRAL_JWT_ISSUER=https://127.0.0.1:8766")
    for index in range(8):
        wrapper = layout.live / "seats" / f"{index:02d}" / "bin/board.sh"
        assert PREVIOUS in wrapper.read_text(encoding="utf-8")
    assert cut.interrupted_steps(cut.journal_load(config.journal_path)) == []


def test_rollback_reports_post_restore_incoherence_as_failure(tmp_path, monkeypatch):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    real_findings = cut.rollback_coherence_findings

    def findings(config_value, *, from_backup=False):
        if from_backup:
            return real_findings(config_value, from_backup=True)
        return [cut.Finding("rollback_coherence", False, "post-restore mismatch")]

    monkeypatch.setattr(cut, "rollback_coherence_findings", findings)

    with pytest.raises(cut.GateFailure, match="post-restore mismatch"):
        cut.run_rollback(config, True)


def test_rollback_refuses_when_backup_integrity_fails(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    with pytest.raises(cut.GateFailure, match="rehearsal interruption"):
        cut.run_activate(config, evidence, True, rehearse_fail_at="swap-seats")
    (config.backup_root / "jwks/jwks.json").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(cut.GateFailure, match="integrity"):
        cut.run_rollback(config, True)


def test_rollback_requires_confirmation_and_is_a_noop_without_journal(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    with pytest.raises(cut.GateFailure, match="operator confirmation"):
        cut.run_rollback(config, False)
    result = cut.run_rollback(config, True)
    assert result["restored"] == []
    assert "no toolkit activation" in result["detail"]


def test_rollback_refuses_and_restores_nothing_when_a_backup_member_is_missing(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    evidence = _dry_run_ok(config)
    cut.run_activate(config, evidence, True)
    assert "kid-target" in (layout.live / "jwks/jwks.json").read_text(encoding="utf-8")

    (config.backup_root / "configs/coordinator.env").unlink()
    with pytest.raises(cut.GateFailure, match="integrity|missing"):
        cut.run_rollback(config, True)

    # Fail closed: nothing was restored, so the operator still sees one coherent state.
    assert "kid-target" in (layout.live / "jwks/jwks.json").read_text(encoding="utf-8")
    entries = cut.journal_load(config.journal_path)
    assert all(entry.state in ("started", "done") for entry in entries)
    assert cut.steps_needing_rollback(entries) == list(reversed(cut.TOOLKIT_ACTIVATION_STEPS))


# ---------------------------------------------------------------------------
# Snapshots, reports and CLI
# ---------------------------------------------------------------------------


def test_snapshot_boards_normalizes_and_refuses_bad_input(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    source = tmp_path / "board-dump.json"
    cut.write_json_atomic(source, {
        "recorded_at": _iso(), "dispatch_paused": True, "teams_paused": True,
        "boards": {board: {"claimed": 0, "open": 2} for board in cut.DEFAULT_ACTIVE_BOARDS}})
    out = layout.staging / "cutover-evidence/snapshot.json"
    result = cut.snapshot_boards(config, source, out)
    assert result["missing_active_boards"] == []
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["boards"]["pursers"]["submitted"] == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600

    cut.write_json_atomic(source, {"boards": {}})
    result = cut.snapshot_boards(config, source, out)
    assert set(result["missing_active_boards"]) == set(cut.DEFAULT_ACTIVE_BOARDS)

    cut.write_json_atomic(source, {"nope": True})
    with pytest.raises(cut.ConfigError):
        cut.snapshot_boards(config, source, out)


def test_snapshot_memberships_requires_a_bound_url(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    source = tmp_path / "members.json"
    cut.write_json_atomic(source, {"url": TARGET, "boards": {
        board: [{"principal_id": f"PR-{board}", "role": "admin"}]
        for board in cut.DEFAULT_ACTIVE_BOARDS}})
    out = layout.staging / "cutover-evidence/members-snapshot.json"
    result = cut.snapshot_memberships(config, source, out)
    assert result["url"] == TARGET

    cut.write_json_atomic(source, {"url": "not-a-url", "boards": {}})
    with pytest.raises(cut.GateFailure):
        cut.snapshot_memberships(config, source, out)


def test_report_redacts_credential_shaped_keys(tmp_path):
    layout = build_layout(tmp_path)
    config = _prepare_and_backup(layout)
    report = cut.build_report("dry-run", config, [
        cut.Finding("credentials", True, "ok", {"token": layout.new_tokens["dashboard-admin"],
                                                "door": "synthetic-door-bundle"})])
    blob = json.dumps(report)
    assert layout.new_tokens["dashboard-admin"] not in blob
    assert "synthetic-door-bundle" not in blob
    assert cut.REDACTED in blob


def test_cli_end_to_end_prepare_backup_dry_run_activate_rollback(tmp_path, capsys):
    layout = build_layout(tmp_path)
    config_path = str(layout.config_path)
    assert cut.main(["template"]) == 0
    assert cut.main(["plan"]) == 0
    assert cut.main(["--config", config_path, "prepare"]) == 0
    assert cut.main(["--config", config_path, "backup"]) == 0
    assert cut.main(["--config", config_path, "verify-artifacts"]) == 0
    assert cut.main(["--config", config_path, "dry-run",
                     "--evidence-out", str(layout.staging / "cutover-evidence/dry.json")]) == 0
    evidence = str(layout.staging / "cutover-evidence/dry.json")
    assert cut.main(["--config", config_path, "activate", "--evidence", evidence]) == 1
    assert cut.main(["--config", config_path, "activate", "--operator-confirmed",
                     "--evidence", evidence]) == 0
    assert cut.main(["--config", config_path, "rollback", "--operator-confirmed"]) == 0
    assert cut.main(["--config", config_path, "command-sheet", "--phase", "activate"]) == 0
    out = capsys.readouterr().out
    assert str(layout.live) not in out
    assert str(layout.staging) not in out
    assert "/PATH/TO/" in out


def test_cli_reports_gate_refusals_with_exit_code_one(tmp_path, capsys):
    layout = build_layout(tmp_path)
    config_path = str(layout.config_path)
    cut.main(["--config", config_path, "prepare"])
    cut.main(["--config", config_path, "backup"])
    snapshot = layout.staging / "cutover-evidence/board-snapshot.json"
    record = json.loads(snapshot.read_text(encoding="utf-8"))
    record["boards"]["pursers"]["reviewing"] = 1
    record["recorded_at"] = _iso()
    cut.write_json_atomic(snapshot, record)
    capsys.readouterr()
    assert cut.main(["--config", config_path, "dry-run"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any("reviewing" in json.dumps(item) or "holds 1 ticket" in item["detail"]
               for item in payload["refusals"])


def test_cli_requires_config_and_reports_config_errors(tmp_path, capsys):
    assert cut.main(["dry-run"]) == 2
    layout = build_layout(tmp_path)
    layout.config_path.write_text("{not json", encoding="utf-8")
    assert cut.main(["--config", str(layout.config_path), "dry-run"]) == 2
    assert cut.main(["--config", str(tmp_path / "absent.json"), "dry-run"]) == 2


def test_cli_dry_run_refuses_to_touch_live_state_without_backup(tmp_path):
    layout = build_layout(tmp_path)
    config = layout.load()
    cut.run_prepare(config)
    before = cut.live_state_fingerprint(config)
    assert cut.main(["--config", str(layout.config_path), "dry-run"]) == 1
    assert cut.live_state_fingerprint(config) == before


def test_module_runs_as_a_script():
    module = Path(cut.__file__).resolve()
    proc = subprocess.run([sys.executable, str(module), "plan"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["plan_hash"] == cut.plan_hash()
    template = subprocess.run([sys.executable, str(module), "template"],
                              capture_output=True, text=True)
    assert template.returncode == 0, template.stderr
    assert json.loads(template.stdout)["target_url"] == TARGET
