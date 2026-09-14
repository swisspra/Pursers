# Pursers 5.0.0b1 first-run report

## Result

A fresh user cannot complete the current README quickstart or reach the first
Central health response with the supplied archived beta wheelhouse. The first
pass stopped at the required first blocker: four of the six product wheels
named by the README are absent.

A second pass confirmed three independent release-input problems before any
Pursers profile could be created:

1. The guide asks for `SHA256SUMS.txt`, but the archive contains
   `SHA256SUMS`.
2. The archive contains only `pursers_client` and
   `pursers_wait_bridge` product wheels. It does not contain `pursers`,
   `pursers_central`, `pursers_personal`, or
   `pursers_personal_import`.
3. The two product-wheel hashes in the archive differ from the hashes published
   in Getting Started. The archive's locked dependencies also include CPython
   3.12 platform wheels, so `python3` 3.14—explicitly supported by the
   documentation—cannot install `./*.whl`.

No real Claude Desktop or Codex configuration was read or changed. No existing
Pursers profile was read. No Central process was started.

## Scope and method

This run used a fresh public-repository clone at
`origin/main@c5b80abc168c5f25a8d7f3a6ec338cb084ac3ba7`. The tester had not authored
`README.md` or `docs/GETTING-STARTED.md` and did not inspect source code
during the walkthrough.

The not-yet-published release URLs were replaced only with the ticket-specified
read-only archive:

```text
/PATH/TO/wheelhouse-approved-a34844ff
```

The host runtime does not permit replacing `HOME`. Coordinator decision
`CQ-c3712506fc7f7f70`, later recorded as `AN-000000000746`, approved
equivalent isolation: a fresh working directory, fresh venv, private
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, and
`XDG_STATE_HOME`, `PIP_CONFIG_FILE=/dev/null`, no `PYTHONPATH`, and no
inherited `ONBOARD_CENTRAL_TOKEN` or `ONBOARD_CENTRAL_TOKEN_FILE`.
Existing `~/.pursers` and host profiles were not read, apart from the
explicitly authorized release archive.

A genuinely fresh user's `HOME` would be empty. Because this host could not
replace `HOME`, any documented command that attempted a default path beneath
`~/.pursers` or `~/Library` was treated as a finding rather than trusted as
isolated state. No Pursers executable reached such a path: installation stopped
before the executables existed. Pip did attempt `~/Library/Caches/pip`; that
attempt was blocked and is recorded below.

The exact shell boundary for the final blocker reproduction was:

```console
example-command: env -u PYTHONPATH -u ONBOARD_CENTRAL_TOKEN -u ONBOARD_CENTRAL_TOKEN_FILE \
    XDG_CONFIG_HOME=/PATH/TO/xdg/config \
    XDG_DATA_HOME=/PATH/TO/xdg/data \
    XDG_CACHE_HOME=/PATH/TO/xdg/cache \
    XDG_STATE_HOME=/PATH/TO/xdg/state \
    PIP_CONFIG_FILE=/dev/null zsh -f

example-command: test -z "${ONBOARD_CENTRAL_TOKEN+x}" && \
    test -z "${ONBOARD_CENTRAL_TOKEN_FILE+x}" && \
    test -z "${PYTHONPATH+x}" && echo isolation_env=pass
isolation_env=pass
```

The `~/.pursers` directory mtime was
`1789388582 Sep 14 19:23:02 2026` immediately before and after the final
reproduction. No entry was created, removed, or rewritten there. The only
intentional access beneath that root was the ticket-authorized read of
`releases/v5.0.0b1/wheelhouse-approved-a34844ff`. Because installation failed
before a Pursers executable existed, no Pursers process could read a default
profile.

Commands below preserve what was typed, with private machine paths normalized
to `/PATH/TO/...`. Wall time is zsh's elapsed time rounded to hundredths of a
second. Wrapped shell commands count once.

## Scorecard

