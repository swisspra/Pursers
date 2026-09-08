# AionUi 2.2.1 host evidence

This evidence was captured from an isolated local AionCore host with a synthetic
`sandbox-home` wait-bridge state. No production Team, seat, board, or operator
profile was used.

- Final package SHA-256:
  `9f18c95cc2817ffed47e83094c6930d5e145b0a8e9fdd2f06e0e19cad3ea5647`
- AionUi version: `2.2.1`
- AionUi `app.asar` SHA-256:
  `6c8ace0cb3e94e4183d50ccc8504bd1f5b75dcad43cfc269c5f339ad0a9d467f`
- Bundled AionCore version: `0.2.1`
- Bundled AionCore binary SHA-256:
  `e52cb9f83cef6186db2df9964367555d4110d10f66c30c8943531286aa83c575`
- Screenshot: `aionui-2.2.1-home.png`
- Screenshot SHA-256:
  `d4c7f259fd13c4f4969e80b5b9fdc566509a93bf66779afd609e8e41665a9d41`

After the exact package was unpacked into the isolated extension directory and
the host restarted, AionCore returned `200` for extension discovery, WebUI
metadata, the settings-tab asset, and the previously imported environment-free
Pursers MCP definition. The unsupported in-host `/pursers/status` route remained
`404`, confirming that Home did not depend on an invented route executor.

The authenticated helper returned `200` for helper status, selected-board
status, and a valid synthetic door. It returned `401` for a wrong token, `403`
without CORS authorization for a wrong origin, `422 wrong_board` for a door
from another board, and `413 request_too_large` for a body above 64 KiB.

Real-browser DOM and network checks showed:

- helper and saved board: `sandbox-home`;
- helper and door input lengths after use: `0`;
- local and session storage entries: `0`;
- helper status and onboarding status: `200`;
- Team status and plan: `409 runtime_context_missing`;
- plan outcome: `Use native Team`; and
- confirmation and start controls: disabled.

The screenshot contains redacted status only. It contains no door or helper
token.
