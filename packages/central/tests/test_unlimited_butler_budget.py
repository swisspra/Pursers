"""Explicit unlimited periodic budgets retain finite-envelope authority."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import jsonschema
import pytest

from test_butler_commands import config_v1, set_envelope_fingerprint
from pursers_central.butler_commands import validate_config, validate_config_authority

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "unlimited_budget_dashboard", ROOT / "tools/fleet-dashboard/butler_settings.py"
)
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)
FIELDS = ("max_tokens", "max_cost_microunits", "max_external_calls")


def unlimited_config():
    config = config_v1()
    for field in FIELDS:
        config["desired"]["budget"][field] = None
        config["envelope"]["max_budget"][field] = None
    set_envelope_fingerprint(config)
    return config


def save_dashboard(config):
    request = copy.deepcopy(config["desired"])
    request.update(board_id="pursers", mutation_id="unlimited-test", expected_revision=1)
    request["mode"] = "shadow"
    return dashboard.prepare_autonomous_butler_config(
        {"config": config, "revision": 1}, request
    )[0]


def test_unlimited_budget_schema_validator_and_shadow_save_round_trip():
    config = unlimited_config()
    schema = json.loads((ROOT / "docs/design/schemas/autonomous-butler-config-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(config)
    assert validate_config(config, "pursers") == config
    saved = save_dashboard(config)
    assert saved["desired"]["budget"] == config["desired"]["budget"]
    assert saved["envelope"] == config["envelope"]
    assert saved["enabled"] is False
    assert "authorization" not in saved


@pytest.mark.parametrize("field", FIELDS)
def test_unlimited_desired_requires_unlimited_envelope(field):
    config = config_v1()
    config["desired"]["budget"][field] = None
    with pytest.raises(ValueError, match="budget exceeds"):
        validate_config(config, "pursers")
    with pytest.raises(dashboard.ButlerSettingsError, match="budget exceeds"):
        save_dashboard(config)


@pytest.mark.parametrize("value", [0, 1, None])
def test_finite_and_zero_budgets_remain_distinct_under_unlimited_envelope(value):
    config = unlimited_config()
    for field in FIELDS:
        config["desired"]["budget"][field] = value
    assert validate_config(config, "pursers") == config
    assert save_dashboard(config)["desired"]["budget"] == config["desired"]["budget"]


def test_unlimited_envelope_requires_human_authority():
    before = config_v1()
    after = unlimited_config()
    after["revision"] = 2
    with pytest.raises(PermissionError):
        validate_config_authority(before, after, "a2a")
    assert validate_config_authority(before, after, "human")
