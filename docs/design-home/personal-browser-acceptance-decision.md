# Personal browser acceptance route decision

Status: approved by coordinator decision `AN-000000000908`.

## Decision

The 79 Personal acceptance rows are MCP App host acceptance, not a top-level
AionUi route. They use the explicit evidence surface ID `mcp-app`. The observer
opens the verifier-supplied AionUi conversation page and selects the one child
frame that matches the shipped Personal MCP App shell. It reads browser
predicates and the sandbox board inside that frame while retaining signed host,
candidate-manifest, pinned dashboard bytes, live Personal process, and HMAC
attestation checks.

No static Personal route is added to AionUi. A row that cannot be observed on
this real surface is a catalogue boundary with an explicit reason and is
blocked before capture; it can never be reported as passed.

## What the catalogue assumes

`docs/design-home/context/acceptance-facts.json` contains exactly 79 inventory
rows whose surface was `personal`: 9 `dashboard-ui.*` rows, 7 `personal-mcp.*`
rows, and 63 `personal.*` rows. The harness maps all three prefixes to the
`mcp-app` surface and assigns the `pinned-signed-aionui-personal-mcp` trust
adapter. The catalogue, typed predicate delta, surface manifest, report schema,
and generated capture commands use the same surface ID.

That adapter currently requires one browser page to satisfy all of these
conditions:

1. The host page is a top-level page on the signed AionUi origin and contains
   exactly one matching embedded Personal MCP App frame.
2. `candidate.json`, fetched from the same-origin `candidate_manifest_url` in
   the isolated host context, identifies the exact installed extension commit.
3. DOM selectors inside the App frame expose the selected sandbox board and
   every visual or transition predicate.
4. Fetching the App frame document returns bytes identical to
   `packages/personal/src/pursers_personal/resources/dashboard.html`.
5. A live `python -m pursers_personal.cli mcp` process matches the pinned source,
   commit, sandbox board, PID, receipt, and verifier-owned challenge key.
6. The captured accessibility tree contains the HMAC-signed response from the
   live Personal stdio transport.

The observer keeps the host and App execution contexts separate. Host status
and candidate identity come from an isolated main-frame world; board identity,
page bytes, accessibility nodes, and transition actions come from an isolated
world in the matched App frame. The full host accessibility tree remains in
the evidence so the live attestation delivered through the conversation can be
verified.

## What the product ships

The AionUi extension contributes one Settings tab at `webui/index.html` and four
assistant presets. Its deterministic archive writes `webui/candidate.json`
beside the Settings assets. It does not contribute a Personal browser page or a
Personal MCP server.

The Personal dashboard is instead registered by the live Personal server as the
MCP Apps resource `ui://pursers/dashboard`. Its HTML uses the MCP Apps
postMessage transport to communicate with its parent host and obtain live
`board_snapshot`, `fleet_snapshot`, `link_snapshot`, and event-feed results.
The acceptance attestation is also a tool on that same live stdio server.

The AionUi 2.2.1 integration documentation records two relevant host limits:
the isolated AionCore listener is API-only, and AionUi does not inject
extension-declared MCP servers into conversations. The extension therefore has
no supported mechanism that can expose the live Personal MCP App as a top-level
page on the signed AionUi origin.

## Why a static route is not a fix

Adding a second copy of `dashboard.html` to the extension archive would make a
same-origin URL and relative `candidate.json` possible, but it would not connect
that page to the Personal stdio server. The page would use its deliberately
synthetic standalone fallback. It could neither consume the required live
product responses nor carry the live HMAC attestation. Treating that page as a
pass would weaken the existing trust boundary and mislabel synthetic data as
product evidence.

## Catalogue boundary

- All 79 rows remain assigned to the Personal product but carry
  `surface=mcp-app`.
- The page URL supplied for each row is the real host conversation page with
  the Personal App open; it is not a fabricated `/personal/` route.
- The frame match fails closed when absent or ambiguous.
- An optional fact-level `catalogue_boundary` object has the closed shape
  `{"kind":"unobservable_on_real_surface","reason":"..."}`. The runner blocks
  such a row during plan preparation and the harness rejects any passed
  evidence for it.
- Fixture, package, unit, and typed checks are non-final preflight evidence.
- Expected predicate values are never converted into fabricated observations
  or inferred passes.

## Worker-owned browser dry-run

The deterministic preflight uses a disposable loopback host page with the
tracked `dashboard.html` loaded as a sandboxed child frame. The child uses its
bundled MCP Apps postMessage transport; the parent implements the host side of
the `ui/initialize` handshake and answers the four read-only Personal tools
from synthetic sandbox fixtures. The browser observer still operates through
its generated `EGO_SCRIPT` and `EGO_TRANSITION_SCRIPT`; the fixture does not
replace or emulate their frame-selection logic.

Run the preflight with a dedicated Ego Lite task space:

```sh
python3 tools/aionui-extension/tests/home_acceptance/mcp_app_frame_dry_run.py \
  --ego-browser /PATH/TO/ego-browser \
  --task-space pursers-personal-mcp-app-dry-run
```

The command requires a positive capture and an in-frame tab transition. It
also requires closed failures for an absent frame, two matching frames, altered
dashboard bytes, a candidate manifest on a different origin, and a dashboard
whose selected sandbox board differs from the pinned target. The reported
`page_sha256` is calculated in the real child frame by fetching its actual
resource URL and must equal the tracked `dashboard.html` digest. Output is
labelled `NOT final`; it is worker preflight evidence, not reviewer-owned
acceptance.