| Measure | Result |
|---|---:|
| Time to create the first fresh venv | 1.05 s |
| Time from venv start to the first blocking install error | 1.36 s |
| Time to first Central health | Not achieved |
| Time to first ticket | Not achieved |
| Timed walkthrough commands in the authoritative two-pass rerun | 46 |
| Timed isolation, instrumentation, snippet-write, and teardown actions | 12 |
| Discrete shell, configuration, host, and MCP actions documented through sections 1–7 | 53 |
| Items a first-time user must already know or decide | 16 |
| Release-input blockers | 4 |
| Host restart steps testable in this environment | 0 |

The documented-action count treats one wrapped command or one MCP tool call as
one action and counts each manual configuration edit or host restart as one. It
does not count explanatory prose.

## First pass: README 60-second quickstart

### Fresh environment

```console
example-command: python3 -m venv .venv
example-elapsed=1.05s status=0

example-command: . .venv/bin/activate

example-command: release=/PATH/TO/wheelhouse-approved-a34844ff
```

Outcome: **OK**. The venv was isolated. Selecting the archive path was the
ticket-authorized substitute for the unpublished release URL.

### Install the six named product wheels

```console
example-command: python -m pip install \
    "$release/pursers-5.0.0b1-py3-none-any.whl" \
    "$release/pursers_central-0.1.0a30-py3-none-any.whl" \
    "$release/pursers_client-0.1.0a23-py3-none-any.whl" \
    "$release/pursers_personal-5.0.0b1-py3-none-any.whl" \
    "$release/pursers_personal_import-5.0.0a3-py3-none-any.whl" \
    "$release/pursers_wait_bridge-0.1.0a16-py3-none-any.whl"
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers-5.0.0b1-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_central-0.1.0a30-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_personal-5.0.0b1-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_personal_import-5.0.0a3-py3-none-any.whl' looks like a filename, but the file does not exist
ERROR: Could not install packages due to an OSError: [Errno 2] No such file or directory: '/PATH/TO/wheelhouse-approved-a34844ff/pursers-5.0.0b1-py3-none-any.whl'
example-elapsed=0.31s status=1
```

Outcome: **failed; first normal-user blocker**. Per the ticket, the first pass
stopped here. A user would need an undocumented source for four missing wheels
before `pursers-personal setup` could exist.

The authoritative isolation-compliance rerun produced the same missing-wheel
error with `isolation_env=pass`; its fresh venv took 1.05 s and the failing
install took 0.31 s. These confirm the blocker independently of inherited
Pursers token variables.

## Second pass: remaining sections and stumbles

### Section 1: checksum and install

The unpublished-download substitution copied the supplied archive into the
fresh release directory in 0.01 s. The command printed by the guide then
failed:

```console
example-command: shasum -a 256 -c SHA256SUMS.txt
shasum: SHA256SUMS.txt: No such file or directory
example-elapsed=0.01s
```

Using the filename that actually exists proves that the archive is internally
consistent, but does not make it the release bundle described by the guide:

```console
example-command: shasum -a 256 -c SHA256SUMS
annotated_types-0.8.0-py3-none-any.whl: OK
...
pursers_client-0.1.0a23-py3-none-any.whl: OK
pursers_wait_bridge-0.1.0a16-py3-none-any.whl: OK
...
uvicorn-0.52.4-py3-none-any.whl: OK
example-elapsed=0.05s
```

The archive has 30 wheels, but only two are Pursers product wheels. Its manifest
identifies source commit
`28f81308d1cf3d40c4ed38091cc02d9c7d0827aa` and only these product
requirements:

```text
pursers-client==0.1.0a23
pursers-wait-bridge==0.1.0a16
```

The documentation publishes these hashes:

```text
e2314191a354ab2ab0d0020c1ef80cc0049909eaf5219fcdff0c0f24847b677e  pursers_client-0.1.0a23-py3-none-any.whl
102d6eb35daae393c8fef589c8b3e60b02ab985d01856f64afb99ba3ba94834f  pursers_wait_bridge-0.1.0a16-py3-none-any.whl
```

The supplied archive contains different bytes:

