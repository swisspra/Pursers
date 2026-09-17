# Unlanded work checklist

Audit date: 2026-09-17. Baseline: `origin/main` at `ef4295f03e4702ed2278437b3b5fb9faa1ed13e4`.

## Method and scope

The target set is the 77 ticket IDs whose remote branches begin with `codex/`, `integration/`, or `operator/` and for which no such branch is an ancestor of the baseline. The all-branch audit saw 570 remote branches; 482 were in those prefixes. The count remains exactly 77 despite drift from the earlier 481-branch snapshot.

Verdicts are conservative: `LANDED ELSEWHERE` requires an identified main carrier, patch equivalence, or content identity; `SUPERSEDED` requires a named replacement; `STRANDED` requires approved content absent from main with no located replacement. Everything else is `UNKNOWN`.

Exact audit command shape, with machine-local credential, CA, cache, and output
locations replaced by required repository-safe placeholders:

```sh
TMPDIR=/PATH/TO/SEAT_CACHE/TK-45deda68383f/tmp python3 tools/branch_audit.py --central-url https://127.0.0.1:8766/mcp --token-file /PATH/TO/WORKER_TOKEN --ca-file /PATH/TO/CA.pem --board pursers --json-out /PATH/TO/BRANCH_AUDIT.json
```

Literal summary output:

```text
Remote branches: 570
Buckets: merged=278  terminal-unmerged=278  live-unmerged=8  no-resolvable-ticket=6
Deletion candidates: tier A=277  tier B=278

Tier A oldest (up to 10)
DATE                       STATUS       TICKET             BRANCH
2026-09-03T19:36:30+07:00  closed       TK-10cea5ba067a    codex/TK-10cea5ba067a
2026-09-03T20:05:23+07:00  closed       TK-75bc6cdc2405    codex/TK-75bc6cdc2405
2026-09-03T20:21:52+07:00  closed       TK-fb29d1de526b    codex/TK-fb29d1de526b
2026-09-03T20:34:49+07:00  closed       TK-d08152560570    codex/TK-d08152560570
2026-09-04T11:09:59+07:00  closed       TK-011d4336785a    codex/TK-011d4336785a
2026-09-04T11:11:50+07:00  closed       TK-a6cd4fc8d082    codex/TK-a6cd4fc8d082
2026-09-04T11:13:14+07:00  closed       TK-65ba2d1bba78    codex/TK-65ba2d1bba78
2026-09-04T11:38:29+07:00  closed       TK-6f77b767c264    codex/TK-6f77b767c264
2026-09-04T11:49:02+07:00  closed       TK-c7e38ee25d5c    codex/TK-c7e38ee25d5c
2026-09-04T17:12:50+07:00  closed       TK-7bbc75fcd1a6    codex/TK-7bbc75fcd1a6

Tier B oldest (up to 10)
DATE                       STATUS       TICKET             BRANCH
2026-09-03T21:38:02+07:00  canceled     TK-2eaba07eca7f    codex/TK-2eaba07eca7f
2026-09-04T20:00:22+07:00  canceled     TK-de3a738ba514    codex/TK-de3a738ba514
2026-09-04T20:43:05+07:00  canceled     TK-ef3ee214b30c    codex/TK-ef3ee214b30c
2026-09-04T21:12:27+07:00  closed       TK-b63868f13453    codex/TK-b63868f13453
2026-09-04T22:43:44+07:00  closed       TK-55b6bc8985fc    goose/TK-55b6bc8985fc
2026-09-04T23:02:10+07:00  closed       TK-55b6bc8985fc    goose/TK-55b6bc8985fc-v2
2026-09-05T12:56:55+07:00  canceled     TK-a4993b1a0320    codex/TK-a4993b1a0320
2026-09-05T23:30:48+07:00  closed       TK-e5cfe34cebdf    goose/TK-e5cfe34cebdf
2026-09-06T00:08:17+07:00  canceled     TK-c2667af0de29    codex/TK-c2667af0de29
2026-09-06T01:58:28+07:00  closed       TK-b53f73288142    goose/TK-b53f73288142

JSON: /PATH/TO/BRANCH_AUDIT.json
```

The non-sensitive tool output above is literal. The final private JSON location is
the sole redaction and uses the same `/PATH/TO/BRANCH_AUDIT.json` placeholder as
the command. The document contains no credential, personal, home, or host-specific
path.

## Checklist

