# 06 — Multipage Pursers public website

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 6 and shared invariants.
**Repository:** Pursers-WebApp, routed by `pursers-webapp/`. Do not implement this in the core repository.

## Files and ownership

- Inspect `src/App.tsx`, current components/styles, `worker/index.ts`, `package.json`, Vite/TypeScript configuration and `wrangler.jsonc`.
- Add Astro route/layout/content structure and only necessary React islands; remove obsolete routing/build inputs after equivalent behavior is verified.
- Preserve Hono API behavior and Cloudflare bindings/domains; no deployments or secret edits.
- Add route/link/content checks and browser smoke coverage using the repository's supported tooling.

## Steps

- [ ] Inventory current anchors, content, API routes, headers and build/deployment contract. Verify official current Astro/Cloudflare support and record compatible dependency choices.
- [ ] Draft sitemap and low-fidelity page/content layout for home, how-it-works, get-started, docs, integrations and releases. Capture in ticket evidence; implementation can proceed under the approved design.
- [ ] Establish Astro build/routing and preserve Hono `/api/*` behavior. Test health/unknown API paths, direct document links, real 404, security/cache headers and asset routing.
- [ ] Implement canonical content/layout/navigation, meaningful docs search or filter, released-version commands/links and support matrix. Preserve old anchor destinations. Static content works without JavaScript.
- [ ] Explain OS-for-agent-fleet positioning and lifecycle with synthetic examples. Mark new capabilities as planned until actually released; no private briefings, production screenshots or unsupported automation claims.
- [ ] Apply the ui-design-animation design principles: purposeful short feedback, reduced motion, gated hover, visible keyboard focus; no gratuitous hero or status animation. Maintain readable light/dark layouts.
- [ ] Verify mobile/desktop routes, keyboard navigation, search empty/error states, command copying, contrast, canonical/sitemap/link checks and performance against baseline. Capture browser screenshots and outcomes.
- [ ] Run build, available checks and Cloudflare deployment dry-run. Update website README/changelog with content maintenance and local preview. Submit SHA, literal output, route matrix and browser evidence; do not publish.

## Completion gate

The website gives new and existing users a clear next action, routes work directly, existing API/domain behavior is preserved, and public claims match released product support. The private fleet dashboard remains independent.
