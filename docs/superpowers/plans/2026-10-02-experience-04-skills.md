# 04 — Portable skills and onboarding

**Execution:** existing Mac Pursers worker plus independent fleet reviewer; use `$token-thrift`.
**Design:** [shared contracts](../specs/2026-10-02-agent-experience-design.md), section 4 and shared invariants.
**Repository:** Pursers. May start with released tool/docs fallbacks while stream 03 is pending.

## Files and ownership

- Add product-owned skill bundle under `integrations/skills/` with a versioned manifest and focused references.
- Update `docs/GETTING-STARTED.md`, relevant `docs/guides/` role/host guides and onboarding entry points.
- Inspect existing packaging and `delivery-manifest.toml`; declare inclusion through source-of-truth inputs. Generated locks/hashes are operator-owned.
- Add focused packaging/installation validation and onboarding walkthrough evidence; modify existing installer surfaces only after identifying supported host conventions.

## Steps

- [ ] Inventory existing guides, host installation paths and capabilities; identify canonical sources, not duplicate manuals. Publish skill triggers and role/host support matrix.
- [ ] Define `pursers-start/work/review/operate` metadata and lean core instructions. Use progressive references for coordinator, Butler and release roles. Preserve independent review and explicit authority boundaries.
- [ ] Add contract tests for manifest/package contents, valid metadata, links and safe installer behavior before introducing installer changes. Preview conflicts, preserve existing files and support idempotent upgrade/removal.
- [ ] Write host-aware onboarding: select authorized board/project, capability check, harmless read, next permitted action. Zed GUI first; Codex/Goose CLI supported where verified. Never print credentials or rewrite global AGENTS files.
- [ ] Integrate stream 03's final resource links when available; retain version/unsupported fallbacks. A missing catalog must not block onboarding to a supported released server.
- [ ] Independent reviewer runs scenario walkthroughs: no credentials, wrong board, read-only identity, existing skill conflict, expired/resumed lease, blocked baseline, review independence and unauthorized release. Record actions/outcomes and remaining limitations.
- [ ] Validate packaged install/check/removal in an isolated temporary user environment, docs links and leak scan; no changes to the operator's real host setup.
- [ ] Update docs/config/changelog; submit branch/SHA, manifest, literal validation output and reviewer scenario evidence. List generated release artifacts for operator refresh.

## Completion gate

A new user can reach a successful authorized read and understand the next action. Agents load only needed workflow material. Skills do not expand permissions, accidentally claim work, or mandate one installation's private rules.
