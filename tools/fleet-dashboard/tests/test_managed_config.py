from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Self

import pytest


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("managed_config_dashboard", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


PRINCIPAL_A = "PR-" + "a" * 64
PRINCIPAL_B = "PR-" + "b" * 64


class ManagedClient:
    def __init__(self) -> None:
        self.review_policy = "strict"
        self.stale_after_days = 3
        self.members = {PRINCIPAL_A: "admin"}
        self.fail_stale = False
        self.membership_behavior = "normal"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def board_state_get(self, key: str | None = None) -> dict:
        if key == "project_registry":
            value = {
                "schema_version": 1,
                "projects": {
                    "Pursers": {
                        "board_id": "pursers",
                        "status": "active",
                        "work_dir": "/PATH/TO/PURSERS",
                    }
                },
            }
        elif key == "coordinator_config":
            value = {
                "board_butler": {
                    "schema_version": 1,
                    "global": {
                        "mode": "shadow",
                        "classification": {
                            "model": "bounded-model",
                            "endpoint_ref": "/PRIVATE/endpoint",
                            "key_ref": "/PRIVATE/key",
                            "extra_headers": {"X-Trace": "private-trace"},
                        },
                    },
                }
            }
        elif key == "coordinator_findings":
            value = {"effective_config": {}, "config_sources": {}}
        else:
            value = {}
        return {"state": {"value": json.dumps(value)}}

    async def board_list(self) -> dict:
        return {
            "boards": [{"board_id": "pursers", "membership_role": "admin"}]
        }

    async def board_status(self) -> dict:
        return {
            "board_id": "pursers",
            "review_policy": self.review_policy,
            "stale_after_days": self.stale_after_days,
        }

    async def board_members(self) -> dict:
        return {
            "members": [
                {
                    "principal_id": principal,
                    "role": role,
                    "source": "fixture",
                    "agent_names": ["seat"],
                }
                for principal, role in self.members.items()
            ]
        }

    async def board_review_policy_set(self, value: str) -> dict:
        self.review_policy = value
        return {"ok": True}

    async def board_stale_after_set(self, value: int) -> dict:
        if self.fail_stale:
            raise RuntimeError("fixture stale setter failure")
        self.stale_after_days = value
        return {"ok": True}

    async def board_member_add(self, principal_id: str, role: str) -> dict:
        if self.membership_behavior != "noop":
            self.members[principal_id] = (
                "member" if self.membership_behavior == "mismatch" else role
            )
            if self.membership_behavior == "mismatch":
                self.membership_behavior = "normal"
        return {"ok": True}

    async def board_member_remove(self, principal_id: str) -> dict:
        if self.membership_behavior == "mismatch":
            self.members[principal_id] = "reviewer"
            self.membership_behavior = "normal"
        elif self.membership_behavior != "noop":
            self.members.pop(principal_id, None)
        return {"ok": True}

    async def board_member_set_role(self, principal_id: str, role: str) -> dict:
        if self.membership_behavior != "noop":
            self.members[principal_id] = (
                "member" if self.membership_behavior == "mismatch" else role
            )
            if self.membership_behavior == "mismatch":
                self.membership_behavior = "normal"
        return {"ok": True}


def make_fetcher(client: ManagedClient) -> dashboard.FleetFetcher:
    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="token",
        home_board="pursers",
        agent_name="dashboard-seat",
        stale_seconds=300,
        cache_seconds=5,
    )
    return dashboard.FleetFetcher(config, client_factory=lambda *_a, **_k: client)


def test_managed_contract_is_typed_and_redacts_references() -> None:
    client = ManagedClient()
    fetcher = make_fetcher(client)
    result = asyncio.run(fetcher.fetch_managed_configuration("pursers"))

    assert result["schema"] == "fleet_managed_config_v1"
    effective = result["families"]["board_butler"]["effective"]
    provider = effective["classification"]
    assert provider["model"] == "bounded-model"
    assert provider["endpoint_configured"] is True
    assert provider["key_configured"] is True
    encoded = json.dumps(result)
    assert "/PRIVATE/" not in encoded
    assert "private-trace" not in encoded
    assert effective["mode"] == "shadow"
    assert effective["provenance"]["mode"] == "global"
    assert effective["inheritance"]["precedence"] == [
        "safe_defaults",
        "global",
        "project",
        "board",
    ]
    assert result["families"]["source_connectors"]["dependency_ticket"] == (
        "TK-dcc6d183eb1e15126e1f"
    )
    assert result["families"]["central_retention"]["dependency_ticket"] == (
        "TK-eebab5f77b7a6b63eb37"
    )