| Ticket | Title | Verdict | Evidence / recovery |
|---|---|---|---|
| `TK-08e58df4a435` | harness: candidate-diff-check must resolve the real candidate commit in PR merge checkouts (post-beta) | **STRANDED** | approved `40468b998169d573eea865f009b57e0faca2c2c0` absent from main; no carrier/successor found. Action: Rebase candidate-resolution logic; rerun candidate-only CI and AionUi harness tests. Size: +150/-3, 4 files. |
| `TK-69ac87902083` | Salvage durable AionUi 2.2.1 facts from the superseded spike into wait-bridge docs (docs-only, no GUI evidence) | **STRANDED** | approved `ddf8517f0f625105edbdafc6b1711a104f4f0bcf` absent from main; no carrier/successor found. Action: Restore sanitized AionUi 2.2.1 observations in current wait-bridge docs. Size: +13/-2, 2 docs. |
| `TK-b7bb402993b7` | Resolve actual AionUi candidate installation and Personal transport paths for Beta sandbox | **STRANDED** | approved `0354cf6764915bb46512c30227b213b229a272c3` absent from main; no carrier/successor found. Action: Recover approved host-readiness report and refresh only changed facts. Size: +508/-0, 1 report. |
| `TK-d20c5429d9b7` | Repair work-offer and ticket-claim disagreement without bypassing seat eligibility | **STRANDED** | approved `fc4d6a8db31a2cfbd80b6441d2dee1830842e40f` absent from main; no carrier/successor found. Action: Rebase claim/offer diagnostics; rerun dispatch/coordinator tests. Size: +385/-13, 3 files. |
| `TK-da393a0cdcb4` | Post-beta: eliminate the py/polynomial-redos regex in fleet_dashboard.py _clean_text redaction | **STRANDED** | approved `f93335b60ed4a07f3ec4c2fc8aaaa7afee5b706f` absent from main; no carrier/successor found. Action: Port linear redaction; rerun CodeQL and redaction tests. Size: +53/-45, 2 files. |
| `TK-ecad41b13a66` | central: allow a lease-less information message from a seat to its project coordinator about offer/identity problems | **STRANDED** | approved `c9847ddf343bdd20529654fc970376b3e1a3a52c` absent from main; no carrier/successor found. Action: Rebase lease-less coordinator-information path; rerun authorization tests. Size: +163/-2, 2 files. |
| `TK-01d03238540b` | Next train: verify post-merge Dependabot and code-scanning alert disposition on foundation main | **LANDED ELSEWHERE** | content-identical file; main carrier e3666944421627528230b6f91078b983bfb3d43b; audited source `a2d2525b2af427472437f550a10914bbaa8f1069` |
| `TK-01f2f9c530ba` | Security: resolve open Dependabot alerts in Pursers; reuse existing update PRs and verify dependency fixes | **LANDED ELSEWHERE** | identified main carrier 133d5f5acd3bd1dca8121b9171879516bcfc3474 (foundation train); audited source `e7d4e8e6c60f48df8b8ba317b77211c87e33ec99` |
| `TK-034e85d4ae4d` | Next train: integrate complete approved Superdesign handoff onto current main | **LANDED ELSEWHERE** | identified main carrier cb8c9c22ec7bcca53040a783d5c946fea3be907d (design carrier); audited source `936e681ad0b4f1d32db323558fce9c65b628abf5` |
| `TK-08269cf76117` | Home feature: standalone seat lifecycle | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `557fbc8af9362031dee6f18db84e3c604f4d133f` |
| `TK-0a395c726efd` | Final train integration: Home, runtime, observer, Personal dashboards and O1 with real acceptance | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `c7c9a2ed2923a0c8bc4b2c4fce51491fa9dfe96a` |
| `TK-0c32e05b2e47` | Fleet + Personal UX: handoff card with accountable endpoints (research prop 4) | **LANDED ELSEWHERE** | identified main carrier dc26988e22134138a036d1089f40f33400ca6fb3 (Personal/handoff train); audited source `41933bc0de79bede87a3b40b868c1a4eed98c088` |
| `TK-12187737e73f` | Home feature: show submitted results and review outcomes | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `792dcae5dfe1a194329471fd914f0f6b1518db27` |
| `TK-14449dad2afb` | Fix generated seat verifier false-positive credential vocabulary and safe suite-command replay | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `60a048f9df0b8b91ea9b967e3670796d87a3e469` |
| `TK-1771392453e4` | capabilities.model is null on every seat: stamp model and provider at onboard so cost and quality become a query instead of a guess | **LANDED ELSEWHERE** | model/provider outcome carried by 82c1224ece69a9f4e50d19be2fa366f62dc50398; audited source `d689d333994feacbad82c7d2b73e3e7b0cfc9197` |
| `TK-1cca1a4f663d` | Personal dashboard-ui UX: lifecycle rail + Now/Next/Blocked + activity drawer (research props 1-3) | **LANDED ELSEWHERE** | identified main carrier dc26988e22134138a036d1089f40f33400ca6fb3 (Personal/handoff train); audited source `eba5c37c915127eb07adedf095043fe21b8a8f9e` |
| `TK-1f8315536a3e` | Pursers Home: beginner journey, plain-language navigation and all-dashboard acceptance matrix | **LANDED ELSEWHERE** | identified main carrier 133d5f5acd3bd1dca8121b9171879516bcfc3474 (foundation train); audited source `8202422c6841b84a6aeab64f6722e93805a3c86a` |
| `TK-21ec389509f8` | Unblock final browser acceptance: isolated sandbox worker and Fleet credentials | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `f82cc19aac3b7d57a3ea6e0ea648a49650778de1` |
| `TK-23f86d56ff99` | Aion host transport: make installed Home functional through supported runtime integration | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `f5a24301262bcbe56d993276362dc74d6f1728ad` |
| `TK-2eaba07eca7f` | Push-wait 4/4 worker-runtime: headless worker + reviewer fully cue-driven | **LANDED ELSEWHERE** | patch-equivalent main commit 6c9c3676dfb981603cdba010f85906681371c018; audited source `6c9c26366794803ae9c0b84331fb1e1dcf3b67c5` |
| `TK-352404f16a5f` | Next train: extension and embedded-dashboard artifact parity audit checklist | **LANDED ELSEWHERE** | patch-equivalent main commit b06ce6627edb62fc588eee541fa568445b709054; audited source `188490d823937b9ff69e0adc62929c77cd8207b5` |
| `TK-3776ba2309a8` | O1: audit HTTP-loopback cutover inputs, rollback and current AionUI fleet migration readiness | **LANDED ELSEWHERE** | identified main carrier 133d5f5acd3bd1dca8121b9171879516bcfc3474 (foundation train); audited source `47bd5d39f2b75fa166a12948200f71bc665099ed` |
| `TK-3862f61d9ee7` | Central MCP: response projection phase 2 - view=summary\|work\|full on reads, dispatch_summary, id_map | **LANDED ELSEWHERE** | patch equivalents dc47979f60467781359a57639e263dc5e86ceb36 and 20e75c08c67f7f0c36d3d838c5fc5acaba91bd63; successor a59944575cf7ec9cd255cabe54e7d2e8826a42ec; audited source `bf4ef978196e5680863b080ff01163e9863ffc6a` |
| `TK-3887c4b0a7c9` | Next train: verify and fix remaining CodeQL polynomial ReDoS alert #10 in Fleet redaction | **LANDED ELSEWHERE** | CodeQL successor carrier ad55c14a38e28fe49253d974d96d1771141a7607; audited source `770f27fcabd4b3333faec692668d1e88661f2afe` |
| `TK-3aed0f57c5ad` | Rebase approved handoff card (TK-0c32) onto main after Fleet rail + bundle landings (rebuild bundle, regenerate lock) | **LANDED ELSEWHERE** | identified main carrier dc26988e22134138a036d1089f40f33400ca6fb3 (Personal/handoff train); audited source `88f22fe9dd72217bb9d8863063ff33ccaf5aff32` |
| `TK-3d56743e40b2` | Fix installed wait-bridge help requiring Central credentials before usage | **LANDED ELSEWHERE** | approved help patch carrier 1e00c633e753334c75435835209b9dd577601abc; audited source `41d9ee914a7f02b4e59480ee68610978b36e25b2` |
| `TK-4ba6bd1964de` | AionUI extension: secure idempotent door onboarding and resumable first-run backend contract | **LANDED ELSEWHERE** | onboarding successor carrier 9e3b05072e4521c10d0f4390d7ac57ad7421c07a; audited source `ff749da633815f0b539b39155ae4d712a4bcdb44` |
| `TK-4f1f01ba50eb` | Pursers Home: end-to-end acceptance harness for extension onboarding, Teams and dashboard parity | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `44f19c05b16446cfd2127cc9e03760c65adcc9c6` |
| `TK-566e3ea3418c` | deps: migrate tools/dashboard-ui to @modelcontextprotocol/ext-apps 2.0.0 (major) with bundle + component-lock refresh - closes dependabot #31 | **LANDED ELSEWHERE** | ext-apps successor carrier 844c91bb4ffc830bb44ae0f68be29a504272e726; audited source `21bf3d1b7871add4552456c71b19d83a528b2620` |
| `TK-63330db61047` | Rebase approved offer catch-up (TK-97d8) onto main after the same-cursor fix (wait-bridge + central tests) | **LANDED ELSEWHERE** | patch-equivalent main commit 5e809adb626cdf75afa070df50678c45ac96c767; audited source `bef515c8ff4ec4179f2958ffc2ebf694e4b7b384` |
| `TK-63e391d86a18` | Map 121 behavior IDs onto typed collector contracts using existing source maps | **LANDED ELSEWHERE** | all patches carried by ef83a3f4bffad89569a81a377969d47cb00bbcdd, 6e4e8de0065ee9848978ae15a9dc500f30a9af26, 0505e242e253f1c4ec466e51a12b490735f56191; audited source `f9b2d37ac687abcef510e7a854446a469588702a` |
| `TK-6db356ba00b8` | Home feature: standalone worker group lifecycle | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `20bb6bc5c54ad7b233dfc790a48d3bea335a92da` |
| `TK-6f15dd177ee5` | Instrument one real ticket end to end: turns and tokens per side, so the routing table stops being a shape and becomes a forecast | **LANDED ELSEWHERE** | successor 922b04bbcec4ba59579c495a8d0da1f9821c3406; patch equivalents 5c10cb135c28bf7578633338ba4fb7e9abc6716d and 042013223dbb1d240cd2667d729d2311162fae8e; audited source `749c97084bc28d6546863e290cb2514535874d8d` |
| `TK-80e362bdcd96` | Seat submission preflight: prevent nonexistent or mismatched commit evidence before board mutation | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `165dfd9f83aa6e783164f47125ae3002c6a7b804` |
| `TK-810b86e4b9c1` | Superdesign canvas: redesign ALL Pursers dashboards as an approachable AionUI Home | **LANDED ELSEWHERE** | identified main carrier cb8c9c22ec7bcca53040a783d5c946fea3be907d (design carrier); audited source `d329ddaaf6068fbf6bed8e3cec219d49a574048d` |
| `TK-812eb9e77e57` | Next train: integrate approved complete Fleet Home dashboard onto current main | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `b8b15fbbe1b76dccd79bf34b3a19e10a48ace019` |
| `TK-823656fc373f` | Security: resolve open Code scanning alerts in Pursers; validate fixes with scanning and regression evidence | **LANDED ELSEWHERE** | identified main carrier 133d5f5acd3bd1dca8121b9171879516bcfc3474 (foundation train); audited source `a93dd1ff97cfe62e25dc9fbb80baf2d176d91276` |
| `TK-856626a2af6d` | Fix board_list KeyError on membership without role without weakening authorization | **LANDED ELSEWHERE** | patch-equivalent main commit 2ee4474d0ab37a241ac329b254ec1c143ee55cab; audited source `ed05ea81786b853961fa81206c9a1cddffaa8f74` |
| `TK-8cfd30eabb7a` | Next release train preparation: verified version map, exact ship gates and rollback commands | **LANDED ELSEWHERE** | content-identical ship plan; carrier 068b67d5b1333c82855dceb0bcd359d8bdd9bab5; audited source `85e11843e2993385b0a79e3039895759df10b29e` |
| `TK-919fe5722f3d` | DOCS (EN): add docs-local/architecture-en.html — English counterpart of architecture-th with a14→a21 + unreleased architecture, TH↔EN parity | **LANDED ELSEWHERE** | ticket-named main carrier e1d698fa621c711c8d9bf5a5bb4c40f3e38e0e0b; audited source `ce1c4ae260f99ff51b81bcbc646d14213d9cdf90` |
| `TK-9878db6ffe9f` | Browser201 verifier: automate AionUi WebUI pairing (pre-authorized by operator) - macOS first, Linux notes | **LANDED ELSEWHERE** | pairing patch carrier e01c7077e8929557752a7b44b75d9ec69dff73e1; audited source `4218cb8a6d83b360fe583bac52f774b060cd20bf` |
| `TK-9ca52e7ad8bf` | Personal browser route: make the 79 uncaptured Browser201 rows reachable on the AionUi origin | **LANDED ELSEWHERE** | ticket-named main carrier 8ecca2996a707a5c7c0bc7336edad4092820cb27; audited source `ee024066d1305762581f17e758ed580f60e56869` |
| `TK-a2115c493a18` | Post-beta: strict review enforces SEAT independence but the product says PRINCIPAL independence | **LANDED ELSEWHERE** | principal-independence behavior exists on main; path carriers 4ab90830f5f5ef978b6af9d34e0d864e4362a736 and 5c10cb135c28bf7578633338ba4fb7e9abc6716d; audited source `4fd28443d752e053d2c343d4cfba6ceb1050709e` |
| `TK-a4972ef8fab0` | P0 fleet-dashboard: /api/fleet and /api/workers always 503 ExceptionGroup (httpx2.ReadError) in server mode against live Central | **LANDED ELSEWHERE** | ticket-named main carrier bb16ad8be7af81c27cd93b4b830271702051ef83; audited source `1d751f8c159a6cb4fff4b7aa285082452197806b` |
| `TK-a4993b1a0320` | Release train 5.0.0a18 (refile, lease-safe): central a22 / client a16 / personal+meta a18 / wait-bridge a8 — CHANGELOG + lock | **LANDED ELSEWHERE** | release-train main carrier f9535b0d8211b48103fd8634bd6bd7208bc63fc5; audited source `9079fdb12a74a3d82f8095da3add5b9104265d06` |
| `TK-a8802d838910` | Fix Fleet Dashboard 503 from repeated same-principal dashboard identity joins | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `e229dcb08879666475c532fd7296f4c5a2d9167b` |
| `TK-ab876afbb077` | Beta UI: execute the 400px/1440px, keyboard, zoom and host-theme acceptance pass (ego lite) | **LANDED ELSEWHERE** | patch-equivalent main commits 597fcb17d9de7ae0d45fee5b596990db58ddea99 and a2ae7669253f8af77d469fd325a7b98eb22a031f; audited source `e3dbddc6c5dc1a36d65330fb4612ed9c76ec4ec7` |
| `TK-ab9583ba9756` | Integrate approved train baseline: Home, Canvas, acceptance harness, submission preflight and verifier | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `27811c540465ed94a3f2eb784be50d0c417d3c2a` |
| `TK-ad10a0e7b9d8` | fleet-dashboard: stop joining the board to read it; lazy single join only when writing | **LANDED ELSEWHERE** | lazy-join successor carrier 2a9365e6c39e86558ddf3a34f477f69e20f57d85; audited source `1eaabe93669c98c42727d9ef809af95eb5320611` |
| `TK-b63868f13453` | Release train 5.0.0a17: central a21 / client a15 / personal+meta a17 / wait-bridge a7 — absorbs review-lease kinds alignment; isolated-install gate; component lock; CHANGELOG | **LANDED ELSEWHERE** | successor d4b33db1cb4f68266b20d7a0069da5d0222242a2; patch equivalent 82d886c0ef9a4c5295b568c375c313f15e29032f; audited source `83e922052ab8e599e30a3375ba28d3551ad741c0` |
| `TK-b84df54bfb56` | Post-publish docs: remove Publication notes, link the live v5.0.0b1 release everywhere (README badge, GETTING-STARTED download, CHANGELOG date) | **LANDED ELSEWHERE** | ticket-named main carrier 00d4d5b6b684988433deac2f8a6546e19a3f52ce; audited source `6e0115b81e1712ca092d1cbd1fb28cc30ec3f836` |
| `TK-bb8a5525c620` | Implement Warm Guided Home across Personal dashboard and all MCP dashboard views | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `53a098963b414aa73185ae9c8d046d7c5f0ada09` |
| `TK-c2667af0de29` | Decide the memory_* deprecation: Personal app is the intended caller (memory_read/search/links/checkpoint/handoff/unpin, ticket_terminate) — un-deprecate or migrate before a21 removal | **LANDED ELSEWHERE** | memory successor carrier 8c0f544fac3f1ce157aaf194fcc66a6d1a9fa30e; audited source `5e9211602c6ab6ebed27283a5f489bc260fb84dd` |
| `TK-c5b7fef1ee2d` | Wait bridge + dashboard: deliver human requests via MCP elicitation (InputRequiredResult, form/url, capability-checked) with a dashboard fallback form; directive uses ticket_request_human | **LANDED ELSEWHERE** | elicitation carrier f28e3b35c1c70016457d6dc76cd3413d10cbb89b and bddad94f5a4a856aaff80058171e494cbb6ce254; audited source `971d26a17a279265c080d1c018b138d52b91cc04` |
| `TK-c7eace5014e3` | Integrate approved Fleet Home, extension package, and CodeQL scanner on frozen main | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `ad042cbdae529a52868bbbc22d35229fe62708e4` |
| `TK-c915b4220dd3` | Implement Warm Guided Home across ALL Fleet dashboard routes and operations surfaces | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `9f883da59e60a879df5a5591f9beaf75c8f393ad` |
| `TK-cae112a74ae9` | Prepare reproducible isolated acceptance sandbox and independent reviewer handoff for final Home candidate | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `197254bfe2758d1e656c6a923a6d5b3d531193ea` |
| `TK-de3a738ba514` | seat-kit reviewer hardening: HARD-verify checklist in generated hints, evidence-gated approve, board.sh verify helper | **LANDED ELSEWHERE** | seat-kit successor carrier 6d1b7acee4e92906516658d7712806550b5ecfc5; audited source `7f7299ce21fd302b7595c90ad40218ff96de5fd9` |
| `TK-df38c5eb3b9f` | Implement AionUI Pursers Home extension: guided setup, Team controls, recovery and packaged entrypoint | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `8ae39dafb9e1a681961c22259dccc19164231591` |
| `TK-e18268cb4fb1` | Fix stale review offers that survive verdict and block the next independent review | **LANDED ELSEWHERE** | patch-equivalent main commit 931dd83a68cbdc86b4e85cea05e5050c1e52d40c; audited source `56ead83c7f4ed8d3e1973775a9d73b2f973e8a09` |
| `TK-eeb3ac4fba84` | O1: prepare private staged HTTP cutover and coherent rollback scripts from approved readiness runbook | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `10f29f122dbc6975f5f272b7315d9fd58eebc3ab` |
| `TK-ef3ee214b30c` | Align review-lease journal event kinds across Central, bridge, seat-kit (after TK-84db4b8511ff merges) | **LANDED ELSEWHERE** | review-lease successor d4b33db1cb4f68266b20d7a0069da5d0222242a2; audited source `d0c36e48dc52901c7e9e85d85b5c3f19193ee47a` |
| `TK-f8a62bab8d05` | Pursers Home: inventory ALL dashboard surfaces and prepare complete Superdesign UI context | **LANDED ELSEWHERE** | identified main carrier cb8c9c22ec7bcca53040a783d5c946fea3be907d (design carrier); audited source `0b0ef231793440b14402b76ab03f041dda70bd01` |
| `TK-fa155aa34187` | Real AionUI acceptance: implement verifier-owned browser observer and reproducible sandbox runner | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `e4fa835ff9eaca3af5156c74c0c7335fbe43316a` |
| `TK-fbc60c370b5b` | fleet-dashboard tests not hermetic: they write to the real ~/.pursers/workers and shell out to ps, so the ten-suite gate fails inside a sandboxed seat; isolate the state root, fake the process lister | **LANDED ELSEWHERE** | hermetic-runtime successor f95328b606174a2edb0d08f141fc3c4264ed393e; four blobs identical; audited source `a1570b59279c86ee50c214c5f57391638302d57b` |
| `TK-fc9727be2848` | Home feature: ticket lifecycle through supported board actions | **LANDED ELSEWHERE** | identified main carrier 3f16f0595f0dad56b4d1b104a59c261b1ae93ca6 (assembled Home train); audited source `0b0f0b78385a0315aa4e93e574fa967d0cef65ab` |
| `TK-fce1144ef977` | Fleet unblock: diagnose stale offers and Team-local waiting without restarting any seat | **LANDED ELSEWHERE** | stale-offer successor 919635c8924d091a74a614940a0aab3e549b7c7a; audited source `3f5eb30f1f7a1f53e12c603d280adae226d0ba54` |
| `TK-763e2b9761bb` | AionUi spike (sandbox board, no Central changes): one extension pre-wires Pursers seats? mcpServers reach sessions, preset seat joins and claims, API-opened conversation, parallel seats distinct | **SUPERSEDED** | rejected spike replaced by supported Home/Aion carrier 8dbe9ff0cf1acb6b7aae1bcecafbe7394a7b0a14; audited source `78502bb5871030cf861192d0f0cc95c72b79b8d9` |
| `TK-7d8fb1bd7e28` | Map Fleet acceptance facts from source for Opus ground B | **SUPERSEDED** | source map consumed by approved TK-af8a59c05f34 successor on main; audited source `fa62185980e69a5a4087ed31ab5fe501a9f44472` |
| `TK-806ff6c21769` | Draft the v5.0.0b1 GitHub prerelease notes ahead of the tag, from repository evidence only | **SUPERSEDED** | pre-tag draft replaced by published release documentation on main; audited source `127a1e2db64113fd6e3c1a508c223448eba48239` |
| `TK-c169c98c6150` | Map Personal and Extension acceptance facts from source for Opus ground B | **SUPERSEDED** | source map consumed by approved TK-af8a59c05f34 successor; intermediate JSON not retained; audited source `11885134bd53ec83821ea8c672f8c85358825be1` |
| `TK-515fc1706606` | Prepare isolated sandbox Central and reviewer door for final Home browser acceptance | **UNKNOWN** | `closed` / review `approve`; source `197254bfe2758d1e656c6a923a6d5b3d531193ea`; 1/1 blobs equal main; insufficient carrier evidence |
| `TK-56282d7b4b42` | release_train.py maps the README and whats-new to product only, so a component-only train ships install commands for a cohort that does not exist | **UNKNOWN** | `claimed` / review `reject`; source `9b5e5889a938937948682ead19f8d175916492c5`; 0/2 blobs equal main; insufficient carrier evidence |
| `TK-87875561e2da` | Give the butler every board in the registry to watch: today a parked ticket still reported starved for seven minutes and one board's findings are a day old | **UNKNOWN** | `claimed` / review `reject`; source `0b6be31d8535320603c02e352472905ec725abb1`; 1/4 blobs equal main; insufficient carrier evidence |
| `TK-891afe79aa99` | Butler settings in the Fleet dashboard: choose endpoint and model on the page, keep the key off the page and out of the board | **UNKNOWN** | `claimed` / review `reject`; source `b06e559d96f4d114a841b3581b805d9fa697e5e6`; 0/3 blobs equal main; insufficient carrier evidence |
| `TK-cadfa2b8b33f` | Next train: beginner quickstart and recovery guide aligned with Warm Guided Home | **UNKNOWN** | `canceled` / review `reject`; source `7cbd1edaa6b883d2a06c8df461a5dd450889f487`; 0/1 blobs equal main; insufficient carrier evidence |
| `TK-f8f45698471a` | Score the butler against the answers it did not give: keep every shadow draft next to the human answer, and cite precedent on the next question | **UNKNOWN** | `claimed` / review `None`; source `bfc2e7cf839d28c0ff8a5323d52906575652c2d3`; 0/0 blobs equal main; insufficient carrier evidence |

