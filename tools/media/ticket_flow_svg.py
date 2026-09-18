#!/usr/bin/env python3
"""Generate the animated ticket-flow SVGs used by the README and pursers.app.

GitHub renders an SVG inside <img> with its own CSS animations, but a
prefers-color-scheme query inside the SVG follows the operating system, not the
GitHub theme. So this writes one light and one dark file with fixed colors; the
README picks between them with <picture><source media="(prefers-color-scheme: dark)">,
which GitHub maps to the viewer's GitHub theme.

    python3 tools/media/ticket_flow_svg.py          # write both files
    python3 tools/media/ticket_flow_svg.py --check  # fail if they are stale
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media"

PALETTES = {
    "light": dict(fg="#1f2328", muted="#59636e", line="#d1d9e0", card="#f6f8fa",
                  accent="#0969da", ok="#1a7f37", bad="#cf222e", node="#ffffff"),
    "dark": dict(fg="#f0f6fc", muted="#9198a1", line="#3d444d", card="#151b23",
                 accent="#4493f8", ok="#3fb950", bad="#f85149", node="#0d1117"),
}

TEMPLATE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 300" width="820" height="300" role="img" aria-labelledby="t d">\n  <title id="t">How a ticket moves on a Pursers board</title>\n  <desc id="d">A coordinator creates a ticket; the board offers it and the waiting worker wakes; the worker claims it under a lease and submits evidence; an independent reviewer rejects it once with fixes, the worker resubmits, the reviewer approves, and the work is ready for you to merge.</desc>\n  <style>\n    {palette}\n    text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: var(--fg); }}\n    .muted {{ fill: var(--muted); }}\n    .label {{ font-size: 13px; font-weight: 600; text-anchor: middle; }}\n    .glyph {{ font-size: 15px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }}\n    .node {{ fill: var(--node); stroke: var(--line); stroke-width: 2; }}\n    .rail {{ stroke: var(--line); stroke-width: 2; }}\n    .cap {{ font-size: 15px; font-weight: 600; text-anchor: middle; opacity: 0; }}\n\n    .anim {{ animation-duration: 12s; animation-iteration-count: infinite; animation-fill-mode: both; }}\n\n    /* The ticket card travels between stations. */\n    .card {{ animation-name: card; animation-timing-function: var(--ease-move); }}\n    @keyframes card {{\n      0%   {{ opacity: 0; transform: translate(20px, 60px) scale(0.96); animation-timing-function: var(--ease-out); }}\n      4%   {{ opacity: 1; transform: translate(20px, 60px) scale(1); }}\n      8%   {{ transform: translate(20px, 60px); }}\n      15%  {{ transform: translate(185px, 60px); }}\n      20%  {{ transform: translate(185px, 60px); }}\n      27%  {{ transform: translate(350px, 60px); }}\n      40%  {{ transform: translate(350px, 60px); }}\n      47%  {{ transform: translate(515px, 60px); }}\n      54%  {{ transform: translate(515px, 60px); }}\n      60%  {{ transform: translate(350px, 60px); }}\n      66%  {{ transform: translate(350px, 60px); }}\n      72%  {{ transform: translate(515px, 60px); }}\n      80%  {{ transform: translate(515px, 60px); }}\n      88%  {{ transform: translate(680px, 60px); opacity: 1; }}\n      96%  {{ transform: translate(680px, 60px); opacity: 1; }}\n      100% {{ transform: translate(680px, 60px); opacity: 0; }}\n    }}\n\n    /* Evidence appears once the worker has built something. */\n    .evidence {{ animation-name: evidence; animation-timing-function: var(--ease-out); }}\n    @keyframes evidence {{ 0%, 30% {{ opacity: 0; }} 34%, 100% {{ opacity: 1; }} }}\n\n    /* Card border turns green on approval. */\n    .approved {{ animation-name: approved; }}\n    @keyframes approved {{ 0%, 76% {{ opacity: 0; }} 79%, 100% {{ opacity: 1; }} }}\n\n    /* Waiting worker wakes when the board offers the ticket. */\n    .wake {{ animation-name: wake; animation-timing-function: var(--ease-out); transform-box: fill-box; transform-origin: center; }}\n    @keyframes wake {{\n      0%, 11% {{ opacity: 0; transform: scale(0.9); }}\n      14% {{ opacity: 1; transform: scale(1.15); }}\n      20%, 100% {{ opacity: 0; transform: scale(1.3); }}\n    }}\n\n    /* Lease ring around the worker while it holds the claim. */\n    .lease {{ animation-name: lease; animation-timing-function: linear; transform-box: fill-box; transform-origin: center; }}\n    @keyframes lease {{\n      0%, 25% {{ opacity: 0; transform: rotate(0deg); }}\n      28% {{ opacity: 1; }}\n      44% {{ opacity: 1; transform: rotate(360deg); }}\n      47% {{ opacity: 0; transform: rotate(400deg); }}\n      60% {{ opacity: 0; transform: rotate(400deg); }}\n      63% {{ opacity: 1; }}\n      70% {{ opacity: 1; transform: rotate(720deg); }}\n      73%, 100% {{ opacity: 0; transform: rotate(760deg); }}\n    }}\n\n    /* Reviewer verdicts. */\n    .reject {{ animation-name: reject; animation-timing-function: var(--ease-out); transform-box: fill-box; transform-origin: center; }}\n    @keyframes reject {{ 0%, 49% {{ opacity: 0; transform: scale(0.9); }} 51%, 57% {{ opacity: 1; transform: scale(1); }} 60%, 100% {{ opacity: 0; transform: scale(1); }} }}\n    .approve {{ animation-name: approve; animation-timing-function: var(--ease-out); transform-box: fill-box; transform-origin: center; }}\n    @keyframes approve {{ 0%, 75% {{ opacity: 0; transform: scale(0.9); }} 77%, 86% {{ opacity: 1; transform: scale(1); }} 90%, 100% {{ opacity: 0; transform: scale(1); }} }}\n\n    /* You: the merge. */\n    .merge {{ animation-name: merge; animation-timing-function: var(--ease-out); transform-box: fill-box; transform-origin: center; }}\n    @keyframes merge {{ 0%, 86% {{ opacity: 0; transform: scale(0.9); }} 90%, 96% {{ opacity: 1; transform: scale(1.15); }} 100% {{ opacity: 0; transform: scale(1.3); }} }}\n\n    /* Captions, one per stage. */\n    .c1 {{ animation-name: c1; }} @keyframes c1 {{ 0% {{ opacity: 0; }} 2%, 7% {{ opacity: 1; }} 9%, 100% {{ opacity: 0; }} }}\n    .c2 {{ animation-name: c2; }} @keyframes c2 {{ 0%, 8% {{ opacity: 0; }} 10%, 19% {{ opacity: 1; }} 21%, 100% {{ opacity: 0; }} }}\n    .c3 {{ animation-name: c3; }} @keyframes c3 {{ 0%, 20% {{ opacity: 0; }} 22%, 39% {{ opacity: 1; }} 41%, 100% {{ opacity: 0; }} }}\n    .c4 {{ animation-name: c4; }} @keyframes c4 {{ 0%, 40% {{ opacity: 0; }} 42%, 49% {{ opacity: 1; }} 51%, 100% {{ opacity: 0; }} }}\n    .c5 {{ animation-name: c5; }} @keyframes c5 {{ 0%, 50% {{ opacity: 0; }} 52%, 65% {{ opacity: 1; }} 67%, 100% {{ opacity: 0; }} }}\n    .c6 {{ animation-name: c6; }} @keyframes c6 {{ 0%, 66% {{ opacity: 0; }} 68%, 81% {{ opacity: 1; }} 83%, 100% {{ opacity: 0; }} }}\n    .c7 {{ animation-name: c7; }} @keyframes c7 {{ 0%, 82% {{ opacity: 0; }} 84%, 97% {{ opacity: 1; }} 100% {{ opacity: 0; }} }}\n\n    .static {{ opacity: 0; }}\n\n    @media (prefers-reduced-motion: reduce) {{\n      .anim {{ animation: none; }}\n      .card, .cap, .wake, .lease, .reject, .approve, .merge, .approved {{ opacity: 0; }}\n      .static {{ opacity: 1; }}\n    }}\n  </style>\n\n  <!-- Rail between stations -->\n  <line class="rail" x1="80" y1="220" x2="740" y2="220"/>\n  <g class="muted" fill="none">\n    <path d="M155 214 l8 6 l-8 6" stroke="var(--line)" stroke-width="2"/>\n    <path d="M320 214 l8 6 l-8 6" stroke="var(--line)" stroke-width="2"/>\n    <path d="M485 214 l8 6 l-8 6" stroke="var(--line)" stroke-width="2"/>\n    <path d="M650 214 l8 6 l-8 6" stroke="var(--line)" stroke-width="2"/>\n  </g>\n\n  <!-- Stations -->\n  <g>\n    <circle class="node" cx="80" cy="220" r="22"/><text class="glyph" x="80" y="220">C</text>\n    <text class="label" x="80" y="262">Coordinator</text>\n\n    <circle class="node" cx="245" cy="220" r="22"/><text class="glyph" x="245" y="220">B</text>\n    <text class="label" x="245" y="262">Board</text>\n\n    <circle class="wake anim" cx="410" cy="220" r="30" fill="none" stroke="var(--accent)" stroke-width="3"/>\n    <circle class="lease anim" cx="410" cy="220" r="30" fill="none" stroke="var(--accent)" stroke-width="3" stroke-dasharray="36 12" stroke-linecap="round"/>\n    <circle class="node" cx="410" cy="220" r="22"/><text class="glyph" x="410" y="220">W</text>\n    <text class="label" x="410" y="262">Worker</text>\n\n    <circle class="node" cx="575" cy="220" r="22"/><text class="glyph" x="575" y="220">R</text>\n    <text class="label" x="575" y="262">Reviewer</text>\n    <g class="reject anim">\n      <circle cx="601" cy="196" r="11" fill="var(--bad)"/>\n      <path d="M596 191 l10 10 M606 191 l-10 10" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>\n    </g>\n    <g class="approve anim">\n      <circle cx="601" cy="196" r="11" fill="var(--ok)"/>\n      <path d="M595.5 196.5 l4 4 l7.5 -8" stroke="#fff" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>\n    </g>\n\n    <circle class="merge anim" cx="740" cy="220" r="30" fill="none" stroke="var(--ok)" stroke-width="3"/>\n    <circle class="node" cx="740" cy="220" r="22"/><text class="glyph" x="740" y="220">Y</text>\n    <text class="label" x="740" y="262">You</text>\n  </g>\n\n  <!-- The ticket card -->\n  <g class="card anim">\n    <rect x="0" y="0" width="120" height="96" rx="10" fill="var(--card)" stroke="var(--line)" stroke-width="1.5"/>\n    <rect class="approved anim" x="0" y="0" width="120" height="96" rx="10" fill="none" stroke="var(--ok)" stroke-width="2.5"/>\n    <text x="12" y="22" font-size="11" class="muted">TK-042</text>\n    <text x="12" y="41" font-size="13" font-weight="600">Add sign-in</text>\n    <g class="evidence anim">\n      <text x="12" y="62" font-size="11" class="muted">commit ✓  files ✓</text>\n      <text x="12" y="80" font-size="11" class="muted">tests ✓</text>\n    </g>\n  </g>\n\n  <!-- Captions -->\n  <text class="cap c1 anim" x="410" y="30">The coordinator turns your intent into a ticket</text>\n  <text class="cap c2 anim" x="410" y="30">The board offers it — the waiting worker wakes</text>\n  <text class="cap c3 anim" x="410" y="30">The worker claims it under a lease and builds</text>\n  <text class="cap c4 anim" x="410" y="30">Evidence goes to an independent reviewer</text>\n  <text class="cap c5 anim" x="410" y="30">Rejected with fixes — back to the same worker</text>\n  <text class="cap c6 anim" x="410" y="30">Resubmitted and approved on evidence</text>\n  <text class="cap c7 anim" x="410" y="30">Ready to merge. The final call is yours.</text>\n  <text class="cap static" x="410" y="30">offer → claim → evidence → independent review → merge</text>\n</svg>\n'


def render(theme: str) -> str:
    p = PALETTES[theme]
    palette = (
        ":root {{ --fg: {fg}; --muted: {muted}; --line: {line}; --card: {card}; "
        "--accent: {accent}; --ok: {ok}; --bad: {bad}; --node: {node}; "
        "--ease-move: cubic-bezier(0.77, 0, 0.175, 1); "
        "--ease-out: cubic-bezier(0.23, 1, 0.32, 1); }}"
    ).format(**p)
    return TEMPLATE.format(palette=palette)


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    stale = []
    for theme in PALETTES:
        path = OUT / f"ticket-flow-{theme}.svg"
        content = render(theme)
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(path.name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if stale:
        print("stale: " + ", ".join(stale) + "; run python3 tools/media/ticket_flow_svg.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
