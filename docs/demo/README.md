# Recording guide

Use this guide with [the timed demo script](DEMO-SCRIPT.md). The target is a
clean 1440 × 900, light-theme recording with readable product state and no
credential or personal-data exposure.

## Before recording

1. Create a fresh macOS user or a dedicated recording profile. Disable desktop
   widgets, notification banners, chat previews, password-manager prompts, and
   cloud-drive overlays.
2. Set the display or canvas to 1440 × 900. Use 100% browser zoom and a terminal
   font large enough to read at 720p playback.
3. Switch Claude Desktop, Codex, AionUi, Fleet Dashboard, browser, and terminal
   to light theme. Use one neutral wallpaper and hide the Dock if it causes
   layout shifts.
4. Stage only throwaway data and fresh loopback ports. Use `/PATH/TO/DEMO` in
   visible text. Never show tokens, door strings, personal paths, usernames,
   emails, Git credentials, shell history, environment dumps, or real ticket
   data.
5. Close unrelated apps and browser tabs. Turn off autocomplete and recent-file
   menus. Clear terminal scrollback after staging.
6. Confirm the submitting worker and reviewer expose different
   `principal_id` values. Confirm the AionUi shot is the managed installed
   extension, not the synthetic rehearsal server.

## QuickTime Player

QuickTime is the simplest choice for a single-display take.

1. Open a new screen recording and select the 1440 × 900 display or a fixed
   1440 × 900 region.
2. Choose the intended microphone. Disable mouse-click visualization unless the
   pointer is difficult to follow.
3. Record a five-second room-tone lead, perform the script, and leave five
   seconds after the Fleet closing frame.
4. Save a lossless master before trimming. Make cuts on duplicates; retain the
   untouched master until the release is published.

QuickTime does not provide a scene stack. Prepare every window position before
the take and use deliberate cuts between hosts; do not drag credential-bearing
settings across the screen.

## OBS Studio

OBS is preferable for repeatable crops and the side-by-side host scene.

- Canvas: 1440 × 900.
- Output: 1440 × 900, 30 fps, constant frame rate.
- Sources: one display or window capture per product; crop each source so menu
  bars, notifications, and private paths remain outside the frame.
- Scenes: `Title`, `Central`, `Two hosts`, `Worker`, `Reviewer`, `AionUi`, and
  `Fleet`. Keep transitions short and plain.
- Audio: one mono microphone track with peaks below clipping. Record system
  audio only when it adds information.
- Master: a high-quality local recording. Export the delivery copy from the
  master instead of recording a compressed preview.

Lock sources after composing them. During rehearsal, verify that switching to
`Reviewer` cannot reveal the worker credential and that browser autofill never
appears over AionUi helper fields.

## Framing and pacing

- Hold each important state for at least two seconds: Central healthy, board ID,
  claimed lease, submitted evidence, independent approval, decision annotation,
  AionUi approved result, and Fleet overview.
- Keep the pointer still while narration explains a state. Move it only to lead
  the viewer to the next label.
- Prefer cuts over fast scrolling. If a response is long, collapse it to the
  fields named in the script before recording.
- Show loopback origins when they establish local-only behavior, but crop all
  token-file paths. A plain `/PATH/TO/DEMO` placeholder is acceptable.
- Read the narration conversationally at roughly 125–140 words per minute. Do
  not add superlatives, competitive claims, or security claims beyond what the
  visible product state demonstrates.

## Privacy and evidence check

Review the master frame by frame at every terminal, settings, and host switch.
Reject the take if any frame contains:

- a JWT-like three-part value, a door string, secret, API key, or auth header;
- a personal filesystem path, username, email, avatar, account switcher, or
  notification;
- an abbreviated or invented source reference presented as verified evidence;
- a reviewer with the same principal as the submitting worker;
- synthetic fixture data presented as a live product result; or
- a label that is not present in the candidate UI.

Keep the eight screenshot frames listed in the demo script alongside the video
master. Record the candidate commit, final video checksum, duration, resolution,
and capture date in the private production log; do not add operator identity or
credential metadata to the repository.

## Final playback checklist

- Duration is between 3:00 and 4:00; the target script is 3:35.
- Frame size is exactly 1440 × 900 and every product is in light theme.
- Spoken narration matches the visible action and contains no hype.
- Claude Desktop and Codex join the same board through distinct seats.
- Offer, claim, lease, submit, independent review, and coordinator decision are
  visible in order.
- Pursers Home is described as bounded and does not appear to execute agent-only
  lifecycle actions.
- Fleet Dashboard is the closing frame.
- The 30-second cut remains understandable without the long narration.
- A second person performs the privacy review before upload.