## Release-artifact gap

The AionUi ZIP and Home wheelhouse were built and verified during beta work, but neither is attached through the b1/b2 release path. `.github/workflows/release.yml` builds six wheels, writes `dist/SHA256SUMS.txt`, and hard-codes `assets=(dist/*.whl dist/SHA256SUMS.txt)`. It never invokes `tools/aionui-extension/build.py` or `tools/build_home_runtime_wheelhouse.py`; therefore `pursers-aionui-0.1.0.zip` and the Home wheelhouse archive/manifest cannot enter the release asset list.

Required workflow changes (not made by this audit):

1. Add a deterministic AionUi ZIP build step, verify checksum/contents, and stage the ZIP.
2. Build and deterministically archive the Home runtime wheelhouse, verify its manifest/checksums, and stage archive plus manifest.
3. Append those files to the `assets` array; the existing verification loop can verify the expanded set.

No repository or ticket decision was found that deliberately excludes these verified artifact families. Docs describe b1 as six wheels plus `SHA256SUMS.txt`, while AionUi/Home remain local build procedures. This is therefore an omission, not an intentional exclusion.

## Landed but unshipped distributions

The original audit (the 79-row audit in the ticket record) was scoped to
tickets and branches and therefore could not have found code that had landed on
`main` but was omitted from the release system. The current checklist documents
77 ticket IDs plus separate artifact analysis; neither target set inventories
declared distributions. This section adds that missing, structurally different
check.