def test_board_policy_plan_apply_reads_back_and_rejects_stale_source() -> None:
    client = ManagedClient()
    fetcher = make_fetcher(client)

    async def scenario() -> None:
        initial = await fetcher.fetch_managed_configuration("pursers")
        expected = initial["families"]["board_policy"]["expected_sha256"]
        plan = await fetcher.prepare_managed_configuration(
            {
                "board_id": "pursers",
                "family": "board_policy",
                "expected_sha256": expected,
                "changes": {"review_policy": "workflow", "stale_after_days": 7},
            }
        )
        receipt = await fetcher.apply_managed_configuration(
            plan["plan_id"], plan["digest"]
        )
        assert receipt["readback"]["values"] == {
            "review_policy": "workflow",
            "stale_after_days": 7,
        }
        with pytest.raises(dashboard.ConfigConflictError, match="reload"):
            await fetcher.prepare_managed_configuration(
                {
                    "board_id": "pursers",
                    "family": "board_policy",
                    "expected_sha256": expected,
                    "changes": {"review_policy": "strict"},
                }
            )

    asyncio.run(scenario())


def test_board_policy_failed_apply_rolls_back_prior_field() -> None:
    client = ManagedClient()
    fetcher = make_fetcher(client)

    async def scenario() -> None:
        current, _members = await fetcher._managed_board_state("pursers")
        plan = await fetcher.prepare_managed_configuration(
            {
                "board_id": "pursers",
                "family": "board_policy",
                "expected_sha256": current["expected_sha256"],
                "changes": {"review_policy": "workflow", "stale_after_days": 9},
            }
        )
        client.fail_stale = True
        with pytest.raises(RuntimeError, match="rollback succeeded"):
            await fetcher.apply_managed_configuration(plan["plan_id"], plan["digest"])
        assert client.review_policy == "strict"
        assert client.stale_after_days == 3

    asyncio.run(scenario())


def test_membership_plan_apply_uses_exact_principal_and_readback() -> None:
    client = ManagedClient()
    fetcher = make_fetcher(client)

    async def scenario() -> None:
        _policy, members = await fetcher._managed_board_state("pursers")
        plan = await fetcher.prepare_managed_configuration(
            {
                "board_id": "pursers",
                "family": "membership",
                "expected_sha256": members["expected_sha256"],
                "change": {
                    "operation": "add",
                    "principal_id": PRINCIPAL_B,
                    "role": "reviewer",
                },
            }
        )
        receipt = await fetcher.apply_managed_configuration(
            plan["plan_id"], plan["digest"]
        )
        assert {row["principal_id"]: row["role"] for row in receipt["readback"]["members"]} == {
            PRINCIPAL_A: "admin",
            PRINCIPAL_B: "reviewer",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("operation", "principal_id", "role"),
    [
        ("add", PRINCIPAL_B, "reviewer"),
        ("remove", PRINCIPAL_A, None),
        ("set_role", PRINCIPAL_A, "reviewer"),
    ],
)
@pytest.mark.parametrize("behavior", ["noop", "mismatch"])
def test_membership_apply_rejects_bad_readback_and_restores_before_state(
    operation: str, principal_id: str, role: str | None, behavior: str
) -> None:
    client = ManagedClient()
    fetcher = make_fetcher(client)

    async def scenario() -> None:
        _policy, members = await fetcher._managed_board_state("pursers")
        change = {"operation": operation, "principal_id": principal_id}
        if role is not None:
            change["role"] = role
        plan = await fetcher.prepare_managed_configuration(
            {
                "board_id": "pursers",
                "family": "membership",
                "expected_sha256": members["expected_sha256"],
                "change": change,
            }
        )
        before = dict(client.members)
        client.membership_behavior = behavior
        with pytest.raises(RuntimeError, match="read-back did not match"):
            await fetcher.apply_managed_configuration(plan["plan_id"], plan["digest"])
        assert client.members == before

    asyncio.run(scenario())


