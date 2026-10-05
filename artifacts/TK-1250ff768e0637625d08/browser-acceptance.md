# TK-1250ff768e0637625d08 browser acceptance

This evidence uses only the hermetic, public-safe Fleet fixture. The configured
Automation policy contains ten generic synthetic template identifiers, one
synthetic connector, realistic capacity and budget values, and every existing
policy action.

## Reproduction on the unmodified 5.2.0 base

At `1b1b66618dcd68e803fd7d183a42538332576a7d`, Chromium at a 768 × 900
viewport, 200% CSS zoom, and stable scrollbar gutter reproduced the live
geometry:

- Settings root: `clientWidth=349`, `scrollWidth=352`;
- toolbar: `347/347`;
- section navigation: `349/349`;
- elements outside the root: `.section-title`, `.autonomous-state`,
  `.autonomous-config-form`, and `.settings-command-row`.

The failing browser command exited non-zero at
`zoom-200-light-simple`. This established that the populated fixture detects
the reported regression before the CSS fix.

## Corrected result

The final browser gate passed all 24 width/theme/mode cases. Every 200% zoom
case reported:

- Settings root: `clientWidth=349`, `scrollWidth=349`;
- toolbar: `347/347`;
- section navigation: `349/349`;
- no outside elements, control overlaps, circular navigation, or clipped
  actions;
- one configured Automation policy card and all ten approved templates; and
- preserved scope, search, Simple/Advanced switching, reload, dirty value, and
  focused-field behavior.

## Visual inspection

The focused screenshots were inspected at original resolution. In both themes,
the policy heading and state remain aligned, long template and connector text
wraps inside the card, all fields remain readable, and Save, Reconcile, Kill,
and Resume remain visible without overlap or clipping.

- [Light theme, Advanced mode](browser/zoom-200-light-advanced-automation.png)
  — SHA-256 `782a26fe1037c435ebcf69cbadf5a82b28b497f5468927cc311af0eb7accf7d6`
- [Dark theme, Advanced mode](browser/zoom-200-dark-advanced-automation.png)
  — SHA-256 `e5986a24ef7a86207edffda4d1ff92fc3254ab8c55f13aaeba03544150149cfb`

These worker-produced fixtures and screenshots are regression evidence, not a
claim of final verifier-owned live-browser acceptance.
