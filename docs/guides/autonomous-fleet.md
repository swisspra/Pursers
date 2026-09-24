# Autonomous fleet end-to-end case study

This case study is a deterministic, disposable proof of one board-owned AI
fleet. It exercises the product boundaries in process and at the MCP stdio
boundary; it does not install services, change an operator's live fleet, or
publish a release.

The checked fixture is
`tools/board-butler/tests/fixtures/autonomous_fleet_e2e_v1.json`. It defines a
synthetic `fleet-lab` registry project, three tickets at tiers 1, 2, and 3,
two workers, one reviewer, one ACP worker, and a hard ceiling of four role
agents plus two control-plane processes. Every seat uses a distinct principal
or template record. Templates are explicitly approved and registry-bound.

## Reproduce the proof

Run the focused acceptance test from a clean checkout. Keep temporary files
under an owner-controlled cache rather than a shared system directory.

```sh
mkdir -p /PATH/TO/OWNER-CACHE/tmp
TMPDIR=/PATH/TO/OWNER-CACHE/tmp \
TEMP=/PATH/TO/OWNER-CACHE/tmp \
TMP=/PATH/TO/OWNER-CACHE/tmp \
PYTHONPATH=packages/central/src:packages/client/src \
python3 -m pytest -q tools/board-butler/tests/test_autonomous_fleet_e2e.py
```

Before submission, run the repository manifest too:

```sh
TMPDIR=/PATH/TO/OWNER-CACHE/tmp \
TEMP=/PATH/TO/OWNER-CACHE/tmp \
TMP=/PATH/TO/OWNER-CACHE/tmp \
python3 tools/ci_manifest.py run
```

The test uses a real SQLite-backed Central server, real Butler policy and
reconciliation code, a signed `FleetExecutor` with a persistent operation
store, the production autonomous model runner, and a real MCP v2 stdio child
process. Only the external provider and host service-manager edges are
deterministic adapters. No expected-value fabrication is passed back as a
product response.

## Scenario and evidence

| Event | Product evidence |
| --- | --- |
| Mixed ticket arrival | Central creates and routes tier 1, 2, and 3 tickets to eligible worker identities. |
| Coordinator question | The Butler answers a ticket-status question from Central evidence. A request to publish and raise its own budget remains open with an escalation verdict. |
| Reviewer backlog | Reconciliation requests two workers, one reviewer, and one ACP worker while retaining the four-agent ceiling. |
| Provider failure | One model provider crashes with a private error; the normalized result is `provider_crash`, leaks no private detail, and a separate request still succeeds. An unavailable fleet provider is excluded from capacity. |
| MCP v2 task | A stdio connector exits after its first committed lookup. The runtime reconnects once, reuses the stable call ID, records one reservation, and redacts the resolved secret. |
| Process restart | A signed start commits before the client disconnects. A new executor instance opens the same SQLite store and returns the existing receipt as `replayed=true`; the host mutation occurs once. |
| Lease safety | A direct stop request for a seat with a live work lease is rejected with `live_lease`; the service adapter records no stop. |
| Idle scale-down | The first idle observation starts the 60-second grace period with no operation. After grace, three non-live seats drain and the live worker remains desired. |
| Independent review | A worker self-review fails authorization. A distinct reviewer principal approves all three submitted tickets and Central closes them. |
| Dashboard | The production projector reports `autonomous`, four role agents, two control-plane processes, agent ceiling 4, and total ceiling 6. |

The deterministic measurements are:

- Central lifecycle usage: 410 tokens across worker and independent reviewer
  records; creation records explicitly report zero rather than an estimate.
- Model boundary usage: 10 input tokens, 5 output tokens, 15 total tokens, and
  37 measured cost microunits against a 500-microunit limit.
- Healthy-provider observation: 12 ms. This is fixture input consumed by the
  reconciler, not a wall-clock benchmark.
- MCP recovery: two process calls, one stable call ID, one reservation, and
  one successful redacted audit result.
- Executor recovery: four requested starts, four unique published receipts,
  and one replayed receipt after restart without a duplicate start.

## Operator limits

The autonomous boundary is deliberately narrow. The Butler may answer only
configured evidence-backed questions and may scale only approved seat
templates inside immutable board, provider, host, and budget ceilings. It may
not approve its own budget, expand authorization, review its own work, merge,
push main, tag, publish, or change host service definitions. Those decisions
remain coordinator, reviewer, or operator actions.

Use an isolated registry project and owner-only credential paths for a live
trial. Verify the dashboard's config digest and process counts before enabling
active mode. Stop the trial by disabling autonomous mode and allowing live
leases to finish; do not terminate a seat that still holds work. Retain the
Central journal, executor receipts, model usage records, and connector audit
records for the independent reviewer.

The acceptance test scans returned failure and connector payloads for the
synthetic private values. A release check should additionally scan the exact
tip diff for credentials, local home paths, hostnames, and private endpoints.