Audit date: 2026-09-17. Distribution baseline: `origin/main` at
`26db4502855dd347a9ca3f37bb6d8351b480e9e1`.

### Method

The sweep enumerated every tracked `pyproject.toml` with a `[project].name`,
including its declared version, `[project.scripts]`, and
`[project.entry-points]` values:

```sh
python3 - <<'PY'
from pathlib import Path
import tomllib

for path in sorted(Path('.').rglob('pyproject.toml')):
    if '.git' in path.parts:
        continue
    project = tomllib.loads(path.read_text()).get('project', {})
    if project.get('name'):
        print(path, project['name'], project.get('version'),
              project.get('scripts', {}), project.get('entry-points', {}))
PY
```

For each result, fixed-string searches checked the version manifest and every
GitHub Actions workflow. The manifest's own coverage function independently
reported which required suites cover each project file. A separate search of
`package.json`, `setup.py`, and `setup.cfg` found no additional public
distribution or user-installable entry point: `pursers-dashboard-ui` is private
and has no `bin`, and `packages/import/setup.py` is the build shim for the
already-counted `pursers-personal-import` project.

```sh
rg -n 'pursers-acp|acp-agent|pursers_acp|pursers-wait-bridge|wait_bridge' \
  tools/release_versions.toml .github/workflows tools/release_train.py \
  tools/release_versions.py tools/verify_publish_wheels.py
python3 - <<'PY'
from pathlib import Path
import tomllib
from tools.ci_manifest import covering_suites

for path in sorted(Path('.').rglob('pyproject.toml')):
    if '.git' in path.parts:
        continue
    project = tomllib.loads(path.read_text()).get('project', {})
    if project.get('name'):
        relative = path.as_posix()
        print(project['name'], covering_suites([relative])[relative])
PY
find . -name package.json -not -path './.git/*' -print
find . -type f \( -name setup.py -o -name setup.cfg \) \
  -not -path './.git/*' -print
```