```text
94b20f5e542df673da6b2903adeb329be10c2a70d977baa3d1b8206680dae0fd  pursers_client-0.1.0a23-py3-none-any.whl
1a0e94f1fe88a0b9d118d7ca02eb26d4c69d4d7b13a26e08b22852a92994bcc4  pursers_wait_bridge-0.1.0a16-py3-none-any.whl
```

The machine's default interpreter is within the documented range:

```console
example-command: python3 --version
Python 3.14.7
example-elapsed=0.01s

example-command: python3 -m venv .venv
example-elapsed=1.43s status=0

example-command: .venv/bin/python -m pip install --upgrade pip
Requirement already satisfied: pip in ./.venv/lib/python3.14/site-packages (26.2.1)
example-elapsed=0.59s status=0

example-command: .venv/bin/python -m pip install ./*.whl
Processing ./annotated_types-0.8.0-py3-none-any.whl
Processing ./anyio-4.15.1-py3-none-any.whl
Processing ./attrs-26.1.0-py3-none-any.whl
ERROR: cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl is not a supported wheel on this platform.
example-elapsed=0.20s status=1
```

Outcome: **failed**. A normal Python 3.14 user must already know to install
Python 3.12 even though the compatibility badge and prerequisites include
3.14.

For the second pass only, the available Python 3.12 interpreter was used to
find later failures:

```console
example-command: python3.12 -m venv .venv
example-elapsed=1.11s status=0

example-command: .venv/bin/python -m pip install --upgrade pip
Successfully installed pip-26.2.1
example-elapsed=1.36s status=0

example-command: .venv/bin/python -m pip install ./*.whl
Successfully installed ... pursers-client-0.1.0a23 pursers-wait-bridge-0.1.0a16 ...
example-elapsed=1.12s status=0

example-command: .venv/bin/pursers-personal --version
zsh: no such file or directory: .venv/bin/pursers-personal
example-elapsed=0.00s status=127

example-command: .venv/bin/pursers-wait-bridge --version
0.1.0a16
example-elapsed=0.33s status=0
```

`pip install --upgrade pip` also attempted the real default cache despite the
fresh XDG cache:

```text
WARNING: The directory '/PATH/TO/Library/Caches/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled.
```

That access was blocked, but a truly isolated walkthrough also needs an
explicit `PIP_CACHE_DIR`.

### Section 2: owner profile

The private project, profile, and host-config paths could be created. Both the
preview and apply commands then failed at the missing executable:

```console
example-command: .venv/bin/pursers-personal --json setup \
    --project "$PURSERS_PROJECT" \
    --profiles-root "$PURSERS_PROFILES" \
    --port 8766 \
    --host-id claude-desktop \
    --session owner \
    --host-config "$CLAUDE_CONFIG"
zsh: no such file or directory: .venv/bin/pursers-personal
example-elapsed=0.00s

example-command: .venv/bin/pursers-personal --json setup \
    --project "$PURSERS_PROJECT" \
    --profiles-root "$PURSERS_PROFILES" \
    --port 8766 \
    --host-id claude-desktop \
    --session owner \
    --host-config "$CLAUDE_CONFIG" \
    --apply
zsh: no such file or directory: .venv/bin/pursers-personal
example-elapsed=0.00s
```

Outcome: **blocked**. Consequently there was no generated `profile.json`,
token file, JWKS, issuer suffix, owner identity, or board ID to carry into later
sections.

### Section 3: Central and health

The documented beta.1 runtime command cannot import the absent Central package:

```console
example-command: .venv/bin/python -c "from pursers_central.pursers_central_runtime import main; main()" \
    --data-dir "$PURSERS_DATA_DIR" \
    --host 127.0.0.1 \
    --port 8766
Traceback (most recent call last):
  File "<string>", line 1, in <module>
ModuleNotFoundError: No module named 'pursers_central'
example-elapsed=0.01s
```

Outcome: **blocked**. No health response attributable to this run existed. The
README also tells the user to run `pursers-personal central --project "$PWD"`,
while Getting Started says that command is the Personal-embedded service and
that its `/healthz` returns 404. The two entry points promise different
outcomes.

### Sections 4 and 5: host configuration

