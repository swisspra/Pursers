#!/usr/bin/env python3
"""Generate the animated prompt-cache SVGs used by the README.

The numbers are the measured ones in docs/evidence/cache-efficiency.md. Like the
ticket-flow SVGs, this writes fixed-color light and dark files; the README picks
one with <picture><source media="(prefers-color-scheme: dark)">.

    python3 tools/media/cache_hit_svg.py          # write both files
    python3 tools/media/cache_hit_svg.py --check  # fail if they are stale
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media"

PALETTES = {
    "light": dict(fg="#1f2328", muted="#59636e", track="#eff2f5", line="#d1d9e0",
                  cached="#1a7f37", paid="#bf8700"),
    "dark": dict(fg="#f0f6fc", muted="#9198a1", track="#212830", line="#3d444d",
                 cached="#3fb950", paid="#d29922"),
}

# (vendor, detail, cache share) — docs/evidence/cache-efficiency.md
ROWS = (
    ("OpenAI Codex fleet", "13 Aug · 294M tokens", 97.6),
    ("OpenAI Codex fleet", "14 Aug · 210M tokens", 97.5),
    ("OpenAI Codex fleet", "15 Aug · 998M tokens", 97.2),
    ("Anthropic Claude", "operator seat · shipped 5.0.0", 98.8),
)

BAR_X, BAR_W, BAR_H = 250, 460, 18
ROW_Y0, ROW_GAP = 104, 50
VENDOR_GAP = 20  # extra space before the Claude row, so the vendors read apart
FILL_START, FILL_LEN, STAGGER = 6.0, 16.0, 3.0  # percent of the loop


def _row_y(index: int) -> int:
    return ROW_Y0 + index * ROW_GAP + (VENDOR_GAP if index == len(ROWS) - 1 else 0)


def _row(index: int, vendor: str, detail: str, share: float) -> tuple[str, str]:
    y = _row_y(index)
    cached_w = round(BAR_W * share / 100, 1)
    paid_w = round(BAR_W - cached_w, 1)
    start = FILL_START + index * STAGGER
    end = start + FILL_LEN
    css = (
        f"    .f{index} {{ animation-name: f{index}, dim; }}\n"
        f"    @keyframes f{index} {{ 0%, {start:g}% {{ transform: scaleX(0); animation-timing-function: var(--ease-out); }}"
        f" {end:g}%, 100% {{ transform: scaleX(1); }} }}\n"
        f"    .p{index} {{ animation-name: p{index}; }}\n"
        f"    @keyframes p{index} {{ 0%, {end - 6:g}% {{ opacity: 0; transform: translateX(-8px); animation-timing-function: var(--ease-out); }}"
        f" {end:g}%, 100% {{ opacity: 1; transform: translateX(0); }} }}\n"
    )
    svg = (
        f'  <text x="20" y="{y + 4}" font-size="13" font-weight="600">{vendor}</text>\n'
        f'  <text x="20" y="{y + 21}" font-size="11" class="muted">{detail}</text>\n'
        f'  <rect x="{BAR_X}" y="{y}" width="{BAR_W}" height="{BAR_H}" rx="4" fill="var(--track)"/>\n'
        f'  <rect class="paid anim" x="{BAR_X + cached_w}" y="{y}" width="{paid_w}" height="{BAR_H}" rx="2" fill="var(--paid)"/>\n'
        f'  <rect class="fill f{index} anim" x="{BAR_X}" y="{y}" width="{cached_w}" height="{BAR_H}" rx="4" fill="var(--cached)"/>\n'
        f'  <text class="pct p{index} anim" x="{BAR_X + BAR_W + 16}" y="{y + 14}" font-size="17" font-weight="700">{share:.1f}%</text>\n'
    )
    return css, svg


TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 350" width="820" height="350" role="img" aria-labelledby="t d">
  <title id="t">Prompt-cache hit rate while the fleet built Pursers</title>
  <desc id="d">{desc}</desc>
  <style>
    {palette}
    text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: var(--fg); }}
    .muted {{ fill: var(--muted); }}
    .anim {{ animation-duration: 10s; animation-iteration-count: infinite; animation-fill-mode: both; }}
    .fill {{ transform-box: fill-box; transform-origin: left center; }}
    .cap {{ font-size: 15px; font-weight: 600; opacity: 0; }}

    /* The whole chart fades out at the end so the reset is never seen. */
    .scene {{ animation-name: scene; }}
    @keyframes scene {{ 0% {{ opacity: 0; }} 3%, 93% {{ opacity: 1; }} 99%, 100% {{ opacity: 0; }} }}

    /* Each bar fills to its cache share, one after another. */
{row_css}
    /* Then the small slice paid at full price is called out. */
    .paid {{ animation-name: paid; }}
    @keyframes dim {{ 0%, 46% {{ opacity: 1; }} 52%, 100% {{ opacity: 0.4; }} }}
    @keyframes paid {{ 0%, 46% {{ opacity: 0; }} 50%, 100% {{ opacity: 1; }} }}
    .c1 {{ animation-name: c1; }} @keyframes c1 {{ 0%, 2% {{ opacity: 0; }} 5%, 43% {{ opacity: 1; }} 46%, 100% {{ opacity: 0; }} }}
    .c2 {{ animation-name: c2; }} @keyframes c2 {{ 0%, 47% {{ opacity: 0; }} 50%, 93% {{ opacity: 1; }} 97%, 100% {{ opacity: 0; }} }}

    .static {{ opacity: 0; }}

    @media (prefers-reduced-motion: reduce) {{
      .anim {{ animation: none; }}
      .cap {{ opacity: 0; }}
      .static {{ opacity: 1; }}
    }}
  </style>

  <g class="scene anim">
  <text x="20" y="34" font-size="19" font-weight="700">Prompt-cache hit rate while the fleet built Pursers</text>
  <text class="cap c1 anim" x="20" y="62">97–99% of every prompt was served from cache.</text>
  <text class="cap c2 anim" x="20" y="62">Only <tspan fill="var(--paid)">1–3%</tspan> of input was paid at full price. Across vendors.</text>
  <text class="cap static" x="20" y="62">97–99% of input served from cache; only 1–3% paid at full price.</text>

{row_svg}
  <line x1="20" y1="{divider_y}" x2="800" y2="{divider_y}" stroke="var(--line)" stroke-dasharray="3 5"/>
  <line x1="20" y1="{rule_y}" x2="800" y2="{rule_y}" stroke="var(--line)"/>
  <rect x="20" y="{key_y}" width="12" height="12" rx="2" fill="var(--cached)"/>
  <text x="38" y="{key_text_y}" font-size="12" class="muted">cached input</text>
  <rect x="130" y="{key_y}" width="12" height="12" rx="2" fill="var(--paid)"/>
  <text x="148" y="{key_text_y}" font-size="12" class="muted">uncached input</text>
  <text x="800" y="{key_text_y}" font-size="12" class="muted" text-anchor="end">Provider dashboards · docs/evidence/cache-efficiency.md</text>
  </g>
</svg>
"""


def render(theme: str) -> str:
    p = PALETTES[theme]
    palette = (
        ":root {{ --fg: {fg}; --muted: {muted}; --track: {track}; --line: {line}; "
        "--cached: {cached}; --paid: {paid}; "
        "--ease-out: cubic-bezier(0.23, 1, 0.32, 1); }}"
    ).format(**p)
    parts = [_row(i, *row) for i, row in enumerate(ROWS)]
    last_y = _row_y(len(ROWS) - 1)
    desc = "Share of input tokens served from prompt cache: " + "; ".join(
        f"{vendor}, {detail}: {share:.1f}%" for vendor, detail, share in ROWS
    ) + "."
    return TEMPLATE.format(
        palette=palette,
        desc=desc,
        row_css="".join(css for css, _ in parts),
        row_svg="".join(svg for _, svg in parts),
        divider_y=last_y - 20,
        rule_y=last_y + 44,
        key_y=last_y + 60,
        key_text_y=last_y + 71,
    )


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    stale = []
    for theme in PALETTES:
        path = OUT / f"cache-hit-{theme}.svg"
        content = render(theme)
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(path.name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if stale:
        print("stale: " + ", ".join(stale) + "; run python3 tools/media/cache_hit_svg.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