Finally, the public PyPI JSON endpoint was queried for every declared Python
distribution. HTTP 200 means the project exists on PyPI; HTTP 404 means it does
not. This establishes project presence, not whether the checkout's exact version
has been uploaded.

```sh
for name in pursers pursers-central pursers-client pursers-personal \
  pursers-personal-import pursers-wait-bridge pursers-acp; do
  code=$(curl --silent --show-error --location --output /dev/null \
    --write-out '%{http_code}' "https://pypi.org/pypi/$name/json")
  printf '%s\t%s\n' "$name" "$code"
done
```

### Distribution results

"Built" means at least one checked-in GitHub Actions workflow builds that
project. "Tested" means `tools.ci_manifest.covering_suites()` returns a suite for
the project's `pyproject.toml`; this deliberately does not infer coverage from a
green workflow.

| Distribution | Declared version | In `release_versions.toml` | Built by a workflow | On PyPI | Tested in `ci_manifest` | Evidence |
|---|---:|---|---|---|---|---|
| `pursers` | `5.0.0b2` | Yes | Yes | Yes (HTTP 200) | No | Built by CI, `publish-pypi.yml`, and `release.yml`; the manifest reports no suite covering `packages/pursers/pyproject.toml`. |
| `pursers-central` | `0.1.0a31` | Yes | Yes | Yes (HTTP 200) | Yes | `central` (and the package is built in all three release/build workflows). |
| `pursers-client` | `0.1.0a24` | Yes | Yes | Yes (HTTP 200) | Yes | `client` and `aionui-extension`. |
| `pursers-personal` | `5.0.0b2` | Yes | Yes | Yes (HTTP 200) | Yes | `personal` and `aionui-extension`. |
| `pursers-personal-import` | `5.0.0a3` | Yes | Yes | Yes (HTTP 200) | Yes | `import`. |
| `pursers-wait-bridge` | `0.1.0a17` | Yes | Yes | Yes (HTTP 200) | Yes | `wait-bridge` and `release-tools`; it has its own Trusted Publishing job. |
| `pursers-acp` | `0.1.0` | **No** | **No** | **No (HTTP 404)** | Yes | `acp-agent` and `release-tools`; no release/version/build match exists outside ACP's own source and tests. |

