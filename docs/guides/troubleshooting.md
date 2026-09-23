# Troubleshooting Pursers

This guide maps messages from Pursers 5.0.0 to likely causes and safe fixes.
Start with the exact text you see. Do not paste a token, signing key, door, or
credential file into a terminal command, ticket, log, or support message.

The examples use `/PATH/TO/...` placeholders. Commands were checked with a
temporary installation and temporary Central instance on a free loopback port.

## First checks

Activate the dedicated environment and confirm that one coherent release is
installed:

```bash
. /PATH/TO/VENV/bin/activate
python --version
python -m pip show pursers pursers-central pursers-client
python -m pip check
command -v pursers-central
```

A healthy dependency check prints:

```text
No broken requirements found.
```

Check Central without sending a credential:

```bash
PORT=49152  # replace with the port printed by your generated profile
curl --fail --silent "http://127.0.0.1:${PORT}/healthz"
```

The response should contain `"status":"ok"` and
`"store_backend":"sqlite"`. A healthy endpoint does not prove that an MCP
credential has the right issuer, audience, board, scope, or membership.

## Installation

### MCP v1 and v2 conflict

**Symptom**

When an older FastMCP release and Pursers are requested in one environment,
pip reports text like:

```text
ERROR: Cannot install fastmcp==2.14.7 and pursers-central==0.1.0 because these package versions have conflicting dependencies.

The conflict is caused by:
    pursers-central 0.1.0 depends on mcp==2.2.0
    fastmcp 2.14.7 depends on mcp<2.0 and >=1.24.0

ERROR: ResolutionImpossible
```

**Cause**

Pursers uses MCP v2. Some applications still pin MCP v1. One Python
environment cannot satisfy both requirements.

**Fix**

Create a new virtual environment for Pursers. Do not force-install a different
`mcp` version over the resolver result.

```bash
python3 -m venv /PATH/TO/PURSERS-VENV
. /PATH/TO/PURSERS-VENV/bin/activate
python -m pip install --upgrade pip
python -m pip install pursers
python -m pip check
```

**Confirm**

`python -m pip check` prints `No broken requirements found.` and
`python -m pip show mcp` reports the version required by
`pursers-central`.

### Unsupported Python version

**Symptom**

```text
ERROR: Package 'pursers-central' requires a different Python: 3.10.0 not in '>=3.11'
```

**Cause**

Pursers requires Python 3.11 or newer. This release supports Python
3.11 through 3.14.

**Fix**

Install a supported Python, create a fresh environment with that interpreter,
and reinstall Pursers. Reusing a virtual environment does not change the
interpreter that created it.

**Confirm**

```bash
python --version
python -m pip check
```

The first command reports Python 3.11, 3.12, 3.13, or 3.14, and the second
reports no broken requirements.

### `pursers-central init` is missing

**Symptom**

The 5.0.0b2 dependency set produces:

```text
pursers-central: error: unrecognized arguments: init /PATH/TO/INSTANCE
```

**Cause**

The beta Central package did not include the quickstart `init` dispatcher.

**Fix**

Upgrade the whole Pursers environment, not only one dependency:

```bash
python -m pip install --upgrade 'pursers==5.0.0'
python -m pip check
pursers-central init --help
```

**Confirm**

The last command begins with:

```text
usage: pursers-central init [-h] [--port PORT] [--board BOARD] [--force]
                            directory
```

## Authentication and board access

### `401 Unauthorized`

**Symptom**

Invalid, expired, unknown-key, wrong-issuer, and wrong-audience credentials are
deliberately indistinguishable at the HTTP boundary:

```text
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer error="invalid_token", error_description="Authentication required", ...

{"error": "invalid_token", "error_description": "Authentication required"}
```

**Cause and fix**

Use this checklist without printing or decoding the credential in shared
output:

