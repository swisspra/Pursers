# QoderWake vs Pursers: first-impression page structure

Research date: 2026-09-15. Scope: public presentation design only—section
order, hero, screenshot use, copy rhythm, and CTA placement. Third-party
intake, governance, permissions, and memory-policy design are intentionally
excluded.

## Evidence and limits

The primary source is the public [QoderWake landing page](https://qoder.com/en/qoderwake),
with the public [launch article](https://qoder.com/en/blog/qoderwake) used only
to distinguish product claims from what the landing page visually presents.
The page was fetched without signing in. Its accessible response exposed the
complete heading/copy order and image positions/alts. A separate public image
result from Qoder's launch article showed an employee profile for “Alex” with
online state, join date, role summary, and a left rail for Projects, Triggers,
Tasks, Memory, Skills, and Connectors.

The interactive browser was unavailable: Ego Lite could not connect from the
sandbox, and the browser UI then denied access to `qoder.com`. That denial was
not bypassed. Therefore this report does **not** infer exact landing-page pixel
colours, font families, animation curves, hover states, responsive crops, or
whether paired image URLs are desktop/mobile variants. Those details are
recorded below as “not verifiable,” not guessed.

Pursers evidence comes from the current repository at
`02e2dad2f9b339137fa8c7d947ef480408362446`, especially
[`website/index.html`](../../website/index.html),
[`website/style.css`](../../website/style.css), the
[`docs/showcase`](../showcase/README.md) capture notes, and these real,
synthetic-data screenshots:

- [`01-fleet-overview.png`](../showcase/01-fleet-overview.png)
- [`02-personal-today.png`](../showcase/02-personal-today.png)
- [`03-personal-work.png`](../showcase/03-personal-work.png)
- [`04-personal-agents.png`](../showcase/04-personal-agents.png)
- [`05-aionui-join.png`](../showcase/05-aionui-join.png)
- [`06-aionui-offer-claim.png`](../showcase/06-aionui-offer-claim.png)
- [`07-aionui-status.png`](../showcase/07-aionui-status.png)

## What the QoderWake page actually shows

### 1. Global navigation, then a direct product promise

The header presents Product and Enterprise, direct links to Pricing and
Marketplace, a Resource group, Activities, a China-site switch, Sign in, and
Download. The hero then uses this exact hierarchy:

1. product name: “QoderWake”;
2. headline: “Autonomous AI Employees, On the Job”;
3. one sentence that says where it appears and that it carries work through
   delivery;
4. a primary “Download” action plus “All Downloads”;
5. a large product-screenshot slot.

The crawl exposes two image links with the same “QoderWake product screenshot”
alt at this position. It does not establish whether these are two distinct
screens, responsive sources, or duplicated markup. No hero status badges,
installation commands, architecture qualifiers, empty state, or visible
customer proof are exposed. Exact colour, typography, crop, and hero motion
are not verifiable from the accessible response.

### 2. Outcome examples before technical explanation

“Meet AI Employees That Truly Own the Work” follows immediately, with the
bridge copy “Not another chat box, it works where you and your teammates
already are.” Three short example blocks alternate a named outcome, one
sentence, and a product visual:

- “A Teammate in the IM Chat” — the sentence describes turning a long thread
  into a plan;
- “Autonomously Start Work on Its Own” — the sentence lists four trigger
  categories;
- “Multiple Wakers, Working Together” — the sentence describes parallel roles,
  handoff, review, and traceability.

Each visual position appears as a pair of same-alt image links in the crawl.
The visible copy uses verbs and completed outcomes rather than feature nouns.
No error, loading, permission, or empty-state screenshot is exposed. Motion is
not described and could not be verified.

### 3. A “how it works” bridge after the examples

“How QoderWake Works” promises “Three underlying systems.” The following
content is presented in short headline/body units around image slots:

- “Harness Engineering for Stable Delivery” — persistence, recovery,
  verification, and retry;
- “Your First AI Employee” — identity/profile attributes and traceable record;
- “Job skills continuously expand” — built-in and custom role skills;
- “Acts Inside Your Existing Workflows” — places where work occurs.

The public employee-profile image is the clearest in-product hierarchy found:
a persistent left rail, an employee detail page, a named role, online status,
join date, and a compact biography. This makes “who is working?” legible before
exposing the underlying system. The landing-page crawl does not expose exact
card count, grid geometry, colours, font metrics, interaction, or empty states.

### 4. Proof-oriented closing section and a repeated CTA

“Built for Real Work, Not for Demos” resets the rhythm with the terse line
“Demos don't count. Delivery does.” It then presents four proof claims:

- “Customize for Each Profession”;
- “Knowledge Retention and Continuous Evolution”;
- “Secure, Governable, Accountable”;
- “An AI Team for the Whole Organization.”

The page closes with “Beyond coding. Create anything” and “Try Qoder now,” then
the full product/resource/footer link groups. Thus the story is promise →
recognizable outcomes → mechanism → credibility → CTA. The page does not show
a pricing card, step-by-step setup, keyboard state, alternate theme, compact
mobile layout, or a failure/empty state in the evidence available here.

## Side-by-side with Pursers today

| First-impression job | QoderWake | Pursers today | Consequence |
|---|---|---|---|
| Say what it is | Product name, outcome headline, one sentence | `website/` says “One governed board for every agent” plus a longer local-first explanation; README adds a ship metaphor before the workflow | Pursers is accurate, but “board” and “fleet” arrive before a stranger sees an agent doing work |
| Show the product | Screenshot immediately after hero copy | Website hero uses a hand-built four-row ledger; no real screenshot appears anywhere on the site | The current site explains a model before proving that a usable product exists |
| Establish narrative rhythm | Three named work outcomes before system detail | Principle strip, six feature cards, then setup | Pursers front-loads concepts; the first real use story is delayed |
| Show coordination inside the product | Named employee/profile and multi-role work visuals | Fleet screenshot shows central health, capacity, and attention; Personal shows work/agents; AionUi shows join and ticket lifecycle | Pursers has stronger operational evidence, but it is split across seven images without one guided sequence |
| Use status language | “Online,” role, join date, delivery/working language | Fleet: Busy/Ready/Stale/Open/Claimed/Submitted; Personal: working/idle; AionUi: connected/not connected/open/claimed/submitted | Pursers needs a small legend or narrative to connect system status to human meaning |
| CTA placement | Download in header and hero, “Try Qoder now” at close | Website: Preview setup / Read setup guide in hero, then repository/license at close; README reaches commands before screenshots | Pursers CTAs are honest but ask for setup before visual confidence |
| Colour and type | Not verifiable from the permitted capture | Website: warm paper `#f3efe4`, ink `#15222b`, rust accent `#d04a2a`, serif display + system sans; product captures use calm blue/green state accents and dense sans typography | Keep Pursers' own visual identity; improve evidence order rather than imitating an unverified palette |
| Empty states and motion | Not shown/verifiable | Fleet explicitly shows “No tickets are waiting”; Personal labels synthetic/demo state; website buttons lift 2px and smooth-scrolls | Pursers can turn truthful empty/error states into trust evidence; motion should remain secondary |

## Ranked proposals

Ranking balances first-impression impact against implementation effort. “Now”
means a bounded beta.2 content/layout change using existing assets; it does not
authorize implementation in this research ticket.

### 1. Put a real product frame in the website hero — impact high, effort S, now

Use `docs/showcase/06-aionui-offer-claim.png` as the first proof because it
shows board-backed work, a claim action, and persisted ticket states in one
surface. Keep the ledger as a compact explanation below it, not as the hero's
only visual.

```text
┌──────────────────────────┬──────────────────────────┐
│ Keep every AI agent on   │ [REAL AionUi ticket      │
│ the same verified board. │  screenshot + caption]   │
│ [See the workflow] [Try] │ “offer → claim → review”  │
└──────────────────────────┴──────────────────────────┘
```

- Component/files: `website/index.html`, `website/style.css`, existing
  `docs/showcase/06-aionui-offer-claim.png` (or a copied web-served asset).
- Acceptance: at 1440px, headline/CTA and meaningful screenshot region are
  visible without scrolling; at 400px, copy precedes an uncropped, zoomable
  image; keyboard focus is visible; light/dark themes keep caption contrast.

### 2. Replace concept-first section order with a 60-second story — impact high, effort S, now

Order the site as promise → live work → three-step flow → proof → setup →
boundary → CTA. Move the six feature cards after the screenshots or reduce them
to three outcome blocks.

```text
Hero → See work move → Offer/Claim/Review → Fleet proof → 60-sec setup → CTA
```

- Component/files: `website/index.html`; mirror headings in `README.md` later.
- Acceptance: a first-time reader can answer “what, who, how, proof, next step”
  from headings alone at both widths; Tab order matches visual order; themes do
  not reorder content.

### 3. Use a three-frame coordination strip — impact high, effort M, now

Tell one continuous story with existing captures instead of presenting a
gallery: Fleet sees the queue, Personal shows current work, AionUi shows the
exact claim.

```text
[01 Fleet] ──routes──> [02 Personal] ──opens──> [06 AionUi]
  See all work            See my work             Act safely
```

- Component/files: `website/index.html`, `website/style.css`,
  `docs/showcase/README.md`; assets `01`, `02`, and `06` unchanged.
- Acceptance: captions remain adjacent to images; horizontal strip becomes a
  vertical numbered sequence at 400px; links and enlarged views are keyboard
  reachable; status colours are not the only carrier of meaning in either
  theme.

### 4. Tighten the hero copy around a visible job — impact high, effort S, now

Recommended copy skeleton:

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
- Acceptance: headline fits within about three lines at 400px; CTA labels
  describe destinations; focus order is copy → primary → secondary → proof;
  no theme changes wording or hides the beta/local boundary.

### 5. Turn status vocabulary into a human-readable proof row — impact medium-high, effort S, now

Translate without renaming product states: Ready = can take work, Claimed = one
owner, Submitted = waiting for independent review, Stale = needs attention.

```text
[7 Ready: can take work] [1 Claimed: owned] [1 Submitted: awaiting review]
```

- Component/files: `website/index.html`; screenshot captions in
  `docs/showcase/README.md`; no dashboard code required.
- Acceptance: meanings are present as text, not colour alone; chips wrap at
  400px; keyboard users can reach any linked definition; light/dark contrast
  remains sufficient.

### 6. Lead README screenshots with a compact contact sheet — impact medium, effort M, now

The README currently places a long install block before three full-width
screenshots. Put a small three-frame linked overview after the opening paragraph
and keep the full-size captures in `docs/showcase/`.

```text
| Fleet overview | Personal today | AionUi ticket |
| queue + health | my attention   | claim + state |
```

- Component/files: `README.md`, `docs/showcase/README.md`; use only existing
  images or a deterministic derivative of them.
- Acceptance: GitHub renders useful alt text; images link to originals; the
  table stacks/readably scrolls on narrow view; links have textual labels and
  remain legible in GitHub light/dark themes.

### 7. Make empty and disconnected states part of the credibility story — impact medium, effort S, later

Pair the populated sequence with one honest state from `05-aionui-join.png` or
the Fleet “No tickets are waiting” panel. Caption what the user does next.

```text
[Not connected]  →  Connect local helper  →  [Project connected]
```

- Component/files: `docs/showcase/README.md`, later `website/index.html`.
- Acceptance: state and next action are written in text; 400px crop includes
  the message and control; keyboard focus is visible; warning/success meaning
  survives monochrome and both themes.

### 8. Add restrained reveal motion only after the static story works — impact low, effort S, later

Use no autonomous “busy agent” theatre. If motion is added, limit it to a short
opacity/translate reveal for the three coordination frames and disable it under
`prefers-reduced-motion`.

```text
frame 1 (visible) → frame 2 (120–180ms) → frame 3 (120–180ms)
reduced motion: all frames visible, no transition
```

- Component/files: future `website/style.css` only.
- Acceptance: zero layout shift; no information gated by animation; focus does
  not move; 400px and 1440px flows are identical; reduced-motion and both
  themes are explicitly checked.

## What not to copy

- Do not copy the “AI employee” relationship metaphor. Pursers coordinates
  authenticated seats and reviewed work; implying employment changes the
  product promise and obscures its cross-host infrastructure role.
- Do not copy QoderWake's third-party-trigger story. It is outside this brief
  and would distract from Pursers' product-visible coordination proof.
- Do not imitate an unverified colour palette, font, animation, or screenshot
  crop. The accessible evidence is insufficient, and Pursers already has a
  coherent warm-paper website plus blue/green product surfaces.
- Do not replace real captures with fabricated dashboard mockups. Pursers'
  strongest differentiator is that every shown state can be traced to a
  disposable live Central and documented capture procedure.
- Do not use “24/7,” “autonomous,” or “always working” without runtime evidence
  and an explicit product boundary. The beta is local-first and single-owner.
- Do not make Download the primary CTA until it resolves to the approved beta
  artifact. “See the workflow” is the safer first action when availability is
  conditional.
- Do not hide synthetic/demo labels to make screenshots look more impressive.
  Keep the labels and explain that they protect user data while demonstrating
  real product code.

## Recommended cross-surface skeleton

Use the same five beats, with different depth, across all three entry points:

| Beat | pursers.app | README hero | docs/showcase |
|---|---|---|---|
| 1. Promise | one outcome headline + two CTAs | one sentence + badges | one-sentence viewing guide |
| 2. Proof | `06` hero frame | three-image compact overview | full `01`→`02`→`06` sequence |
| 3. Flow | Offer → Claim → Submit → Review | one-line lifecycle | captions tied to each capture |
| 4. Boundary | beta/local-first strip | beta status link | synthetic-data/capture provenance |
| 5. Next action | see workflow / install beta | 60-second quickstart | open original / follow staging notes |

This keeps Pursers' honest boundaries and real evidence while borrowing the
useful part of QoderWake's presentation: show a recognizable product outcome
before asking a stranger to absorb the system model.