There is exactly **one** landed-but-unshipped distribution:
`pursers-acp`. No other declared Python distribution is in the same state.
`pursers` has a separate test-coverage gap, but it is versioned, built, and
published, so it is not an unshipped distribution.

The source re-check also corrects two preliminary claims. There are 16 tracked
files under `tools/acp-agent/` and `tools/acp-seat/`, not 17. The declared tests
do collect as 32 ACP-seat tests plus 15 ACP-agent tests, but the ACP package is
not release-synchronized: its dependency pins remain
`pursers-client==0.1.0a23`, `pursers-personal==5.0.0a26`, and
`pursers-wait-bridge==0.1.0a16`, while this baseline declares `0.1.0a24`,
`5.0.0b2`, and `0.1.0a17`, respectively. The code is maintained and tested;
the installable release contract is stale.

### Installable entry-point results

No `[project.entry-points]` tables were declared. All user-installable entry
points came from `[project.scripts]`:

| Command | Owning distribution | Owner in release manifest | Owner built by workflow | Owner on PyPI | Owner tested in `ci_manifest` |
|---|---|---|---|---|---|
| `pursers-central` | `pursers-central` | Yes | Yes | Yes | Yes |
| `pursers-personal-import` | `pursers-personal-import` | Yes | Yes | Yes | Yes |
| `pursers-personal` | `pursers-personal` | Yes | Yes | Yes | Yes |
| `pursers-wait-bridge` | `pursers-wait-bridge` | Yes | Yes | Yes | Yes |
| `pursers-door` | `pursers-wait-bridge` | Yes | Yes | Yes | Yes |
| `pursers-acp` | `pursers-acp` | **No** | **No** | **No** | Yes |

