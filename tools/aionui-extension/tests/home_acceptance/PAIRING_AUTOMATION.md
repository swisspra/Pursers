# AionUi WebUI pairing automation

`pair_aionui.py` automates the operator-pre-authorized pairing gate for a
private Home acceptance handoff. It reads the loopback WebUI port and Core
data directory from `handoff.json`, and reads the ego-browser executable and
task space from that handoff's installed observer configuration. It does not
start another browser stack.

## macOS one-time setup

Grant Accessibility permission to the terminal or agent host that runs the
tool: **System Settings > Privacy & Security > Accessibility**. AionUi must be
running, signed in, and its WebUI must be running. The tool opens Settings,
reads the active code from the Remote/WebUI view through System Events, fills
the WebUI `#/login` form in the existing ego-browser task space, and clicks
the native confirmation only when both English strings match exactly:

- title: `WebUI Pairing Request`
- message: `A browser is requesting access to the desktop WebUI.`
- confirmation button: `Confirm`

Run it once for each long-lived Core runtime. The code is transient and is
never printed or stored:

```sh
python3 /PATH/TO/CHECKOUT/tools/aionui-extension/tests/home_acceptance/pair_aionui.py \
  --handoff-runtime /PATH/TO/HANDOFF/handoff.json \
  > /PATH/TO/HANDOFF/pairing-proof.json
```

Attach that proof to the first AionUi capture for the runtime:

```sh
python3 /PATH/TO/CHECKOUT/tools/aionui-extension/tests/home_acceptance/runner.py capture \
  ... \
  --pairing-proof /PATH/TO/HANDOFF/pairing-proof.json
```

The proof contains only pairing outcome, pre-authorization mode, tool version
and SHA-256, Core port, UTC timestamp, and code-consumption status. The runner
and harness enforce its exact schema and target port.

## Linux design notes (no implementation yet)

Keep the HTTP and ego-browser portions unchanged. Replace only the desktop UI
driver with an AT-SPI implementation (preferred) or a tightly scoped `xdotool`
fallback. The driver must read exactly one six-character code from the
Remote/WebUI settings accessibility tree, match the exact dialog title and
message before activating the default confirmation button, and use bounded
waits. An upstream authenticated IPC/token API would be preferable if AionUi
adds one; `/auth/pair-info` intentionally exposes status but not the code.

Do not read AionUi state files, weaken the confirmation gate, persist the
pairing code, or introduce Playwright/Selenium. Linux acceptance remains an
operator follow-up until one of those drivers is reviewed on a disposable
runtime.