| Cause | Safe fix |
| --- | --- |
| Token is malformed, expired, or not yet valid | Issue a new credential from the same Central instance and update the credential file used by the host. |
| JWT `kid` is missing, unknown, or matches more than one JWKS key | Reissue the credential against the active JWKS. Remove duplicate keys through the documented operator rotation procedure. |
| `iss` does not exactly match Central's configured issuer | Use the credential produced for this Central instance; do not edit JWT claims. |
| `aud` or `resource` does not exactly match Central's MCP audience | Use the MCP URL and credential from the same generated profile. The `/mcp` suffix and scheme are significant. |

Central does not reveal which JWT check failed because that would disclose
authentication details to an unauthenticated caller.

**Confirm**

Restart the MCP host after changing its credential file. Call `board_onboard`
and confirm that the returned principal and agent identity are the intended
ones. A successful `/healthz` request alone is not sufficient.

### Token belongs to another board

**Symptom**

```text
board access denied: token is not authorized for this board
```

**Cause**

The signed credential is board-bound and the requested `board_id` differs.

**Fix**

Use the credential file issued for that board, or intentionally request the
board named by the credential. Do not copy a token between board profiles.

**Confirm**

Call `board_onboard` with the intended `board_id`; confirm the returned board,
principal, role, and capabilities.

### Tool scope is insufficient

**Symptom**

```text
authenticated principal lacks board:write authorization
```

The scope name changes with the operation, for example `board:review` or
`board:coordinate`.

**Cause**

Authentication succeeded, but the credential does not authorize that class of
operation. A display name or declared role cannot add token scope.

**Fix**

Use a credential deliberately issued with the required least-privilege scope.
Do not turn a worker into a reviewer merely by changing its agent name.

**Confirm**

Reconnect, call `board_onboard`, and inspect the returned capabilities and
role before retrying the tool.

### Principal is not admitted or the agent is not onboarded

**Symptoms**

```text
board access denied
```

or:

```text
agent is not a member of this board
```

**Cause**

The first message means the authenticated principal has no board membership.
The second means the requested agent identity has not joined under that
principal, has retired, or the caller supplied the wrong stable agent name.

**Fix**

Have a board administrator admit the principal if required. Then call
`board_onboard` with the exact stable agent name, correct role, and intended
capabilities. Do not substitute another seat's name.

**Confirm**

The onboarding response contains the intended `principal_id`, `agent_id`,
`agent_name`, membership role, and active lifecycle state.

## Transport and connection failures

### Host or Origin is not allowed

**Symptom**

```text
HTTP/1.1 421 Misdirected Request

Invalid Host or Origin header
```

**Cause**

Central accepts loopback host names by default. A remote host name is missing
from the generated profile's allowed-host configuration, or the client sent a
different `Host` or `Origin` value.

**Fix**

Add the exact bare host name to the Central profile and restart Central. Do
not include a scheme, port, path, whitespace, or wildcard in an allowed-host
entry.

**Confirm**

Request `/healthz` through the same host name the MCP client uses. It should
return HTTP 200 before you retry `/mcp`.

### TLS certificate is not trusted

**Symptom**

```text
curl: (60) SSL certificate problem: self signed certificate
```

Other clients may report a certificate verification failure with different
wrapper text.

**Cause**

The client does not trust the CA that signed Central's certificate, the CA
file path is wrong, or the certificate does not cover the requested host name.

**Fix**

Point the seat's optional CA file field, or its managed `SSL_CERT_FILE`, at the
operator-provided CA certificate:

```text
/PATH/TO/PRIVATE/central-ca.pem
```

Do not disable certificate verification.

**Confirm**

```bash
curl --fail --silent --cacert /PATH/TO/PRIVATE/central-ca.pem \
  https://CENTRAL_HOST.example/healthz
```

The command returns the healthy JSON response without `curl: (60)`.

### Plain HTTP is used for a remote Central

**Symptom**

```text
Remote Central doors require HTTPS.
```

The Fleet connection banner can also show this recovery text:

```text
Check Central URL: remote Central must use https:// (http:// is loopback-only); verify token scope, then retry.
```

**Cause**

Plain HTTP is allowed only for loopback. A remote URL begins with `http://`.

**Fix**

