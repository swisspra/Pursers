# Fleet public media pack

These six 1440 × 900 PNGs are a shareable capture of Fleet's read-only public
projection. They were captured from one live local Fleet snapshot projected by
the source at commit `05864cec15ddf116829548151d7c27440c224f76`. The private
source snapshot was never persisted. The immutable public projection digest is
`eef5fe5f1849c5323864fccac0472da8a07acb44af5d16ede2792dba1cbd7ea4`.

The projection deliberately uses pseudonymous aliases, bucketed counts,
minimum cohorts, delayed activity, and closed schemas. A displayed `none` or an
empty route means “nothing publishable in this projection,” not proof that the
private source contained zero records.

## Captures and captions

| Frame | Caption | Evidence caveat |
| --- | --- | --- |
| [Home](01-home.png) | Fleet reported an operational, recent public snapshot. | The active-agent cohort was suppressed; `none` is a public result, not a private headcount. |
| [Projects](02-projects.png) | A pseudonymous project remained active while work counts stayed bucketed. | `few` is a privacy bucket, not an exact total. |
| [Work](03-work.png) | Multiple pseudonymous work items were concurrently active and recent in the captured projection. | The frame is a point-in-time operational view, not a controlled speedup measurement. |
| [Team](04-team.png) | No team row met the public cohort rule at capture time. | Do not caption this as “no agents.” |
| [Approvals](05-approvals.png) | The public projection reported no publishable review or human-approval backlog. | This is transient snapshot state, not a historical claim. |
| [Activity](06-activity.png) | No delayed, cohort-safe activity row was publishable at capture time. | Activity can be absent because of delay or suppression. |

Settings is intentionally omitted. The public asset contract exposes Home,
Projects, Work, Team, Approvals, and Activity only; it excludes Settings,
clipboard behavior, and private APIs. Capturing the private Settings screen
would violate the “public projection only” requirement.

## Source and validation record

- Source run: `public-display-05864cec-eef5fe5f`.
- Capture date: 2026-09-28.
- Browser viewport: 1440 × 900 CSS pixels at device scale 1.
- Theme: browser-selected dark color scheme from the unmodified public CSS.
- Browser chrome: excluded.
- Candidate assets: unmodified `PUBLIC_HTML`, `PUBLIC_CSS`, and `PUBLIC_JS`.
- Visual review: every frame checked for clipped content, unexpected overlays,
  private identifiers, credentials, host paths, and low-count interpretations.
- File review: every PNG has only `IHDR`, `iCCP`, `IDAT`, and `IEND` chunks;
  there are no text, EXIF, location, author, or comment chunks.
- Machine-verifiable dimensions, byte sizes, hashes, route names, and omissions
  are in [manifest.json](manifest.json).

The accompanying [storyboard](STORYBOARD.md) is a 30-second shot list. It uses
the real public snapshot as evidence but makes no throughput, cost, causal, or
sequential-versus-parallel speedup claim.