The Claude Desktop JSON and Codex TOML examples were written only to disposable
files. Their syntax is valid:

```console
example-command: .venv/bin/python -m json.tool /PATH/TO/claude_snippet.json
{
    "mcpServers": {
        "pursers": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote@0.14.0",
                "http://127.0.0.1:8766/mcp",
                "--protocol",
                "auto",
                "--header-file",
                "/PATH/TO/private/pursers-owner.headers"
            ]
        }
    }
}
example-elapsed=0.02s

example-command: .venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("/PATH/TO/codex_snippet.toml").read_text()); print("TOML OK")'
TOML OK
example-elapsed=0.02s
```

Outcome: **syntax OK; integration untestable**. No token was generated, so the
private header file and `PURSERS_OWNER_TOKEN` could not be created. Per the
ticket, the real host configuration was not modified and the required host
restarts were not attempted.

### Sections 6 and 7: first ticket and worker

Outcome: **blocked by section 2**. The following required values did not exist:
`BOARD_ID`, `OWNER_AGENT_NAME`, `TOKEN_FILE`, `JWKS_FILE`,
`CENTRAL_JWT_ISSUER`, and the worker door. Therefore no authenticated
`board_onboard`, `board_invite`, `ticket_create`, `board_member_add`,
`pursers-wait-bridge join`, or `a2a_wait` call could be made without
fabricating product state. No credential or invite was printed.

This also prevents measuring time-to-first-ticket. A static or fabricated MCP
response would not answer the operator's acceptance question.

### Section 8: AionUi ZIP

The guide's exact-tag checkout fails before a ZIP can be built:

```console
example-command: git clone --branch v5.0.0b1 --depth 1 https://github.com/swisspra/Pursers.git pursers-source
Cloning into 'pursers-source'...
fatal: Remote branch v5.0.0b1 not found in upstream origin
example-elapsed=0.74s
```

Outcome: **blocked**. The guide does explain that AionUi 2.2.1 has no arbitrary
local-ZIP import route, but the documented disposable AionCore verification is
also unavailable until the tag exists.

## Authoritative chronological transcript

This is the complete rerun requested by the first review. Earlier command
excerpts in this report explain individual findings; this section alone is the
authoritative count. It contains exactly 46 timed walkthrough commands. Each
pass ran in one persistent shell inside the approved isolation boundary, so
exports and venv activation persisted exactly as they would in a user's
terminal. Paths are normalized after execution.

### Isolation, instrumentation, file-write, and teardown actions

These 12 actions surround the 46 walkthrough commands. They are kept separate
because they implement the test harness rather than the published guide.

