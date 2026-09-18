# QoderWake vs Pursers: landing and showcase structure

Research date: 2026-09-15. Scope: public presentation design only—section
order, hero, screenshot use, copy rhythm, and CTA placement. Third-party
intake, governance, permissions, and memory-policy design are intentionally
excluded.

## Evidence and limits

The primary source is the public [QoderWake landing page](https://qoder.com/en/qoderwake).
Ego Lite captured that page and the seven public product pages linked directly
from its footer at 1440×900 and 400×844 CSS-pixel viewports. All 16 captures
are full-page PNGs in [`qoderwake-ux/landing-and-showcase/`](qoderwake-ux/landing-and-showcase/).
[`capture-manifest.tsv`](qoderwake-ux/landing-and-showcase/capture-manifest.tsv)
records the final URL, viewport, full PNG dimensions, UTC timestamp, rendered
theme, and SHA-256 for every file; [`SHA256SUMS`](qoderwake-ux/landing-and-showcase/SHA256SUMS)
is the directly checkable checksum manifest.

The rendered controls contained no light/dark or appearance toggle on any of
the eight pages. The site produced the same light presentation even though the
browser environment preferred dark, so there is no site-offered dark variant
to capture. The result is visible in both QoderWake captures:
[`1440px`](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png)
and [`400px`](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png).

Static PNGs establish layout, hierarchy, colour relationships, typography,
responsive stacking, and the state visible at capture time. Animation timing,
easing, hover treatment, and transitions between states are **not observable**
in these still renders. Neither QoderWake capture shows a dedicated loading,
error, or empty-state screen, so those states are also **not observable** from
the evidence rather than assumed absent
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

Pursers comparisons use the current repository and its real, synthetic-data
screenshots:

- [`01-fleet-overview.png`](../showcase/01-fleet-overview.png)
- [`02-personal-today.png`](../showcase/02-personal-today.png)
- [`03-personal-work.png`](../showcase/03-personal-work.png)
- [`04-personal-agents.png`](../showcase/04-personal-agents.png)
- [`05-aionui-join.png`](../showcase/05-aionui-join.png)
- [`06-aionui-offer-claim.png`](../showcase/06-aionui-offer-claim.png)
- [`07-aionui-status.png`](../showcase/07-aionui-status.png)

## What the QoderWake page actually shows

### 1. Global shell, then one direct promise

The desktop shell is a thin pale-green campaign strip over a white navigation
bar. The Qoder mark and six navigation groups sit left-to-centre; locale,
language, sign-in, and a rounded Download action sit on the right. At 400px,
the navigation collapses to the mark and a menu icon while the campaign strip
remains. The page uses a rounded sans-serif display face, heavy dark headings,
small grey body copy, generous white space, pale-grey section grounds, and a
muted forest-green primary action
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

The mobile hero makes the hierarchy especially literal: “QoderWake”, then
“Autonomous AI Employees, On the Job”, one short explanatory paragraph, two
side-by-side download actions, and a product screenshot. Desktop centres the
same promise and actions above a much larger product frame. The product frame
shows a left rail, a workspace headed around waking an AI team, assigned people
with role/model/status columns, connected chat integrations, and a recent-
activity timeline. White panels, hairline dividers, small green state markers,
and a soft shadow make it read as a real product rather than an illustration
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

### 2. Recognizable outcomes before mechanism

“Meet AI Employees That Truly Own the Work” follows the hero. Desktop uses one
row of three equally weighted image cards with a heading and a short paragraph
under each; mobile turns that row into three full-width blocks. The pictures
show a chat-summary response, an automation schedule, and a multi-person
workspace. Their mint, pale-green, and grey image grounds vary just enough to
separate the examples while retaining one restrained palette
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

This order matters: the page spends its first screen proving that there is a
product, then spends the next section showing three jobs. Technical vocabulary
does not lead the story. At desktop width the three cards can be scanned as a
set; at 400px each image stays above its matching label and body copy, so the
reading order remains unambiguous
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

### 3. Mechanism becomes a visual mosaic

“How QoderWake Works” introduces a desktop mosaic: a wide engineering card, a
tall illustrated employee badge, a green skills card, and a grey workflow
card. The uneven grid creates a change in rhythm after the regular three-card
row. On mobile the mosaic becomes a simple vertical sequence: engineering,
employee badge, skills, and workflow. Each block keeps its own image and copy
together, with no horizontal scroll
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

The visible interface language inside the hero uses compact operational terms
such as online, active, connected, assigned, and recent activity. These words
sit next to names, roles, integrations, and timeline events rather than being
presented as abstract feature claims. No dedicated loading, disconnected,
failure, or no-results panel is pictured in this sequence
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

### 4. Proof grid, then a quiet footer CTA

“Built for Real Work, Not for Demos” changes the desktop layout again: four
claims occupy the corners of a dashed two-by-two grid around a dotted centre
motif. At 400px, the motif comes first and the four claims become one vertical
list separated by dashed rules. The footer then returns to a large mark and
short brand line on the left with compact product/resource columns; on mobile,
those columns become a two-column list under the brand block
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

The complete narrative is therefore promise → real product frame → three
outcomes → mechanism mosaic → credibility grid → product-family footer. The
primary CTA appears in the global navigation and hero; the close is visually
quieter and relies on the footer's product links rather than another large
banner
([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
[400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).

## What the directly linked product pages add

The direct product links were treated as presentation references, not as
claims about capabilities. They show that Qoder reuses a family structure while
changing the hero artifact to match each product.

| Page | Literal desktop presentation | 400px response | Evidence |
|---|---|---|---|
| Qoder | Centred outcome headline and two download actions lead into a very large desktop-app screenshot; a three-card product section and a wide proactive-agent panel follow. | The hero, screenshot, feature cards, mascot panel, metrics, and connector marks become one vertical reading stream. | [1440px](qoderwake-ux/landing-and-showcase/qoder__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/qoder__400x844__site-default.png) |
| Mobile | A left-aligned introduction leads into a split rounded card: phone imagery on the left and platform actions on the right; a four-column control list and a glasses panel follow. | Image precedes copy and platform buttons; the four control items stack; the glasses image moves below its copy. | [1440px](qoderwake-ux/landing-and-showcase/mobile__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/mobile__400x844__site-default.png) |
| IDE | A product screenshot and download selector share one large split card, followed by four engineering-capability columns. | Screenshot, title, explanation, and platform selector stack inside the card; capability columns become four bordered rows. | [1440px](qoderwake-ux/landing-and-showcase/ide__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/ide__400x844__site-default.png) |
| JetBrains | The same split-card grammar pairs an IDE screenshot with one browser CTA; four backend-intelligence claims sit in a dashed horizontal grid. | Screenshot comes first; CTA remains full width; the four claims become a single dashed vertical list. | [1440px](qoderwake-ux/landing-and-showcase/jetbrains__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/jetbrains__400x844__site-default.png) |
| CLI | Terminal screenshot and install commands share a split hero; metrics, four claims, and an SDK panel follow. | Screenshot, install commands, four claims, and SDK installers stack without losing their labels. | [1440px](qoderwake-ux/landing-and-showcase/cli__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/cli__400x844__site-default.png) |
| Agent SDK | A centred statement leads into a large three-column comparison table, then capability grids, ecosystem cards, steps, and scenario cards. | Most later grids stack, but the comparison table remains wider than the 400px viewport and is visibly clipped rather than reformatted. | [1440px](qoderwake-ux/landing-and-showcase/agent-sdk__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/agent-sdk__400x844__site-default.png) |
| Cloud Agents | A centred two-line promise and three CTAs lead into a large system diagram; comparison rows and two four-item grids follow. | The system diagram and later claim grids stack, but the “Integrate in minutes. Ship in hours.” comparison table remains horizontally clipped: the desktop “BUILD IT YOURSELF” column is omitted while the row labels and “CLOUD AGENTS” column remain visible. | [1440px](qoderwake-ux/landing-and-showcase/cloud-agents__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/cloud-agents__400x844__site-default.png) |

Across these pages, the repeated visual grammar is an off-white shell, dark
rounded sans-serif headings, pale-grey card grounds, one product-specific
green/teal/blue accent, generous desktop spacing, and mostly faithful mobile
stacking. The Agent SDK and Cloud Agents comparison tables are both visibly
clipped at 400px
([Qoder 1440px](qoderwake-ux/landing-and-showcase/qoder__1440x900__site-default.png),
[IDE 1440px](qoderwake-ux/landing-and-showcase/ide__1440x900__site-default.png),
[Agent SDK 400px](qoderwake-ux/landing-and-showcase/agent-sdk__400x844__site-default.png),
[Cloud Agents 400px](qoderwake-ux/landing-and-showcase/cloud-agents__400x844__site-default.png)).

## Side-by-side with Pursers today

| First-impression job | QoderWake | Pursers today | Consequence |
|---|---|---|---|
| Say what it is | One product name, one outcome headline, and one short explanation appear before the product frame ([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)). | `website/` says “One governed board for every agent,” while the real product views lead with system labels and state ([Fleet](../showcase/01-fleet-overview.png), [AionUi](../showcase/06-aionui-offer-claim.png)). | Pursers is accurate, but a stranger meets “board” and “fleet” before seeing one task move. |
| Show the product | A large, legible product screenshot immediately follows the hero copy ([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png)). | Pursers already has strong captures of queue health, personal work, and ticket action, but the website hero uses a constructed ledger instead of one of them ([Fleet](../showcase/01-fleet-overview.png), [Personal](../showcase/02-personal-today.png), [AionUi](../showcase/06-aionui-offer-claim.png), `website/index.html`, since moved to the Pursers-WebApp repository). | The available proof is stronger than the current first screen. |
| Establish rhythm | Three pictured outcomes precede the technical mosaic ([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png)). | Fleet, Personal, and AionUi already form a real coordination sequence, but the screenshots live as a gallery rather than one narrated path ([Fleet](../showcase/01-fleet-overview.png), [Personal](../showcase/03-personal-work.png), [AionUi](../showcase/06-aionui-offer-claim.png)). | Reordering and captions can create a 60-second story without fabricating UI. |
| Explain status | Names, roles, statuses, integrations, and activity share one product frame ([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png)). | Fleet shows capacity and attention; Personal shows owned work and agents; AionUi shows connected/open/claimed/submitted states ([Fleet](../showcase/01-fleet-overview.png), [Personal agents](../showcase/04-personal-agents.png), [AionUi status](../showcase/07-aionui-status.png)). | Pursers needs one plain-language legend connecting machine state to user meaning. |
| Responsive proof | The three outcome cards, mosaic, proof grid, and footer all stack into one readable 400px stream ([400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)). | Existing Pursers captures are desktop-oriented; their content is real, but a marketing treatment must supply a deliberate narrow-width caption and zoom path ([Fleet](../showcase/01-fleet-overview.png), [AionUi](../showcase/06-aionui-offer-claim.png)). | Treat mobile framing as part of the story, not as a scaled-down desktop gallery. |
| Empty/disconnected proof | No dedicated empty, loading, error, or disconnected screen appears in the captures ([1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png), [400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)). | Pursers has an honest join/disconnected view and visible no-work messaging ([Join](../showcase/05-aionui-join.png), [Fleet](../showcase/01-fleet-overview.png)). | Pursers can turn operational honesty into a trust advantage. |

## Ranked proposals

Ranking balances first-impression impact against implementation effort. “Now”
means a bounded beta.2 content/layout change using existing assets; it does not
authorize implementation in this research ticket.

### 1. Put a real product frame in the website hero — impact high, size S, now

Use [`06-aionui-offer-claim.png`](../showcase/06-aionui-offer-claim.png) as the
first proof because it shows board-backed work, a claim action, and persisted
ticket states in one real surface. Keep the current ledger as a compact
explanation below it.

```text
┌──────────────────────────┬──────────────────────────┐
│ Keep every AI agent on   │ [REAL AionUi ticket      │
│ the same verified board. │  screenshot + caption]   │
│ [See the workflow] [Try] │ offer → claim → review   │
└──────────────────────────┴──────────────────────────┘
```

- Component/files: `website/index.html`, `website/style.css`, existing capture
  `docs/showcase/06-aionui-offer-claim.png` or a copied web-served asset.
- Acceptance: at 1440px, headline, CTA, and meaningful screenshot region are
  visible without scrolling; at 400px, copy precedes an uncropped, zoomable
  image; keyboard focus is visible; light and dark themes keep caption contrast.

### 2. Replace concept-first order with a 60-second story — impact high, size S, now

```text
Promise → Live work → Offer/Claim/Review → Fleet proof → Setup → Boundary → CTA
```

- Component/files: `website/index.html`; mirror the heading order in `README.md`
  in a separately approved change.
- Acceptance: at 400px and 1440px, a first-time reader can answer what, who,
  how, proof, and next step from headings alone; Tab order matches visual order;
  light and dark themes do not reorder or hide content.

### 3. Use a three-frame coordination strip — impact high, size M, now

Tell one continuous story with existing captures: Fleet sees the queue,
Personal shows current work, and AionUi exposes the exact claim
([Fleet](../showcase/01-fleet-overview.png),
[Personal](../showcase/02-personal-today.png),
[AionUi](../showcase/06-aionui-offer-claim.png)).

```text
[01 Fleet] ──routes──> [02 Personal] ──opens──> [06 AionUi]
 See all work             See my work              Act safely
```

- Component/files: `website/index.html`, `website/style.css`,
  `docs/showcase/README.md`; assets `01`, `02`, and `06` unchanged.
- Acceptance: captions stay adjacent at 1440px; the strip becomes a vertical
  numbered sequence at 400px; originals and zoom controls are keyboard
  reachable; status meaning does not depend on colour in either theme.

### 4. Tighten the hero copy around a visible job — impact high, size S, now

```text
BETA · LOCAL-FIRST COORDINATION
Keep every AI agent on the same verified board.
Pursers routes one task to one owner, preserves the handoff, and requires
source-and-test evidence before independent review.
[See work move] [Install the beta]
One board · one active owner · durable history · reviewed evidence
```

- Component/files: future edits to `website/index.html`, `README.md`, and the
  opening of `docs/showcase/README.md`.
- Acceptance: headline stays within about three lines at 400px and remains
  comfortably grouped at 1440px; CTA labels describe destinations; keyboard
  focus order is copy → primary → secondary → proof; neither theme changes the
  beta/local boundary.

### 5. Turn status vocabulary into a proof row — impact medium-high, size S, now

```text
[7 Ready: can take work] [1 Claimed: owned] [1 Submitted: awaiting review]
```

- Component/files: `website/index.html`; screenshot captions in
  `docs/showcase/README.md`; no dashboard code required.
- Acceptance: meanings are text, not colour alone; chips wrap at 400px and
  remain one scannable row where space permits at 1440px; linked definitions
  are keyboard reachable; both themes preserve contrast.

### 6. Lead README screenshots with a compact contact sheet — impact medium, size M, now

Use only the existing Fleet, Personal, and AionUi originals
([Fleet](../showcase/01-fleet-overview.png),
[Personal](../showcase/02-personal-today.png),
[AionUi](../showcase/06-aionui-offer-claim.png)).

```text
| Fleet overview | Personal today | AionUi ticket |
| queue + health | my attention   | claim + state |
```

- Component/files: `README.md`, `docs/showcase/README.md`; use existing images
  or a deterministic derivative of them.
- Acceptance: at 1440px the three labelled previews form one row; at 400px
  they stack or scroll without clipped captions; each preview links to its
  original with meaningful alt text; all links are keyboard reachable; GitHub
  light and dark themes keep labels, borders, and focus indicators legible.

### 7. Make empty and disconnected states credibility evidence — impact medium, size S, later

Pair a populated flow with the real join/disconnected capture and a text next
step ([`05-aionui-join.png`](../showcase/05-aionui-join.png)).

```text
[Not connected] → Connect local helper → [Project connected]
```

- Component/files: `docs/showcase/README.md`, later `website/index.html`.
- Acceptance: at 400px and 1440px, the state and next action remain in the same
  view; the action is keyboard reachable with visible focus; warning/success
  meaning survives monochrome and both themes.

### 8. Add restrained reveal motion only after the static story works — impact low, size S, later

```text
frame 1 visible → frame 2 120–180ms → frame 3 120–180ms
prefers-reduced-motion: all frames visible, no transition
```

- Component/files: future `website/style.css` only.
- Acceptance: zero layout shift; no information is gated by animation; focus
  does not move; 400px and 1440px reading orders are identical; reduced-motion,
  keyboard operation, and both themes are explicitly checked.

## What not to copy

- Do not copy the “AI employee” relationship metaphor. Pursers coordinates
  authenticated seats and reviewed work; a different metaphor would obscure
  its cross-host coordination role.
- Do not copy QoderWake's third-party-trigger story. It is outside this brief
  and would distract from product-visible coordination proof.
- Do not copy the off-white-and-green visual system merely because it is calm
  and consistent. It belongs to Qoder's family and is evidenced across its
  product pages; Pursers should improve evidence order while retaining its own
  identity ([QoderWake](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
  [IDE](qoderwake-ux/landing-and-showcase/ide__1440x900__site-default.png)).
- Do not copy the comparison tables' narrow-screen behaviour. Agent SDK and
  Cloud Agents both clip right-hand content at 400px
  ([Agent SDK](qoderwake-ux/landing-and-showcase/agent-sdk__400x844__site-default.png),
  [Cloud Agents](qoderwake-ux/landing-and-showcase/cloud-agents__400x844__site-default.png)).
- Do not replace real captures with fabricated dashboard mockups. Pursers'
  strongest showcase material is already traceable to documented synthetic
  runs ([showcase notes](../showcase/README.md)).
- Do not use always-on animation to imply invisible work. Motion is not
  observable in the static Qoder evidence, and Pursers should not make a claim
  that its own screenshots cannot prove
  ([QoderWake 1440px](qoderwake-ux/landing-and-showcase/qoderwake__1440x900__site-default.png),
  [QoderWake 400px](qoderwake-ux/landing-and-showcase/qoderwake__400x844__site-default.png)).
- Do not hide synthetic/demo labels. They protect user data while letting the
  product show real code and real states
  ([Fleet](../showcase/01-fleet-overview.png),
  [Personal](../showcase/02-personal-today.png)).

## Recommended cross-surface skeleton

| Beat | pursers.app | README hero | docs/showcase |
|---|---|---|---|
| 1. Promise | one outcome headline + two CTAs | one sentence + badges | one-sentence viewing guide |
| 2. Proof | `06` hero frame | three-image compact overview | full `01`→`02`→`06` sequence |
| 3. Flow | Offer → Claim → Submit → Review | one-line lifecycle | captions tied to each capture |
| 4. Boundary | beta/local-first strip | beta status link | synthetic-data/capture provenance |
| 5. Next action | see workflow / install beta | 60-second quickstart | open original / follow staging notes |

This preserves Pursers' honest product boundaries and real evidence while
borrowing the useful presentation principle demonstrated by the Qoder pages:
show a recognizable product outcome before asking a stranger to absorb the
system model.
