# Listing Pursers on the MCP Registry

`server.json` at the repository root describes Pursers to the official MCP
Registry as `io.github.swisspra/pursers`. It advertises the `pursers-central`
PyPI package over `streamable-http` at `http://127.0.0.1:8766/mcp`, which is
what the packaged runtime actually serves: plain HTTP on loopback, no TLS.

The registry verifies ownership by finding this marker in the README that PyPI
serves for the package:

```
<!-- mcp-name: io.github.swisspra/pursers -->
```

It is in `packages/central/README.md` and `packages/pursers/README.md`. The
releases already on PyPI were built before the marker existed, so **the listing
cannot be verified until the next publish carries it.** The operator decision
is to ship the marker with the b3 train, not as a marker-only patch.

## Keeping it honest

```
python3 tools/check_server_json.py
```

It fails unless all of these hold:

- the `version` fields in `server.json` match `tools/release_versions.toml`
  (product version, and the `pursers-central` package version);
- both READMEs carry the `mcp-name` marker;
- a freshly built `pursers-central` wheel carries the marker in its metadata,
  which is what PyPI will actually show the registry.

`--repository` points it at another checkout; the default is this one.

## At b3

1. Bump versions through the release train as usual, then copy the b3 product
   and central versions into `server.json`. Do not edit them by hand anywhere
   else.
2. `python3 tools/check_server_json.py` must print `PASS`.
3. Publish b3 to PyPI.
4. Install `mcp-publisher` using the method the registry documents at that
   time, then `mcp-publisher login github`, `mcp-publisher validate`, and only
   after that passes, `mcp-publisher publish`.

Do not publish from a tree whose `server.json` versions differ from what is on
PyPI.

## Before listing at all

A listing invites a stranger to install and run this. Today `pursers-central`
needs a JWT issuer, an audience and a JWKS file before it will start, and no
newcomer document explains how to produce them. Decide first whether to ship a
quickstart for that or to state the constraint plainly in the description.