| # | Exact action, with private paths normalized | Seconds | Outcome |
|---:|---|---:|---|
| H1 | `/usr/bin/time -p mkdir -p /PATH/TO/rerun/first/xdg/{config,data,cache,state} /PATH/TO/rerun/first/run` | 0.00 | OK; empty first-pass roots created |
| H2 | `/usr/bin/time -p stat -f '%m %Sm' /PATH/TO/operator/.pursers` | 0.00 | OK; `1789388582 Sep 14 19:23:02 2026` |
| H3 | `env -u PYTHONPATH -u ONBOARD_CENTRAL_TOKEN -u ONBOARD_CENTRAL_TOKEN_FILE XDG_CONFIG_HOME=/PATH/TO/rerun/first/xdg/config XDG_DATA_HOME=/PATH/TO/rerun/first/xdg/data XDG_CACHE_HOME=/PATH/TO/rerun/first/xdg/cache XDG_STATE_HOME=/PATH/TO/rerun/first/xdg/state PIP_CONFIG_FILE=/dev/null zsh -f` | 1.00 | OK; persistent first-pass shell started |
| H4 | `zmodload zsh/datetime` and define `run_step` to print the command, elapsed seconds, and exit status | 0.25 combined | OK; instrumentation only |
| H5 | `exit` | 0.00 | OK; first-pass shell closed immediately after blocker |
| H6 | `/usr/bin/time -p mkdir -p /PATH/TO/rerun/second/xdg/{config,data,cache,state} /PATH/TO/rerun/second/{run,private,project}` | 0.00 | OK; empty second-pass roots created |
| H7 | `env -u PYTHONPATH -u ONBOARD_CENTRAL_TOKEN -u ONBOARD_CENTRAL_TOKEN_FILE XDG_CONFIG_HOME=/PATH/TO/rerun/second/xdg/config XDG_DATA_HOME=/PATH/TO/rerun/second/xdg/data XDG_CACHE_HOME=/PATH/TO/rerun/second/xdg/cache XDG_STATE_HOME=/PATH/TO/rerun/second/xdg/state PIP_CONFIG_FILE=/dev/null zsh -f` | 1.00 | OK; persistent second-pass shell started |
| H8 | `zmodload zsh/datetime` and define the same `run_step` function | 0.25 combined | OK; instrumentation only |
| H9 | `apply_patch` created `/PATH/TO/private/claude_snippet.json` and `/PATH/TO/private/codex_snippet.toml` from the exact documented snippets | 0.10 | OK; disposable files only |
| H10 | `exit` | 0.00 | OK; second-pass shell closed |
| H11 | `/usr/bin/time -p stat -f '%m %Sm' /PATH/TO/operator/.pursers` | 0.00 | OK; `1789388582 Sep 14 19:23:02 2026`, unchanged |
| H12 | `/usr/bin/time -p find /PATH/TO/rerun -depth -delete` | 0.31 | OK; all disposable state removed |

The exact timing function used inside each persistent shell was:

```zsh
zmodload zsh/datetime
run_step() {
  local cmd="$1"
  print -r -- "$ $cmd"
  local started=$EPOCHREALTIME
  eval "$cmd"
  local rc=$?
  local elapsed=$(( EPOCHREALTIME - started ))
  printf 'elapsed=%.2fs status=%d\n' "$elapsed" "$rc"
  return 0
}
```

### First pass — stop at the first blocker

```console
$ test -z "${ONBOARD_CENTRAL_TOKEN+x}" && test -z "${ONBOARD_CENTRAL_TOKEN_FILE+x}" && test -z "${PYTHONPATH+x}" && echo isolation_env=pass
isolation_env=pass
elapsed=0.00s status=0

$ python3 -m venv .venv
elapsed=1.05s status=0

$ . .venv/bin/activate
elapsed=0.00s status=0

$ release=/PATH/TO/wheelhouse-approved-a34844ff
elapsed=0.00s status=0

$ python -m pip install "$release/pursers-5.0.0b1-py3-none-any.whl" "$release/pursers_central-0.1.0a30-py3-none-any.whl" "$release/pursers_client-0.1.0a23-py3-none-any.whl" "$release/pursers_personal-5.0.0b1-py3-none-any.whl" "$release/pursers_personal_import-5.0.0a3-py3-none-any.whl" "$release/pursers_wait_bridge-0.1.0a16-py3-none-any.whl"
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers-5.0.0b1-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_central-0.1.0a30-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_personal-5.0.0b1-py3-none-any.whl' looks like a filename, but the file does not exist
WARNING: Requirement '/PATH/TO/wheelhouse-approved-a34844ff/pursers_personal_import-5.0.0a3-py3-none-any.whl' looks like a filename, but the file does not exist
Processing /PATH/TO/wheelhouse-approved-a34844ff/pursers-5.0.0b1-py3-none-any.whl
ERROR: Could not install packages due to an OSError: [Errno 2] No such file or directory: '/PATH/TO/wheelhouse-approved-a34844ff/pursers-5.0.0b1-py3-none-any.whl'
elapsed=0.31s status=1
```

Outcome: the first pass stopped here, as required.

### Second pass — collect the remaining stumbles

