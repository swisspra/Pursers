# Azure DevOps PR connector

`connector.py` polls Azure DevOps pull requests from a configured bot author and/or
label, creates deterministic Pursers tickets, and writes a completion comment plus
a non-approving service vote back to the PR. It is generic: no organization, repository,
scanner vendor, host, PAT, or Central token is embedded in this directory.

The connector never merges. Its only allowed vote values are `0` (no vote) and
`-5` (waiting for author); positive approval votes are rejected by config validation
and by the fake server. Comments identify themselves as automated and not a human
approval.

## Configuration

Create a JSON file with mode `0600`. The PAT is supplied only through the named
environment variable; the Central token is read from an absolute file path.

```json
{
  "ado": {
    "base_url": "https://dev.azure.com/example",
    "project": "project-slug",
    "repo": "repository-id-or-name",
    "pat_env": "ADO_CONNECTOR_PAT"
  },
  "central": {
    "url": "https://127.0.0.1:8766/mcp",
    "token_path": "/absolute/path/to/central-token",
    "create_mode": "intake"
  },
  "board": {
    "id": "pursers",
    "target_url_prefix": "pursers/ado",
    "agent_name": "ado-connector"
  },
  "filters": {
    "authors": ["scanner-bot"],
    "labels": ["finding"],
    "vote_reviewer_id": "ado-connector-service-id",
    "closed_vote": 0
  },
  "poll_seconds": 60,
  "state_file": "ado-connector-state.json"
}
```

When both author and label filters are configured, a PR must match both. Relative
state paths resolve beside the config. The state is atomically replaced with mode
`0600`; corrupt state is moved to a timestamped sibling and rebuilt safely. Ticket
IDs derive from `(PR id, source commit)`, so a retry after a local-state loss finds
the same ticket rather than creating a duplicate. A new source commit creates a new
suffixed ticket whose description links earlier connector tickets for that PR.

`central.create_mode` defaults to `intake` and uses the least-privilege
`board:read + board:coordinate + board:intake` principal with a deterministic
`coordinator_op_key`. Set it to `writer` only for an explicitly provisioned legacy
`board:write` principal; the connector never attempts to infer or upgrade scopes.

Run one cycle or poll continuously:

```sh
chmod 600 /path/to/ado-connector.json
export ADO_CONNECTOR_PAT='...'
python3 tools/ado-connector/connector.py run --config /path/to/ado-connector.json --once
python3 tools/ado-connector/connector.py run --config /path/to/ado-connector.json
```

## Write-back idempotency

Each PR comment carries a deterministic hidden marker for `(PR, ticket)`. Before a
comment is posted, the connector reads current threads and treats an existing marker
as success. This closes the crash window between the remote POST and local state
write. Reviewer vote updates use Azure DevOps' idempotent PUT endpoint and stay
neutral/non-approving.

## Fake ADO fixture and tests

The in-process fixture implements the exact REST surface used by the connector:

- active PR listing;
- PR thread listing and comment creation;
- reviewer vote PUT;
- Azure DevOps-style Basic PAT authentication.

It is used by the tests and can run empty for local demos:

```sh
export ADO_PAT='local-fixture-secret'
python3 tools/ado-connector/connector.py fake-server --host 127.0.0.1 --port 8089 --pat-env ADO_PAT
```

Run the suite:

```sh
python3 -m pytest -q tools/ado-connector/test_connector.py
```