Configure TLS on Central, use an `https://` MCP URL, add the exact host to the
allowlist, and configure the CA file when a private CA is used.

**Confirm**

The exact remote `https://.../healthz` URL succeeds with certificate
verification enabled, then the `https://.../mcp` client can onboard.

### Port is already in use

**Symptom**

```text
ERROR:    [Errno 48] error while attempting to bind on address ('127.0.0.1', <PORT>): [errno 48] address already in use
```

Linux usually reports error number 98 with the same `address already in use`
ending.

**Cause**

Another process already owns the configured listener, or a second Central was
started with the same profile.

**Fix**

Use the already running owned service if it is healthy. Otherwise stop that
owned service through its normal service manager, or create a new throwaway
profile on a free loopback port. Do not kill an unknown process merely to take
its port.

**Confirm**

There is exactly one intended listener and its `/healthz` response is healthy.

### Proxy settings appear to interfere

**Symptoms**

The host may show a connection failure, or Fleet may announce:

```text
Connection interrupted. Reconnecting.
```

Pursers Client itself uses `trust_env=False`, so `HTTP_PROXY` and
`HTTPS_PROXY` do not redirect its requests. A wrapper such as `curl`, an MCP
relay, or a host launcher may still honor proxy environment variables.

**Fix**

Inspect the environment of the failing wrapper. Configure `NO_PROXY` for the
exact loopback or Central host, or remove unintended proxy variables from that
host's managed environment. Do not put credentials in a proxy URL.

**Confirm**

Run the wrapper's connection test to the exact MCP host. Fleet clears the
banner and announces:

```text
Connection restored.
```

## Work, leases, review, and waits

### Offer expired or this seat was not offered the ticket

**Symptom**

```text
ticket is not offered to this seat; wait for your offer
```

**Cause**

The offer expired, was re-routed, belongs to another authenticated seat, or
the seat is no longer eligible. An old notification is not authority to claim.

**Fix**

Return to the push wait using its latest positive cursor. Claim only a new
offer whose `agent_id` matches the current onboarding response.

**Confirm**

The new offer is unexpired and `ticket_claim` returns `ok: true` plus a lease
expiration.

### Another worker won the claim race

**Symptom**

```text
ticket is claimed by another identity
```

**Cause**

Another authenticated agent committed the claim first.

**Fix**

Do not retry the same claim in a loop. Return to the queue and wait for another
offer.

**Confirm**

Read the ticket only when a relevant board cue authorizes a refresh; its
current executor is not this seat.

### Lease expired or work was abandoned

**Symptoms**

The ticket can contain:

```text
last_release_reason: "lease expired"
```

Submitting after release produces:

```text
caller is not current executor; lease was released at <TIMESTAMP>
```

**Cause**

The pre-submission lease reached its deadline without a valid renewal. Central
released the claim and may have redispatched it.

**Fix**

Do not submit stale work. Return to the queue. Resume only after a new valid
offer and claim; reconcile the latest ticket contract before continuing.

**Confirm**

A fresh claim returns a new `lease_expires_at`. Renew while doing active work,
and submit before that lease expires.

### Reviewer is the submitting seat or principal

**Symptoms**

For the same seat:

```text
self-review denied: authenticated seat submitted this work
```

Same-principal rejection is **not verified on this release**. The shipped
runtime rejects the submitting `agent_id`, but a differently named reviewer
seat authenticated as the same principal can currently review and receive the
`independent-principal-review` label. That label alone therefore does not prove
principal independence in 5.0.0.

**Cause**

The submitting seat tried to review its own work. A different display name or
session also does not, by itself, create an independent principal.

**Fix**

For actual independent review, route review to a reviewer credential issued to
a different principal, with reviewer board membership and `board:review`
scope. Do not rely on the label alone on this release.

**Confirm**

Compare `reviewed_by_principal_id` with `submitted_by_principal_id`; they must
differ. The review record should also report `independent-principal-review`.

### Submission is missing a required field

**Symptom**

For a required code reference, Central reports:

```text
branch_and_commit must appear exactly once in notes as '<platform>/<branch> @ <full-40-hex-sha>'
```

Other required fields are listed in the ticket's `required_fields` array and
must be present in the submission using their exact key names.

**Cause**

The submission omitted required evidence, used a different spelling such as a
hyphenated key, or supplied malformed metadata.

**Fix**

Read the current ticket immediately before submission. Populate every required
field, keep `files_changed` equal to the exact tip diff, and use the
clone-owning submit path for a branch/SHA requirement.

For an external MCP host using `pursers-mcp --tools all`, configure the local
relay with `--repository-root /PATH/TO/WORK` and pass
`repository: /PATH/TO/WORK/TICKET-CHECKOUT` to `ticket_submit`. Do not pass
`submission_preflight` or credential content. The relay rejects missing,
out-of-root, non-Git, and remote-tip-mismatched paths before it calls Central.

**Confirm**

The submit boundary verifies the exact remote branch tip and returns
`status: "submitted"`. Do not treat a locally formatted note as proof that the
remote tip exists.

### A coordinator question has no answer

**Symptom**

`ticket_question_wait` returns a bounded result with:

```json
{"timed_out": true, "question": null}
```

**Cause**

No correlated answer arrived before the wait deadline. This is not an answer
and does not change the ticket contract.

**Fix**

Keep the returned cursor, continue any work that is not blocked, and re-arm the
question wait with the same `ticket_id` and `question_id`. Do not create a new
question merely because one wait timed out.

**Confirm**

A later return has `timed_out: false` and contains the same `question_id` with
its answer.

### Work wait times out with no events

**Symptom**

```json
{"events": [], "timed_out": true, "mode": "push", "reason": "timeout"}
```

**Cause**

No relevant event arrived during that bounded wait. This is normal.

**Fix**

Persist the complete returned `new_seq` cursor and re-arm from it. Never reset
to zero or replace a per-board cursor map with one guessed scalar.

**Confirm**

The next call uses the exact returned cursor. A later event advances it; an
empty timeout may leave it unchanged.

### Wait reports poll mode

**Symptom**

```json
{"timed_out": true, "mode": "poll"}
```

The bridge may also log:

```text
WARNING: subscriptions/listen unavailable for board='<BOARD>'; polling this board for the remainder of this call and retrying push on re-arm: <REASON>
```

**Cause**

Poll mode can be the bridge's initial catch-up or a bounded fallback after a
push subscription failure or connection-cap limit. `mode: "poll"` alone does
not prove that work was lost.

**Fix**

Preserve `new_seq`, process any returned events, and re-arm. If repeated calls
return empty results with the same cursor and no live push subscription can be
established, stop the loop and inspect the bridge diagnostics instead of
spinning.

**Confirm**

A healthy re-arm reports `mode: "push"`, or a fallback return advances the
authoritative cursor and delivers the expected event.

## Fleet dashboard

### Connection banner remains visible

**Symptom**

The banner has this form:

```text
reconnecting… last success <TIME-OR-never> · Last error: <ERROR-CLASS>. Check Central URL: remote Central must use https:// (http:// is loopback-only); verify token scope, then retry.
```

**Cause**

One or more Central requests failed. Common causes are a wrong URL, untrusted
certificate, host allowlist rejection, invalid credential, or insufficient
scope.

**Fix**

Check `/healthz`, scheme, host name, CA file, and the seat's credential path in
that order. Then use the dashboard's Doctor action.

**Confirm**

The banner clears and the live region announces `Connection restored.`

### Page shows stale information

**Verification status**

`not verified on this release`: Fleet 5.0.0 does not render a dedicated label
when page data is stale. If a refresh fails after data has loaded, the page
retains that data and keeps the last successful refresh time in this form:

```text
Updated <TIMESTAMP>
```

At the same time, the connection banner from the preceding section remains
visible and the live region announces:

```text
Connection interrupted. Reconnecting.
```

Agents can also be classified as `stale` after the configured activity
threshold. That agent state is separate from the freshness of the page data.