```console
$ test -z "${ONBOARD_CENTRAL_TOKEN+x}" && test -z "${ONBOARD_CENTRAL_TOKEN_FILE+x}" && test -z "${PYTHONPATH+x}" && echo isolation_env=pass
isolation_env=pass
elapsed=0.00s status=0

$ mkdir -p pursers-5.0.0b1
elapsed=0.00s status=0

$ cd pursers-5.0.0b1
elapsed=0.00s status=0

$ cp /PATH/TO/wheelhouse-approved-a34844ff/* .
elapsed=0.02s status=0

$ shasum -a 256 -c SHA256SUMS.txt
shasum: SHA256SUMS.txt: No such file or directory
elapsed=0.01s status=2

$ shasum -a 256 -c SHA256SUMS
annotated_types-0.8.0-py3-none-any.whl: OK
anyio-4.15.1-py3-none-any.whl: OK
attrs-26.1.0-py3-none-any.whl: OK
cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl: OK
click-8.5.0-py3-none-any.whl: OK
cryptography-50.0.1-cp311-abi3-macosx_11_0_arm64.whl: OK
h11-0.16.0-py3-none-any.whl: OK
httpcore2-2.12.0-py3-none-any.whl: OK
httpx2-2.12.0-py3-none-any.whl: OK
idna-3.19-py3-none-any.whl: OK
jsonschema-4.26.0-py3-none-any.whl: OK
jsonschema_specifications-2025.9.1-py3-none-any.whl: OK
mcp-2.1.1-py3-none-any.whl: OK
mcp_types-2.1.1-py3-none-any.whl: OK
opentelemetry_api-1.44.0-py3-none-any.whl: OK
pursers_client-0.1.0a23-py3-none-any.whl: OK
pursers_wait_bridge-0.1.0a16-py3-none-any.whl: OK
pycparser-3.0-py3-none-any.whl: OK
pydantic-2.13.5-py3-none-any.whl: OK
pydantic_core-2.46.5-cp312-cp312-macosx_11_0_arm64.whl: OK
pyjwt-2.14.0-py3-none-any.whl: OK
python_multipart-0.0.32-py3-none-any.whl: OK
referencing-0.37.0-py3-none-any.whl: OK
rpds_py-2026.6.3-cp312-cp312-macosx_11_0_arm64.whl: OK
sse_starlette-3.4.11-py3-none-any.whl: OK
starlette-1.6.0-py3-none-any.whl: OK
truststore-0.10.4-py3-none-any.whl: OK
typing_extensions-4.16.0-py3-none-any.whl: OK
typing_inspection-0.4.4-py3-none-any.whl: OK
uvicorn-0.52.4-py3-none-any.whl: OK
elapsed=0.04s status=0

$ python3 --version
Python 3.14.7
elapsed=0.01s status=0

$ python3 -m venv .venv
elapsed=1.43s status=0

$ .venv/bin/python -m pip install --upgrade pip
Requirement already satisfied: pip in ./.venv/lib/python3.14/site-packages (26.2.1)
elapsed=0.59s status=0

$ .venv/bin/python -m pip install ./*.whl
Processing ./annotated_types-0.8.0-py3-none-any.whl
Processing ./anyio-4.15.1-py3-none-any.whl
Processing ./attrs-26.1.0-py3-none-any.whl
ERROR: cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl is not a supported wheel on this platform.
elapsed=0.20s status=1

$ command -v python3.12
/PATH/TO/python3.12
elapsed=0.00s status=0

$ mv .venv .venv-py314-failed
elapsed=0.00s status=0

$ python3.12 -m venv .venv
elapsed=1.11s status=0

$ .venv/bin/python -m pip install --upgrade pip
WARNING: The directory '/PATH/TO/Library/Caches/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
Successfully installed pip-26.2.1
elapsed=1.36s status=0

$ .venv/bin/python -m pip install ./*.whl
Successfully installed annotated-types-0.8.0 anyio-4.15.1 attrs-26.1.0 cffi-2.1.1 click-8.5.0 cryptography-50.0.1 h11-0.16.0 httpcore2-2.12.0 httpx2-2.12.0 idna-3.19 jsonschema-4.26.0 jsonschema-specifications-2025.9.1 mcp-2.1.1 mcp-types-2.1.1 opentelemetry-api-1.44.0 pursers-client-0.1.0a23 pursers-wait-bridge-0.1.0a16 pycparser-3.0 pydantic-2.13.5 pydantic-core-2.46.5 pyjwt-2.14.0 python-multipart-0.0.32 referencing-0.37.0 rpds-py-2026.6.3 sse-starlette-3.4.11 starlette-1.6.0 truststore-0.10.4 typing-extensions-4.16.0 typing-inspection-0.4.4 uvicorn-0.52.4
elapsed=1.12s status=0

$ .venv/bin/pursers-personal --version
(eval):1: no such file or directory: .venv/bin/pursers-personal
elapsed=0.00s status=127

$ .venv/bin/pursers-wait-bridge --version
0.1.0a16
elapsed=0.33s status=0

$ export PURSERS_PROJECT=/PATH/TO/rerun/second/project
elapsed=0.00s status=0

$ export PURSERS_PROFILES=/PATH/TO/rerun/second/private/pursers-profiles
elapsed=0.00s status=0

$ export CLAUDE_CONFIG=/PATH/TO/rerun/second/private/claude_desktop_config.json
elapsed=0.00s status=0

$ mkdir -p "$PURSERS_PROJECT" "$PURSERS_PROFILES"
elapsed=0.00s status=0

$ chmod 700 "$PURSERS_PROFILES"
elapsed=0.00s status=0

$ .venv/bin/pursers-personal --json setup --project "$PURSERS_PROJECT" --profiles-root "$PURSERS_PROFILES" --port 8766 --host-id claude-desktop --session owner --host-config "$CLAUDE_CONFIG"
(eval):1: no such file or directory: .venv/bin/pursers-personal
elapsed=0.00s status=127

$ .venv/bin/pursers-personal --json setup --project "$PURSERS_PROJECT" --profiles-root "$PURSERS_PROFILES" --port 8766 --host-id claude-desktop --session owner --host-config "$CLAUDE_CONFIG" --apply
(eval):1: no such file or directory: .venv/bin/pursers-personal
elapsed=0.00s status=127

$ export PURSERS_PROFILE=/PATH/TO/rerun/second/private/profile.json
elapsed=0.00s status=0

$ export PURSERS_DATA_DIR=/PATH/TO/rerun/second/private/central-data
elapsed=0.00s status=0

$ export TOKEN_FILE=/PATH/TO/rerun/second/private/credential.jwt
elapsed=0.00s status=0

$ export JWKS_FILE=/PATH/TO/rerun/second/private/credential.jwks.json
elapsed=0.00s status=0

$ export CENTRAL_JWT_ISSUER='http://127.0.0.1:8766/personal-issuer/PROFILE_ID'
elapsed=0.00s status=0

$ export CENTRAL_JWT_AUDIENCE='http://127.0.0.1:8766/mcp'
elapsed=0.00s status=0

$ export CENTRAL_JWKS_PATH="$JWKS_FILE"
elapsed=0.00s status=0

$ .venv/bin/python -c "from pursers_central.pursers_central_runtime import main; main()" --data-dir "$PURSERS_DATA_DIR" --host 127.0.0.1 --port 8766
Traceback (most recent call last):
  File "<string>", line 1, in <module>
ModuleNotFoundError: No module named 'pursers_central'
elapsed=0.01s status=1

$ curl --fail --silent http://127.0.0.1:8766/healthz
elapsed=0.01s status=52

$ .venv/bin/python -m json.tool ../../private/claude_snippet.json
{
    "mcpServers": {
        "pursers": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote@0.14.0",
                "http://127.0.0.1:8766/mcp",
                "--protocol",
                "auto",
                "--header-file",
                "/PATH/TO/private/pursers-owner.headers"
            ]
        }
    }
}
elapsed=0.03s status=0

$ .venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("../../private/codex_snippet.toml").read_text()); print("TOML OK")'
TOML OK
elapsed=0.03s status=0

$ test -f "$TOKEN_FILE"
elapsed=0.00s status=1

$ test -f "$JWKS_FILE"
elapsed=0.00s status=1

$ test -f "$PURSERS_PROFILE"
elapsed=0.00s status=1

$ test -x .venv/bin/pursers-door
elapsed=0.00s status=0

$ cd ../..
elapsed=0.00s status=0

$ git clone --branch v5.0.0b1 --depth 1 https://github.com/swisspra/Pursers.git pursers-source
Cloning into 'pursers-source'...
fatal: Remote branch v5.0.0b1 not found in upstream origin
elapsed=0.74s status=128
```