Thus exactly **one** plausibly installable command is also landed but
unshipped: `pursers-acp`, from the distribution of the same name.

### What shipping `pursers-acp` would require

These are the concrete edits, in dependency order. This audit deliberately
makes none of them and selects no release version.

1. The release-train operator chooses the ACP version and adds an `acp` package
   key to `tools/release_versions.toml`. `tools/release_versions.py` must add the
   same key to `PACKAGE_KEYS` and map it to `pursers-acp` in
   `WHEEL_DISTRIBUTIONS`, so exact wheel names and tag assets include it.
2. `tools/release_train.py` must recognize `tools/acp-agent/pyproject.toml` as
   `pursers-acp`, add the ACP version consumers to `VERSION_FILES`, and validate
   ACP's exact dependencies on client, Personal, and wait-bridge. The synchronized
   consumers include the pyproject version, `IMPLEMENTATION_VERSION` in
   `src/pursers_acp/agent.py`, the version and `uvx` pins in
   `pursers/agent.json`, and the registry assertions in
   `tests/test_registry.py`. Its release-train tests must cover all new mappings
   and reject stale pins.
3. The selected train updates those ACP version consumers and replaces ACP's
   stale dependency pins with the client, Personal, and wait-bridge versions
   selected in the same release manifest.
4. `.github/workflows/ci.yml` adds the ACP pyproject to the cache key, builds its
   wheel, installs it in the isolated-wheel smoke test, and checks its
   distribution metadata and `pursers-acp` entry point. The existing
   `acp-agent` and `acp-seat` suites remain required by `ci_manifest.py`.