def test_board_butler_effective_policy_resolves_precedence_and_provenance() -> None:
    client = ManagedClient()

    async def layered_state(key: str | None = None) -> dict:
        result = await ManagedClient.board_state_get(client, key)
        if key != "coordinator_config":
            return result
        document = json.loads(result["state"]["value"])
        document["board_butler"]["global"].update(
            {"mode": "shadow", "ceilings": {"per_hour": 4, "per_ticket": 2}}
        )
        document["board_butler"]["projects"] = {
            "Pursers": {"ceilings": {"per_hour": 6}, "hold_before_post_s": 60}
        }
        document["board_butler"]["boards"] = {
            "pursers": {"mode": "active", "ceilings": {"per_ticket": 3}}
        }
        return {"state": {"value": json.dumps(document)}}

    client.board_state_get = layered_state  # type: ignore[method-assign]
    result = asyncio.run(make_fetcher(client).fetch_managed_configuration("pursers"))
    effective = result["families"]["board_butler"]["effective"]
    assert effective["mode"] == "active"
    assert effective["ceilings"] == {
        "per_hour": 6,
        "per_ticket": 3,
        "per_board": 20,
    }
    assert effective["provenance"]["mode"] == "board:pursers"
    assert effective["provenance"]["ceilings"] == {
        "per_hour": "project:Pursers",
        "per_ticket": "board:pursers",
        "per_board": "safe_defaults",
    }
    assert effective["inheritance"]["applied_layers"] == [
        "safe_defaults",
        "global",
        "project:Pursers",
        "board:pursers",
    ]
    assert "Remove an override field" in effective["inheritance"]["reset_semantics"]


def test_board_butler_safe_defaults_match_authoritative_answer_classes() -> None:
    module_path = Path(__file__).parents[2] / "board-butler" / "board_butler.py"
    spec = importlib.util.spec_from_file_location(
        "managed_config_authoritative_board_butler", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    effective = asyncio.run(
        make_fetcher(ManagedClient()).fetch_managed_configuration("pursers")
    )["families"]["board_butler"]["effective"]
    assert set(effective["answer_scope"]) == set(module.ANSWER_CLASSES)
    assert effective["answer_scope"]["approved_merge"] == "escalate"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "board_id": "pursers",
            "family": "board_policy",
            "expected_sha256": "0" * 64,
            "changes": {"archive_after_days": 2},
        },
        {
            "board_id": "pursers",
            "family": "membership",
            "expected_sha256": "0" * 64,
            "change": {"operation": "remove", "principal_id": "PR-short"},
        },
    ],
)
def test_managed_plans_reject_unknown_or_unbounded_fields(payload: dict) -> None:
    fetcher = make_fetcher(ManagedClient())
    with pytest.raises((ValueError, dashboard.ConfigConflictError)):
        asyncio.run(fetcher.prepare_managed_configuration(payload))


def test_managed_configuration_http_routes_are_guarded_and_registered() -> None:
    calls: list[tuple[str, object]] = []

    class Cache:
        def resolve_central(self, value: str | None = None) -> str:
            return value or "default"

        def get_managed_configuration(
            self, board_id: str, central: str | None = None
        ) -> dict:
            calls.append(("get", board_id))
            return {"schema": "fleet_managed_config_v1", "board_id": board_id}

        def plan_managed_configuration(
            self, payload: object, central: str | None = None
        ) -> dict:
            calls.append(("plan", payload))
            return {"plan_id": "a" * 32, "digest": "b" * 64}

        def apply_managed_configuration(
            self, plan_id: str, digest: str, central: str | None = None
        ) -> dict:
            calls.append(("apply", {"plan_id": plan_id, "digest": digest}))
            return {"ok": True, "plan_id": plan_id}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: object) -> dict:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    try:
        with urllib.request.urlopen(base + "/api/config/managed?board_id=pursers") as response:
            assert json.load(response)["schema"] == "fleet_managed_config_v1"
        plan = post(
            "/api/config/managed/plan",
            {
                "board_id": "pursers",
                "family": "board_policy",
                "expected_sha256": "0" * 64,
                "changes": {"review_policy": "strict"},
            },
        )
        receipt = post(
            "/api/config/managed/apply",
            {"plan_id": plan["plan_id"], "digest": plan["digest"]},
        )
        assert receipt["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert [name for name, _payload in calls] == ["get", "plan", "apply"]
