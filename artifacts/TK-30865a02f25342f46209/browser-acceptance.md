# Dashboard Settings browser acceptance

These screenshots were captured from the repository's deterministic,
public-safe `display_acceptance_server.py` fixture. They do not contain live
fleet, credential, provider, repository, or user data, and they do not assert
that a deployed runtime is at the candidate revision.

## Captures

- `browser/settings-desktop-1440x900.png` — Settings at the Ego Browser wide
  desktop viewport (captured image width 1101 CSS pixels); eight typed editor
  groups loaded from product-shaped fixture responses. The filename is the
  stable acceptance-artifact label, not a claim about the captured pixel width.
- `browser/settings-mobile-390x844.png` — the same Settings route at the mobile
  viewport; `innerWidth=390`, `scrollWidth=390`, and eight typed editor groups.

The captures show connector/source/onboarding, delivery, managed seats,
dispatch, board policy, retention, membership, Butler, automation, and
diagnostic surfaces. Mobile width was verified without horizontal document
overflow. Browser automation used one isolated Ego Browser task space and was
closed after capture.

## Test context

The fresh screenshots supplement, rather than replace, executable acceptance:

```text
python3 -m pytest -q tools/fleet-dashboard/tests
713 passed, 6 skipped in 215.19s (0:03:35)
```

The six skips are the suite's declared environment-dependent skips; no test was
deselected or newly skipped for this ticket.
