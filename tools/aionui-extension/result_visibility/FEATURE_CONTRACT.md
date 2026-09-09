# Result visibility feature contract

Ticket: `TK-12187737e73f`

## User-visible behavior

Pursers Home reads the selected board and shows each ticket's latest submitted
summary, bounded changed-file references, exact branch and commit when safely
parseable, and independent review outcome. Every row reports exactly one result
state: `missing`, `pending`, `approved`, `rejected`, or `failed`.

The feature is read-only. It cannot create, claim, renew, submit, cancel,
assign, or review a ticket. Missing results and backend failures are rendered
as explicit states rather than synthetic successes.

## Trust and bounds

- The authenticated Home helper pins the board; browser input cannot select a
  different board or Central.
- The helper reads only the loopback Fleet board-detail endpoint and forwards
  no Home token, door, cookie, or Central credential.
- Fleet projects only allow-listed, bounded result fields from persisted board
  state. Submission notes and review notes are never returned.
- Branch/commit and changed-file references must be safe relative identifiers;
  malformed values are omitted.
- Wrong origin or Home token fails before the result backend is called.

## File ownership

Feature-owned:

- `tools/aionui-extension/result_visibility/adapter.cjs`
- `tools/aionui-extension/result_visibility/FEATURE_CONTRACT.md`
- `tools/aionui-extension/tests/result_visibility.test.cjs`
- `tools/aionui-extension/tests/test_home_result_visibility.py`
- `tools/fleet-dashboard/result_visibility.py`
- `tools/fleet-dashboard/tests/test_result_visibility.py`

Shared integration points, changed only to register or render the feature:

- `tools/aionui-extension/build.py`
- `tools/aionui-extension/host/helper.cjs`
- `tools/aionui-extension/host/HELPER_CONTRACT.md`
- `tools/aionui-extension/webui/routes.js`
- `tools/aionui-extension/webui/index.html`
- `tools/aionui-extension/webui/app.js`
- `tools/aionui-extension/webui/style.css`
- `tools/aionui-extension/tests/test_package.py`
- `tools/fleet-dashboard/fleet_dashboard.py`

The integration owner resolves shared-file overlap after independent component
approval. This component does not modify the observer or acceptance harness.