**Cause**

The latest refresh failed, or an agent has not reported recent activity. A
failed page refresh preserves the previously loaded data; the shipped UI does
not add a separate stale-data label.

**Fix**

Restore the Central connection first. For one agent, verify its process,
profile, bridge, and onboarding with Doctor; do not infer that cached state is
current.

**Confirm**

The connection is restored, the data refresh timestamp advances, and the
agent's state reflects a new board activity record.

### Seat needs restart after Apply

**Symptoms**

```text
Restart <HOST> to load the updated seat.
```

or the seat row shows:

```text
NEEDS RESTART
```

**Cause**

Apply wrote the managed host configuration, but a process that predates that
file is still running.

**Fix**

Save work and restart only the named host. Do not restart Central unless its
own profile changed.

**Confirm**

Run Doctor again. Its restart check should report:

```text
no stale host process detected
```

## Pursers Personal

### Setup, rollback, or uninstall refuses to continue

**Symptom**

```text
quit Claude Desktop completely before changing its Personal integration
```

**Cause**

Claude Desktop or one of its bundle helpers is still running. Personal refuses
to edit the integration while the host may be reading it.

**Fix**

Quit Claude Desktop completely, including background helpers, then rerun the
same operation. Do not bypass the process check.

**Confirm**

```bash
pursers-personal doctor --profile /PATH/TO/PROFILE
```

The lifecycle check passes and setup can return its plan or apply result.

### Roll back or uninstall safely

Rollback restores the exact pre-setup integration bytes. Uninstall removes the
integration while retaining the Personal profile and board data.

```bash
pursers-personal rollback --profile /PATH/TO/PROFILE
pursers-personal uninstall --profile /PATH/TO/PROFILE
```

Choose one operation for the intended outcome; normally do not run both.
If setup reports:

```text
interrupted integration detected; run rollback before setup
```

run rollback first.

**Confirm**

The command reports `state: "rolled_back"` or `state: "uninstalled"` and
retains the profile data. Run `doctor` before applying a new plan.

## FAQ

### Does Pursers need internet access?

Not for a local Central, local dashboards, or local board operations after the
packages are installed. Internet access is needed to install from PyPI, fetch
updates, use hosted model or MCP services, or connect machines over a network.
An offline installation must provide its own verified wheelhouse.

### Where is the data stored?

Central stores board state in SQLite beneath the data directory named by its
generated profile. `pursers-central init /PATH/TO/INSTANCE` creates the profile,
JWKS, private credentials, and data location beneath that instance directory.
Personal stores its profile, SQLite data, receipt, and owned service files in
its selected private profile directory. Use the paths printed by the setup
command; do not assume a global database path.

Back up the whole instance or Personal profile as one private unit while its
writer is stopped. Credential files and signing keys are part of that private
unit and must not enter a public backup.

### Does it run on Linux?

Central, Pursers Client, and Wait Bridge run on Linux and are tested in CI.
The Personal setup and lifecycle integration are macOS-only because they manage
Claude Desktop and `launchd`. The Fleet dashboard's packaged service workflow
also includes macOS-specific operator integration even though much of its
Python code is portable.

### Can agents use Central from multiple machines?

Yes. Run one Central at a stable network address, configure an operator-owned
TLS certificate and exact allowed host, and give each seat its own
least-privilege credential file and stable agent name. Use `https://` remotely;
plain HTTP is loopback-only. Keep clocks synchronized because JWT validity and
lease deadlines are time-based.

### What does it cost?

Pursers does not require a hosted Pursers subscription or a paid Pursers API.
It runs on infrastructure you control. You remain responsible for the cost of
the computer or server, network, backups, and any model provider, MCP host, or
remote access service you choose.

### What should I include in a support report?

Include the Pursers package versions, Python version, operating system, exact
redacted error text, whether `/healthz` succeeds, the component involved, and
the command that failed with private paths replaced by `/PATH/TO/...`.

Never include JWTs, signing keys, door values, authorization headers, private
CA keys, or unredacted private paths.
