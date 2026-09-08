# Aion Hub manifest schema v0 provenance

This vendored JSON Schema is the repository's deterministic validation form of
the extension contract exercised by AionUi 2.2.1 and AionCore 0.2.1. Those
upstreams did not publish a standalone JSON Schema file at these tags, so the
snapshot was transcribed from their public manifest examples and exact loader
types. In particular, `ExtContributes.webui` is an array of `ExtWebui` records
with `id`, `directory`, and `routes[{path,method,handler}]`; the host does not
define `apiRoutes`, `staticAssets`, or per-route `auth` manifest fields.

- [AionUi tag `v2.2.1`](https://github.com/iOfficeAI/AionUi/tree/v2.2.1),
  commit `dc47f4a0173ff506b08f13c97b10944d61e422d5`:
  `examples/e2e-full-extension/aion-extension.json`,
  `examples/ext-feishu/aion-extension.json`, and
  `examples/ext-wecom-bot/aion-extension.json`.
- [AionCore tag `v0.2.1`](https://github.com/iOfficeAI/AionCore/tree/v0.2.1),
  commit `a9e113437a5cf0552075de618ee9458be24ef756`:
  `crates/aionui-extension/src/types.rs`, `manifest.rs`, `permission.rs`, and
  `constants.rs`.
- [AionHub `main`](https://github.com/iOfficeAI/AionHub), inspected
  2026-09-07: public `aion-extension.json` examples
  under `extensions/`; no standalone schema file was present.

The `risk` metadata block records the calculated permission risk alongside the
loader-compatible `permissions` declaration. AionCore calculates scoped network
access as moderate; unknown manifest metadata is ignored by its v0.2.1 loader.
