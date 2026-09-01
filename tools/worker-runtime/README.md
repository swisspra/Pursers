# Headless worker runtime

`pursers-worker` runs one continuously re-arming board seat against an
OpenAI-compatible chat-completions endpoint. It uses the same JWT seat,
registry, claim, lease, submission, and independent-review flow as an
interactive worker. Provision the seat first with `seat_admin`.

The config is private local input, never board state or a web form. Both the
config and referenced secret files must be regular files with mode `0600`.

```toml
boards = "registry"

[seat]
agent_name = "worker-api-1"
central_url = "https://127.0.0.1:8766/mcp"
token_file = "/private/path/seat.jwt"

[claim]
max_tier = "standard" # light | standard | heavy; default heavy
require_assigned_only = false

[llm]
base_url = "https://proxy.example/v1"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model = "configured-model"
max_tokens = 4096
max_iterations = 40
command_timeout_s = 120
```

```bash
chmod 600 /private/path/worker.toml /private/path/seat.jwt
export OPENAI_COMPATIBLE_API_KEY='set-in-private-shell-or-secret-store'
python tools/worker-runtime/pursers_worker.py /private/path/worker.toml
```

Instead of `api_key_env`, use `api_key_file = "/private/path/proxy.key"`.
Never put a key inline in the config. `boards` may be `"registry"` or a JSON
array / TOML string array of board IDs.

The model receives the static worker directive first, then board context, then
the dynamic ticket. Available tools are bounded file reads/writes, timeboxed
shell commands jailed to the registry work directory, submit, and give-up.
Commands (not their output) are recorded in a mode-`0600` local session log.
The shell receives a non-secret environment with `HOME` and `TMPDIR` reset to
the assigned work directory. Configured token/key values are redacted from
model-visible output and logs; on macOS, the subprocess is additionally denied
read access to the configured secret files. The runtime never reviews or merges
its own work.
