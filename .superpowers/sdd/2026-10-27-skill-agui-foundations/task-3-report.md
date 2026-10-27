# Task 3 report — Projects UX and finite observation consumer

## Status and source identity

Implementation is committed and ready for independent review. Focused Python and Node checks pass. The repository browser tests were collected but skipped because this run did not have the verifier-owned browser task-space environment; Luna3's actual browser verification remains pending.

- Task 3 base after the accepted generic Mac hotfix port: `fe3b962ba2606cd52ae810070d9007ae59de1cb6`
- Task 3 implementation commit / HEAD: `068fe970c815e0249ae8886efe9d4cc44a0b5b73`
- Branch: `codex/skill-agui-ux-20261027`
- The root-owned plan file remains unstaged and unchanged by this task.

The hotfix cherry-pick had one `CHANGELOG.md` conflict because both the accepted hotfix and the existing Task 1/2 work append Unreleased entries. I retained both sets of entries. No other cherry-pick conflicts occurred. The accepted hotfix remains in its own commit, `fe3b962ba2606cd52ae810070d9007ae59de1cb6`.

## Changes

- Added `tools/fleet-dashboard/ui/ag-ui-client.js`, a bounded client for finite `RUN_STARTED`, `STATE_SNAPSHOT`, optional `ACTIVITY_SNAPSHOT`, and `RUN_FINISHED` responses. It sends the empty read-only input shape, passes through an `AbortSignal`, validates run and project scope, rejects incomplete or mismatched responses, and makes no model or provider calls.
- Registered the script in the HTML shell and the packaged UI asset map. The Projects route consumes it on existing dashboard renders only; it adds no polling timer. Project refreshes abort old requests, and late responses must still match the current route and card scope. If the client or endpoint is unavailable, the card retains its already-loaded Fleet snapshot and labels that fallback.
- Reshaped Projects around project identity, work counts, and a `Add work` action. Technical IDs, ticket details, evidence references, and all reported state counts are inside keyboard-operable disclosures. Removal is under secondary actions and still uses the existing preview/confirmation flow.
- Added separate publication, final QA, and security fields. `integration_merged` is described as integration on the delivery branch and never as team-merge readiness. Held QA/security reasons appear as holds; missing evidence and unknown or omitted coverage remain unknown.
- Split confirmed empty, loading, and unavailable-source states. Recovery guidance offers retry and Settings; first-project guidance appears after every configured Central has returned a source-backed empty snapshot. Add-project and delivery-policy forms remain available in a closed setup disclosure and preserve the existing guarded lifecycle path.
- Added responsive styles, theme-token colors, visible focus and press feedback, reduced-motion behavior, and stable disclosure state keys. Updated the Fleet guide and Unreleased changelog.

No `app.js`, work/home view, runtime, configuration, service, board, VM, or generated manifest files were changed for Task 3. No model/provider request was added.

## Tests and verification

The focused observation tests first ran RED with the correct repository `PYTHONPATH`: three failures reproduced the missing client, false first-run message during source failure, and absent stale-scope cancellation. After implementation, the same tests passed. A separate setup-disclosure contract test also failed before adding the disclosure and passed after wiring the Add project controls to open it.

Final focused command, run with a Python 3.12 environment that has the repository test dependencies installed. `PURSERS_PYTHON312` names that interpreter; the private audit environment path is omitted from this repository report.

```sh
PYTHONPATH=packages/client/src:tools/fleet-dashboard "$PURSERS_PYTHON312" -m pytest -q tools/fleet-dashboard/tests/test_project_observation.py tools/fleet-dashboard/tests/test_delivery_view.py tools/fleet-dashboard/tests/test_home_view.py tools/fleet-dashboard/tests/test_work_view.py tools/fleet-dashboard/tests/test_route_css_assets.py tools/fleet-dashboard/tests/test_ag_ui.py tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_ui_shell_loads_packaged_assets_and_route_modules tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_projects_route_groups_centrals_and_preserves_board_actions_and_states tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_projects_route_styles_cover_responsive_and_density_modes tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_ui_assets_are_packaged_with_etag_revalidation tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_ui_asset_bytes_match_packaged_files tools/fleet-dashboard/tests/test_fleet_dashboard_browser.py
```

Result: `41 passed, 6 skipped in 11.23s`. The six skipped cases are existing browser-gated tests:

- `test_seats_route_renders_inventory_and_selector_contract`
- `test_delivery_shared_layer_editor_preserves_scope_and_does_not_copy_repository_values`
- `test_settings_scope_round_trips_exact_central_and_board_in_real_browser`
- `test_projects_route_loads_owned_css_in_real_browser`
- `test_autonomous_butler_browser_accessibility_and_conflict`
- `test_refresh_cycles_preserve_reader_state_at_desktop_and_mobile`

They require `ego-browser` on `PATH` and the verifier-owned `PURSERS_EGO_TASK_SPACE_ID`. The new observer, stale-scope, empty/loading/unavailable, readiness, and asset tests all ran and passed; none were skipped. The automated browser tests use synthetic local fixtures and do not contact or mutate a live Central. A safe serial invocation in the verifier's owned TaskSpace 1 is:

```sh
PURSERS_EGO_TASK_SPACE_ID=1 PYTHONPATH=packages/client/src:tools/fleet-dashboard "$PURSERS_PYTHON312" -m pytest -q tools/fleet-dashboard/tests/test_fleet_dashboard_browser.py
```

This does not replace Luna3's required desktop, light/dark, and 200% zoom inspection. No browser visual result is claimed here.

`node --check tools/fleet-dashboard/ui/ag-ui-client.js`, `node --check tools/fleet-dashboard/ui/views/projects.js`, and `git diff --check` exited successfully. The focused tests cover finite SSE validation, 503 and wrong-scope errors, abort propagation, stale project-scope response rejection, source failure versus loading versus confirmed empty, held QA/security, integration without QA evidence, unknown coverage, evidence disclosure, and asset registration. The dashboard typecheck and complete branch gate remain root-owned final integration checks.

## Self-review and remaining verification

The consumer treats `RUN_FINISHED` as transport completion only. It renders explicit QA/security holds from activity blockers and otherwise says `Unknown`; approval or `integration_merged` alone cannot produce a ready-for-team-merge label. When coverage counts are absent, the UI says coverage is unknown rather than deriving completeness from the visible array.

Luna3 still needs to exercise the temporary install/observe/project flow and inspect rendered desktop, 200% zoom, light, and dark states. Luna2's independent spec and code-quality review is also pending. No other implementation blocker is known.
