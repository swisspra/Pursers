# Recording the product demos

These captures use a real disposable Central, real MCP calls, and the real Fleet
Dashboard. All visible names and ticket text are synthetic. Never point these
commands at an existing Central or use ports `8766` or `8899`.

## Run the deterministic scenarios

Use a private directory under your own cache. The example ports are intentionally
different from the product defaults.

```sh
export DEMO_ROOT="$HOME/.cache/pursers-media-demo/run-1"
export CENTRAL_PORT=29661
export FLEET_PORT=29662
mkdir -p "$HOME/.cache/pursers-media-demo"

PYTHONPATH=packages/client/src:packages/central/src \
  python3 tools/media/record_demo.py prepare \
  --root "$DEMO_ROOT" --port "$CENTRAL_PORT"

PYTHONPATH=packages/client/src:packages/central/src \
  python3 -m pursers_central run "$DEMO_ROOT"
```

Seed the empty board and its three seats once Central is healthy:

```sh
PYTHONPATH=packages/client/src:packages/central/src \
  python3 tools/media/record_demo.py drive \
  --root "$DEMO_ROOT" --scenario seed
```

In a second terminal, start Fleet with the disposable coordinator token file.
The token value never appears in the command or dashboard.

```sh
PYTHONPATH=packages/client/src \
  python3 tools/fleet-dashboard/fleet_dashboard.py \
  --port "$FLEET_PORT" \
  --url "http://127.0.0.1:$CENTRAL_PORT/mcp" \
  --token-file "$DEMO_ROOT/coordinator.jwt" \
  --home-board media-demo \
  --cache-seconds 0.25 \
  --agent-name fleet-dashboard-session-media-demo
```

Open `http://127.0.0.1:29662/#/boards/media-demo`, then run one scenario while
recording. `--delay` is the hold time between genuine state changes.

```sh
PYTHONPATH=packages/client/src:packages/central/src \
  python3 tools/media/record_demo.py drive \
  --root "$DEMO_ROOT" --scenario hero --delay 2

PYTHONPATH=packages/client/src:packages/central/src \
  python3 tools/media/record_demo.py drive \
  --root "$DEMO_ROOT" --scenario wake --delay 2

PYTHONPATH=packages/client/src:packages/central/src \
  python3 tools/media/record_demo.py drive \
  --root "$DEMO_ROOT" --scenario reject-fix --delay 2
```

Each command prints only credential-free JSON cues. `wake` runs the production
wait bridge's `_a2a_wait_impl` over the real MCP resource subscription, prints
`wake-waiting`, creates the ticket, and prints `wake-returned` only after the
push event arrives.

For a determinism check, repeat the entire procedure with a fresh `run-2`
directory and different loopback ports. Compare the ordered `step`,
`ticket_id`, `event_kind`, and `transport` values; credential material and
generated principal identifiers are deliberately excluded from stdout.

## Capture and encode

The checked-in files were captured from Ego Lite Chromium at a 1200 × 750
viewport by taking PNG frames of the real Fleet page during each scripted
delay. The hero sequence keeps one chronological frame for each visible real
state: offered, claimed with its active lease, submitted with evidence,
reviewing with the review lease, and approved. The exact browser capture calls
were:

```text
page.cdp("Emulation.setDeviceMetricsOverride", {width:1200,height:750,deviceScaleFactor:1,mobile:false})
page.screenshot({path:"/PATH/TO/FRAMES/frame-NNN.png"})
```

The frame sequences were encoded with FFmpeg using these exact flags:

```sh
ffmpeg -y -framerate 0.5 -i frame-%03d.png \
  -vf "fps=12,scale=1200:-2:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 ticket-flow.gif

ffmpeg -y -framerate 0.5 -i frame-%03d.png \
  -vf "fps=24,scale=1200:-2:flags=lanczos,format=yuv420p" \
  -c:v libx264 -movflags +faststart -an ticket-flow.mp4

ffmpeg -y -framerate 0.5 -i frame-%03d.png \
  -vf "fps=24,scale=1200:-2:flags=lanczos" \
  -c:v libvpx-vp9 -crf 36 -b:v 0 -an ticket-flow.webm
```

Use the same GIF command for `wake-dont-poll.gif`. Keep browser chrome,
terminal paths, tokens, and unrelated tabs outside the viewport. Inspect every
frame before committing and reject any capture containing a home path,
credential, real board name, or real ticket text.
