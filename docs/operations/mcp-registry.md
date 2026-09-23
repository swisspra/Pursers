# Listing Pursers on the MCP Registry

`server.json` at the repository root describes Pursers to the official MCP
Registry as `io.github.swisspra/pursers`. It advertises the `pursers-central`
PyPI package over `streamable-http` at `http://127.0.0.1:8766/mcp`, which is
what the packaged runtime actually serves: plain HTTP on loopback, no TLS. The
same entry also advertises the `pursers-client` PyPI package as a stdio relay
launched with:

```
uvx --from pursers-client==<VERSION> pursers-mcp \
  --central-url <URL> --board <BOARD_ID> --token-file <PATH> \
  [--ca-file <PATH>] [--tools default|worker|reviewer|all]
```

Use `--tools worker` or `--tools reviewer` for external execution hosts. The
worker profile exposes eight role-specific tools; the reviewer profile exposes
nine, including `dispatch_my_offers` for exact offer discovery. Both reject
credentials without a matching active seat and still rely on Central's bearer-
token authorization for every operation. `default` remains the curated
interactive board profile; `all` is an explicit administrative escape hatch for
principals that need their entire authorized surface.

The pinned
[`2025-12-11` schema](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json)
defines `packages` as an array and assigns `transport`, runtime arguments, and
package arguments to each package; the official
[package documentation at `d1dcaf3`](https://github.com/modelcontextprotocol/registry/blob/d1dcaf3fb36338d45ccdba98b5b8aea915e7d50d/docs/modelcontextprotocol-io/package-types.mdx)
uses that structure for installable variants. Pursers therefore uses one
registry server name with two package transports because Central and its stdio
relay expose the same logical Pursers server; a second name would incorrectly
present an installation choice as a different server.

The stdio package models every configurable launch input as a package argument.
In particular, the registry accepts only a `--token-file` path, never a bearer
token value or secret-valued token environment variable.

The registry verifies ownership by finding this marker in the README that PyPI
serves for the package:

```
<!-- mcp-name: io.github.swisspra/pursers -->
```

It is in `packages/central/README.md`, `packages/client/README.md`, and
`packages/pursers/README.md`. The releases already on PyPI were built before
the marker existed, so **the listing cannot be verified until the next publish
carries it.** The operator decision is to ship the marker with the next release,
not as a marker-only patch.

## Keeping it honest

```
python3 tools/check_server_json.py
```

It fails unless all of these hold:

- the `version` fields in `server.json` match `tools/release_versions.toml`
  (product version plus the `pursers-central` and `pursers-client` package
  versions), including the version pinned in `uvx --from`;
- all three READMEs carry the `mcp-name` marker;
- freshly built `pursers-central` and `pursers-client` wheels carry the marker
  in their metadata, which is what PyPI will actually show the registry;
- the stdio entry stays `uvx` + `pursers-mcp`, contains the exact documented
  arguments, and cannot accept a credential except through `--token-file`.

`--repository` points it at another checkout; the default is this one.

## Publishing a release

1. Bump versions through the release train as usual. It updates the product,
   central, and client versions in `server.json`, including the `uvx --from`
   pin; do not edit those versions by hand.
2. `python3 tools/check_server_json.py` must print `PASS`.
3. Publish both `pursers-central` and `pursers-client` to PyPI and confirm the
   versions match `tools/release_versions.toml`.
4. Install `mcp-publisher` using the method the registry documents at that
   time, then run `mcp-publisher login github` and `mcp-publisher validate`.
   Only the release operator runs `mcp-publisher publish`, and only after
   validation passes.

Do not publish from a tree whose `server.json` versions differ from either PyPI
package.

## Before listing at all

A listing invites a stranger to install and run this. Today `pursers-central`
needs a JWT issuer, an audience and a JWKS file before it will start, and no
newcomer document explains how to produce them. Decide first whether to ship a
quickstart for that or to state the constraint plainly in the description.