Commands from sections 4–7 that need a generated token, JWKS, profile, board,
owner agent, invite, or door were not executed because the three prerequisite
checks above returned status 1. Executing those commands would have required
fabricating product state. Their outcome is **blocked by section 2**, not
untested success. The real Claude Desktop and Codex restart actions remain
untestable in this environment, as required by the ticket.

## What a user must already know

The walkthrough requires at least these 16 decisions or concepts before the
first worker can wait for a ticket:

1. Which supported Python executable is compatible with the supplied archive.
2. Whether a wheelhouse is a release-asset set or a platform-locked offline
   dependency bundle.
3. Which checksum filename is authoritative.
4. Which local directory should represent the project.
5. Where private Pursers profiles should live.
6. How to find an unused loopback port and update every derived URL together.
7. Where Claude Desktop's configuration file lives.
8. How to merge JSON without deleting existing MCP servers.
9. How to map `files.token` and `files.jwks` from `profile.json` to
   absolute paths.
10. How to keep a foreground Central process running while using a second
    terminal.
11. What `npx` and `mcp-remote` are and whether Node.js is installed.
12. How to add TOML without storing the bearer token.
13. The difference between agent name, agent ID, principal, invite, and worker
    door.
14. How to transfer an invite or door through a private channel.
15. How to register a stdio MCP server in the chosen host.
16. Where the AionUi application bundle and architecture-specific
    `aioncore` binary live.

