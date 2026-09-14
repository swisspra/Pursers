# AionUi 2.2.1 host evidence

Release evidence is captured from an isolated local AionCore host with a
synthetic `sandbox-home-acceptance` wait-bridge state. No production Team,
seat, board, operator profile, or credential is used.

The exact installed package SHA-256, candidate commit, screenshot byte count
and SHA-256, accessibility snapshot byte count and SHA-256, observer id, and
action receipts live in the verifier-owned evidence bundle supplied with each
submission. They are intentionally not copied into this tracked file: the
package embeds exact Git `HEAD`, so recording its hash in the same commit would
create a circular and unverifiable self-reference. Old package and screenshot
hashes are not release evidence.

- AionUi version: `2.2.1`
- AionUi `app.asar` SHA-256:
  `6c8ace0cb3e94e4183d50ccc8504bd1f5b75dcad43cfc269c5f339ad0a9d467f`
- Bundled AionCore version: `0.2.1`
- Bundled AionCore binary SHA-256:
  `e52cb9f83cef6186db2df9964367555d4110d10f66c30c8943531286aa83c575`

After the exact package is unpacked into a fresh isolated extension directory
and the host restarted, the observer must bind the installed `candidate.json`,
rendered selected board, and same-origin status reads through a verifier-created
CDP isolated world. It must also independently bind the live listener to the
signed AionUi bundle and AionCore health response.

The authenticated helper must return `200` for helper status, selected-board
status, and a valid synthetic door. It must return `401` for a wrong token, `403`
without CORS authorization for a wrong origin, `422 wrong_board` for a door
from another board, and `413 request_too_large` for a body above 64 KiB.

The external bundle must include real-browser DOM and network receipts for:

- helper and saved board: `sandbox-home-acceptance`;
- helper and door input lengths after use: `0`;
- local and session storage entries: `0`;
- helper status and onboarding status: `200`;
- Team status and plan: `409 runtime_context_missing`;
- plan outcome: `Use native Team`; and
- confirmation and start controls: disabled.

The bundle must additionally prove wrong token, wrong Origin, wrong board, and
oversized body fail closed; native-Team fallback remains visible; plan
confirmation and Start remain disabled. Screenshots and snapshots may contain
redacted status only and must contain no door or helper token.
