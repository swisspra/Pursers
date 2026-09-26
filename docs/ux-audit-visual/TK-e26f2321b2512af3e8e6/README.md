# TK-e26f2321b2512af3e8e6 exact-UI browser evidence

This directory makes the rejected visual checks reviewable from Git rather than
from a worker-local path. The after images and browser observations were captured
from the exact UI revision `db974bf9e808137ddc346c128324981b7068984f` in Ego
task space `28`. The before images use the approved baseline
`f17bad906d4a6d036506118c0f3b2d6735418cde`. Both were served by the same
loopback-only synthetic-safe fixture.

The evidence-only child commit that contains this directory does not change the
tested UI files. Reviewers can verify that the blobs for
`tools/fleet-dashboard/ui/assets/app.js`,
`tools/fleet-dashboard/ui/assets/fleet.css`, and
`tools/fleet-dashboard/ui/index.html` are identical to the tested UI revision.

## Review map

- `before-light-1440x900.png` and `after-light-1440x900.png`: desktop comparison.
- `before-light-390x844.png` and `after-light-390x844.png`: narrow comparison.
- `after-dark-1440x900.png` and `after-dark-390x844.png`: dark and compact controls.
- `after-dark-error-1440x900.png`: real product error/reconnection treatment from
  an independently seeded unavailable board-detail response.
- `evidence-manifest.json`: route, loading/empty/error, keyboard, 200% zoom,
  light/dark, density, reduced-motion, font-load, overflow, dimensions, and
  screenshot digest results.

No credentials, personal data, operator paths, or live board data are present in
the screenshots or manifest.