## Prioritized fixes

### P0 — publish one coherent release bundle

The downloadable or archived b1 bundle must contain all six named product
wheels and `SHA256SUMS.txt`, and each byte must match the hashes printed in
Getting Started. Add this sentence immediately after the approved hashes:

> The release bundle contains exactly these six product wheels plus
> `SHA256SUMS.txt`; if any file is missing or any hash differs, stop because
> the bundle is not the approved b1 release.

Do not label the two-product, CPython-3.12 wait-bridge wheelhouse as the archive
for this six-product walkthrough.

### P0 — make the Python compatibility claim true for the supplied install path

Either publish dependencies compatible with every advertised interpreter or
avoid directing users to install a platform-locked `*.whl` set. If the
archive remains CPython 3.12-only, replace:

> Requires Python 3.11–3.14.

with:

> The archived offline b1 wheelhouse requires CPython 3.12 on Apple silicon;
> the six release product wheels support Python 3.11–3.14 when dependencies are
> resolved from their normal package index.

### P0 — make the README Central command match the promised health endpoint

The README currently starts the Personal-embedded service while promising
Central. Replace the final quickstart command and following sentence with:

> Continue with Getting Started sections 2–3 to record the generated profile
> paths and start `pursers_central.pursers_central_runtime`; then verify
> `http://127.0.0.1:8766/healthz`. The beta.1 Personal-embedded service is not
> the Central health endpoint.

### P1 — align the checksum contract

Use `SHA256SUMS.txt` everywhere, including the archived artifact, or change
the guide command to the actual filename. The preferred command remains:

```bash
shasum -a 256 -c SHA256SUMS.txt
```

### P1 — document isolation-complete pip behavior

Add this command beside the XDG isolation recipe used for reproducible
walkthroughs:

```bash
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
```

### P1 — state the pre-publication AionUi boundary before the clone command

Add:

> This section is runnable only after the `v5.0.0b1` tag exists on GitHub; a
> publication-pending checkout cannot build or verify the exact-tag ZIP.

## Teardown

The disposable shell was closed. Central never started, so there was no process
to kill. The temporary venvs, copied wheels, XDG roots, snippets, and failed
tag-clone directory were removed after recording this report. Existing host
configuration and Pursers state were unchanged.
