# Deployment transport

## Same-machine seats

Run Central on plain HTTP loopback and use the exact URL
`http://127.0.0.1:8766/mcp`. Loopback traffic stays on the machine, so local
seats need no private CA or client trust changes.

## Remote seats

Keep Central bound to loopback and forward its port with SSH or Tailscale. Map
the remote endpoint to local port `8766`, so the seat still connects to
`http://127.0.0.1:8766/mcp`. This preserves the existing bearer tokens and
requires no CA installation or other client trust changes.

If Central must be a shared network service, terminate TLS with a publicly
trusted certificate on a real hostname. Do not distribute a private CA merely
to expose Central.

## URL-bound tokens

JWT `aud` and resource claims bind a token to the exact Central URL. Changing
the scheme, hostname, port, or path changes that resource identifier and
requires re-minting the affected tokens.