5. `.github/workflows/publish-pypi.yml` builds
   `tools/acp-agent`, passes the wheel through
   `tools/verify_publish_wheels.py`, and publishes it from a PyPI Trusted
   Publishing environment authorized for the new project. Because the JSON
   endpoint is currently 404, the operator must first configure the PyPI project
   or pending trusted publisher; the workflow must not assume it already exists.
6. `.github/workflows/release.yml` builds a seventh wheel and includes it in the
   exact `expected_wheel_filenames()` cohort and `SHA256SUMS.txt`. Tests and
   release documentation that currently assert "six wheels" must be updated to
   the new cohort. ACP's registry descriptor should be submitted upstream only
   after its pinned PyPI artifact exists and passes an isolated `uvx` smoke test.
7. Add all 16 tracked ACP-agent and ACP-seat paths to
   `tools/aionui-extension/INTEGRATION_FILES.sha256`, then regenerate that file
   last. Every one of those paths is in the cumulative diff from the manifest's
   frozen base `0c83cd8da4e8ac04ee2dd564559f57718335cdef`; the current manifest contains
   17 wait-bridge rows and zero ACP-agent or ACP-seat rows.

The Personal component lock is not a precedent for adding ACP. Its declared
scope is only the embedded `pursers-central` and `pursers-client` wheels plus the
dashboard view; even `pursers-wait-bridge` is not a locked component.
`tools/regenerate_component_lock.py` therefore needs no ACP entry unless the
product separately decides to embed ACP inside Personal. The existing lock must
still remain byte-consistent after release-train changes.

The resulting change would be gated by: release-manifest parsing and exact
version/dependency checks; release-train tests and `release_train.py check`;
ACP's 47 focused tests; complete `ci_manifest.py` check/collect/run/verify;
integration-manifest validation; pinned-generator wheel verification; isolated
wheel import/metadata/entry-point smoke tests; exact release wheel-cohort and
checksum checks; tracked-file leak scan; `git diff --check`; the normal CI and
CodeQL workflows; PyPI Trusted Publishing; and a post-publish install/`uvx`
smoke test. Tagging, publishing, and version selection remain operator actions.

## Observations

- Stranded count: **6** of 77.
- Top recovery 1: `TK-08e58df4a435` — restore real candidate resolution before more AionUi candidate-only CI.
- Top recovery 2: `TK-da393a0cdcb4` — restore linear redaction because it closes a security-scanner finding.
- Top recovery 3: `TK-d20c5429d9b7` — restore claim/offer diagnostics so eligibility disagreement cannot silently recur.
- `UNKNOWN` is deliberate for live/rejected or no-diff operational tickets where equivalent product content was not proved.
