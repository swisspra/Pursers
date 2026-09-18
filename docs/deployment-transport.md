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

If a reverse proxy preserves its public hostname in the `Host` header, add that
bare hostname to Central's allowlist. The packaged runtime accepts a repeated
`--allowed-host HOST` option or the comma-separated
`ONBOARD_CENTRAL_ALLOWED_HOSTS` environment variable. The legacy
`CENTRAL_ALLOWED_HOSTS` name is also accepted during migration. Each configured
name is accepted both bare and with a port in the `Host` header and as an HTTP
or HTTPS `Origin`. Loopback remains allowed, DNS-rebinding protection remains
enabled, and unlisted hosts receive HTTP 421.

If Central must be a shared network service, use a publicly trusted certificate
on a real hostname. The packaged runtime can terminate TLS when the operator
supplies both `--tls-certfile /PATH/TO/cert.pem` and
`--tls-keyfile /PATH/TO/key.pem`; the equivalent environment variables are
`ONBOARD_CENTRAL_TLS_CERTFILE` and `ONBOARD_CENTRAL_TLS_KEYFILE`. With neither
value, Central continues to serve plain HTTP. Central does not generate or
store certificates or keys. Do not distribute a private CA merely to expose
Central.

The synonymous `--ssl-certfile` and `--ssl-keyfile` flags are accepted for
operators accustomed to Uvicorn's terminology. Their environment equivalents
are `ONBOARD_CENTRAL_SSL_CERTFILE` and `ONBOARD_CENTRAL_SSL_KEYFILE`; the TLS
names take precedence when both forms are set.

## URL-bound tokens

JWT `aud` and resource claims bind a token to the exact Central URL. Changing
the scheme, hostname, port, or path changes that resource identifier and
requires re-minting the affected tokens.
