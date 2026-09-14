# Pursers Central

Pursers Central is the loopback MCP service that owns board state. Run it with
the console script or the equivalent Python module:

```bash
export CENTRAL_JWT_ISSUER='https://issuer.example'
export CENTRAL_JWT_AUDIENCE='http://127.0.0.1:8766/mcp'
export CENTRAL_JWKS_PATH=/PATH/TO/private/credential.jwks.json

pursers-central \
  --host 127.0.0.1 \
  --port 8766 \
  --data-dir /PATH/TO/private/central-data \
  --log-level info
```

`python -m pursers_central` accepts the same arguments. The runtime also reads
`ONBOARD_CENTRAL_HOST`, `ONBOARD_CENTRAL_PORT`,
`ONBOARD_CENTRAL_DATA_DIR`, and `ONBOARD_CENTRAL_LOG_LEVEL`; command-line
arguments override those environment defaults.

At startup the runtime selects JWT authentication, the SQLite store, and
invite-only admission. `CENTRAL_JWT_ISSUER`, `CENTRAL_JWT_AUDIENCE`, and
`CENTRAL_JWKS_PATH` must describe the credential issuer used by this Central
instance. Keep the JWKS and data directory private.

The startup banner prints the MCP bind URL, data directory, and health URL.
Check the service without a credential:

```bash
curl --fail --silent http://127.0.0.1:8766/healthz
```

A healthy response has `"status":"ok"` and `"store_backend":"sqlite"`.
