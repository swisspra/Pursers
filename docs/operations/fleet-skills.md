# Mong1 fleet skills for the Zed build

This runbook records the deliberately small skill set installed for the Mong1
fleet profile. The snapshot and probe evidence are from 2026-09-20. Install
counts are point-in-time values returned by `npx skills find`; they are not
quality scores.

## Discovery rule

The authoritative Codex documentation says that skills are discovered from:

1. `.agents/skills` from the current working directory up to the repository
   root;
2. `$HOME/.agents/skills` for user-wide skills;
3. `/etc/codex/skills` for machine-wide skills; and
4. bundled system skills.

Codex supports symlinked skill directories. It loads only each skill's name,
description, and path at startup, then reads the full `SKILL.md` only when the
skill is selected. Source: [OpenAI, Build skills](https://developers.openai.com/es-419/docs/build-skills).

The Mong1 wrapper also exposes `$HOME/.codex-mong1/skills` as profile root
`r0`. That wrapper-specific behavior is verified by the probes below; it is not
presented as a general Codex discovery rule. A fresh Codex process is the
reliable way to observe additions.

## Selected set

The fleet profile contains nine entries including its pre-existing
`token-thrift`; eight entries were added as symlinks in this operation. This is
below the ticket cap of 12.

| Skill | Source | Installs | Why selected | Installation |
|---|---|---:|---|---|
| `rust-best-practices` | `apollographql/skills` | 18.4K | Idiomatic Rust, ownership, errors, tests, Clippy, and performance for extension implementation | New copy, then profile symlink |
| `mcp-apps-engineering` | Existing curated local skill | N/A | MCP Apps protocol, SDK, host, security, and verification coverage | Existing shared skill; profile symlink |
| `mcpv2-migration-python` | Existing curated local skill | N/A | MCP 2026-07-28 and Python SDK v2 reference for protocol-side work | Existing shared skill; profile symlink |
| `frontend-design` | `anthropics/skills` | 902.2K | Intentional UI direction and implementation guidance | Existing shared skill; profile symlink |
| `emil-design-eng` | `emilkowalski/skills` | 281.8K | UI polish, interaction craft, and design-engineering review | Existing shared skill; profile symlink |
| `design-critique` | `anthropics/knowledge-work-plugins` | 4.7K | Structured critique of usability, hierarchy, and consistency | New copy, then profile symlink |
| `ux-copy` | `anthropics/knowledge-work-plugins` | 3.9K | Microcopy, errors, empty states, and calls to action | New copy, then profile symlink |
| `accessibility-review` | `anthropics/knowledge-work-plugins` | 3.6K | WCAG-oriented keyboard, contrast, touch-target, and screen-reader review | New copy, then profile symlink |
| `token-thrift` | Existing Mong1 profile skill | N/A | Required fleet communication discipline; already present | Unchanged |

No dedicated Zed, ACP, or Rust-to-WASM skill passed the selection bar:

- `zed-industries/zed@brand-writer` is official but covers brand copy, not Zed
  extension development. The extension-development search results had only
  20 installs or fewer and were not official.
- the only WASM result above 1K installs,
  `marimo-team/skills@wasm-compatibility` (2.6K), is specifically about Python
  packages in marimo/Pyodide notebooks. The Rust/WASM-specific results had 67
  installs or fewer.
- ACP results were unrelated implementations or had at most 97 installs; none
  was an official Zed Agent Client Protocol skill.
- `anthropics/skills@mcp-builder` (116.3K) was not added because the two
  existing MCP skills are more specific to the fleet's Apps and v2 work.

Rejecting mismatched skills keeps activation unambiguous; Zed and ACP behavior
must continue to come from the checked-in product specification and official
project documentation.

## Vetting record

Every selected `SKILL.md` was read before installation or linking. The new
source snapshots inspected were:

- `apollographql/skills@c288eb80629dd2309eed81f23d693f66a452d043`
- `anthropics/knowledge-work-plugins@67e216dbe5621470581dd3d944df22d1cc8231ce`

The four newly copied skills contain no executable scripts. Their installed
directories were byte-for-byte compared with the inspected source directories
using `diff -qr` after installation.

Among the four reused shared skills, only `mcp-apps-engineering` contains
scripts. Both `scripts/audit_mcp_app.py` and
`scripts/test_audit_mcp_app.py` were read in full. They are a local static file
scanner and its local temporary-directory unit tests; imports are Python
standard-library modules only. They do not invoke subprocesses, open sockets,
make HTTP requests, read credentials, or upload data. The other reused skills
contain instructions/references only. No unvetted executable source was
installed.

The installer reported `Safe`/zero Socket alerts for all four new skills; its
Snyk labels were Low Risk for Rust, critique, and copy, and Med Risk for the
accessibility skill. Those automated labels supplement, but do not replace,
the manual inspection above.

## Exact install and rollback commands

Installation used the standard skills CLI for sources absent from the shared
catalog, then explicit symlinks for profile exposure:

```sh
npx -y skills add apollographql/skills -g -a codex -s rust-best-practices -y
npx -y skills add anthropics/knowledge-work-plugins -g -a codex -s design-critique ux-copy accessibility-review -y

CODEX_HOME="$HOME/.codex-mong1"
ln -s "$HOME/.agents/skills/frontend-design" "$CODEX_HOME/skills/frontend-design"
ln -s "$HOME/.agents/skills/emil-design-eng" "$CODEX_HOME/skills/emil-design-eng"
ln -s "$HOME/.agents/skills/mcp-apps-engineering" "$CODEX_HOME/skills/mcp-apps-engineering"
ln -s "$HOME/.agents/skills/mcpv2-migration-python" "$CODEX_HOME/skills/mcpv2-migration-python"
ln -s "$HOME/.agents/skills/rust-best-practices" "$CODEX_HOME/skills/rust-best-practices"
ln -s "$HOME/.agents/skills/design-critique" "$CODEX_HOME/skills/design-critique"
ln -s "$HOME/.agents/skills/ux-copy" "$CODEX_HOME/skills/ux-copy"
ln -s "$HOME/.agents/skills/accessibility-review" "$CODEX_HOME/skills/accessibility-review"
```

Rollback removes only links created by this operation, then the four newly
copied shared skills. It does not remove or edit the four pre-existing shared
skills or `token-thrift`:

```sh
CODEX_HOME="$HOME/.codex-mong1"
unlink "$CODEX_HOME/skills/frontend-design"
unlink "$CODEX_HOME/skills/emil-design-eng"
unlink "$CODEX_HOME/skills/mcp-apps-engineering"
unlink "$CODEX_HOME/skills/mcpv2-migration-python"
unlink "$CODEX_HOME/skills/rust-best-practices"
unlink "$CODEX_HOME/skills/design-critique"
unlink "$CODEX_HOME/skills/ux-copy"
unlink "$CODEX_HOME/skills/accessibility-review"
npx -y skills remove -g -a codex rust-best-practices design-critique ux-copy accessibility-review -y
```

Precise external changes:

- copied four new directories under `$HOME/.agents/skills/`:
  `rust-best-practices`, `design-critique`, `ux-copy`, and
  `accessibility-review`;
- updated the corresponding four records in `$HOME/.agents/.skill-lock.json`;
- created the eight symlinks shown above under
  `$HOME/.codex-mong1/skills/`; and
- did not touch `$HOME/.codex`, change any model/reasoning/service setting, or
  modify/remove an existing skill.

## Before/after discovery proof

Both probes used a new scratch directory under the worker-owned home cache and
an ephemeral `codex-profile cli mong1 exec` process. The prompt and model were
identical. The output file, rather than the CLI's surrounding diagnostics, is
the literal evidence.

```sh
PROBE_DIR=$(mktemp -d "$HOME/.cache/mong1-worker-9/tmp/skill-probe.XXXXXX")
codex-profile cli mong1 exec --ephemeral --skip-git-repo-check \
  -C "$PROBE_DIR" -c 'model_reasoning_effort="low"' \
  --output-last-message <WORKER_ROOT>/runs/<PROBE_OUTPUT>.txt \
  'List every available skill name and its SKILL.md path exactly as exposed in your current instructions. Do not call tools. Output one line per skill as NAME<TAB>PATH, with no commentary.'
```

Before (literal 48-line output):

```text
imagegen	r2/imagegen/SKILL.md
openai-docs	r2/openai-docs/SKILL.md
plugin-creator	r2/plugin-creator/SKILL.md
skill-creator	r2/skill-creator/SKILL.md
skill-installer	r2/skill-installer/SKILL.md
agents-sdk	r1/agents-sdk/SKILL.md
animate	r1/animate/SKILL.md
animate-expo	r1/animate-expo/SKILL.md
animation-vocabulary	r1/animation-vocabulary/SKILL.md
apple-design	r1/apple-design/SKILL.md
ask-sonner	r1/ask-sonner/SKILL.md
azure-aigateway	r1/azure-aigateway/SKILL.md
cloudflare	r1/cloudflare/SKILL.md
cloudflare-email-service	r1/cloudflare-email-service/SKILL.md
cloudflare-one	r1/cloudflare-one/SKILL.md
cloudflare-one-migrations	r1/cloudflare-one-migrations/SKILL.md
documents:documents	r5/documents/26.909.12148/skills/documents/SKILL.md
durable-objects	r1/durable-objects/SKILL.md
ego-browser	r1/ego-browser/SKILL.md
emil-design-eng	r1/emil-design-eng/SKILL.md
find-animation-opportunities	r1/find-animation-opportunities/SKILL.md
find-skills	r1/find-skills/SKILL.md
frontend-design	r1/frontend-design/SKILL.md
hyperframes	r1/hyperframes/SKILL.md
i-have-adhd	r1/i-have-adhd/SKILL.md
improve-animations	r1/improve-animations/SKILL.md
mcp-apps-engineering	r1/mcp-apps-engineering/SKILL.md
mcpv2-migration-python	r1/mcpv2-migration-python/SKILL.md
mobile-native	r1/mobile-native/SKILL.md
pdf:pdf	r5/pdf/26.909.12148/skills/pdf/SKILL.md
pick-ui-library	r1/pick-ui-library/SKILL.md
plugin-management:plugin-management	r4/plugin-management/0.1.0/skills/plugin-management/SKILL.md
presentations:Presentations	r5/presentations/26.909.12148/skills/presentations/SKILL.md
prototype	r1/prototype/SKILL.md
review-animations	r1/review-animations/SKILL.md
sandbox-sdk	r1/sandbox-sdk/SKILL.md
spreadsheets:Spreadsheets	r6/spreadsheets/SKILL.md
spreadsheets:excel-live-control	r6/excel-live-control/SKILL.md
template-creator:template-creator	r5/template-creator/26.909.12148/skills/template-creator/SKILL.md
token-thrift	r0/token-thrift/SKILL.md
token-thrift:token-thrift	r1/token-thrift/skills/token-thrift/SKILL.md
turnstile-spin	r1/turnstile-spin/SKILL.md
ui-design-animation	r1/ui-design-animation/SKILL.md
visualize:visualize	r3/visualize/1.0.37/skills/visualize/SKILL.md
web-perf	r1/web-perf/SKILL.md
workers-best-practices	r1/workers-best-practices/SKILL.md
wrangler	r1/wrangler/SKILL.md
write-swift	r1/write-swift/SKILL.md
```

After (literal 52-line output):

```text
imagegen	r2/imagegen/SKILL.md
openai-docs	r2/openai-docs/SKILL.md
plugin-creator	r2/plugin-creator/SKILL.md
skill-creator	r2/skill-creator/SKILL.md
skill-installer	r2/skill-installer/SKILL.md
accessibility-review	r0/accessibility-review/SKILL.md
agents-sdk	r1/agents-sdk/SKILL.md
animate	r1/animate/SKILL.md
animate-expo	r1/animate-expo/SKILL.md
animation-vocabulary	r1/animation-vocabulary/SKILL.md
apple-design	r1/apple-design/SKILL.md
ask-sonner	r1/ask-sonner/SKILL.md
azure-aigateway	r1/azure-aigateway/SKILL.md
cloudflare	r1/cloudflare/SKILL.md
cloudflare-email-service	r1/cloudflare-email-service/SKILL.md
cloudflare-one	r1/cloudflare-one/SKILL.md
cloudflare-one-migrations	r1/cloudflare-one-migrations/SKILL.md
design-critique	r0/design-critique/SKILL.md
documents:documents	r5/documents/26.909.12148/skills/documents/SKILL.md
durable-objects	r1/durable-objects/SKILL.md
ego-browser	r1/ego-browser/SKILL.md
emil-design-eng	r0/emil-design-eng/SKILL.md
find-animation-opportunities	r1/find-animation-opportunities/SKILL.md
find-skills	r1/find-skills/SKILL.md
frontend-design	r0/frontend-design/SKILL.md
hyperframes	r1/hyperframes/SKILL.md
i-have-adhd	r1/i-have-adhd/SKILL.md
improve-animations	r1/improve-animations/SKILL.md
mcp-apps-engineering	r0/mcp-apps-engineering/SKILL.md
mcpv2-migration-python	r0/mcpv2-migration-python/SKILL.md
mobile-native	r1/mobile-native/SKILL.md
pdf:pdf	r5/pdf/26.909.12148/skills/pdf/SKILL.md
pick-ui-library	r1/pick-ui-library/SKILL.md
plugin-management:plugin-management	r4/plugin-management/0.1.0/skills/plugin-management/SKILL.md
presentations:Presentations	r5/presentations/26.909.12148/skills/presentations/SKILL.md
prototype	r1/prototype/SKILL.md
review-animations	r1/review-animations/SKILL.md
rust-best-practices	r0/rust-best-practices/SKILL.md
sandbox-sdk	r1/sandbox-sdk/SKILL.md
spreadsheets:Spreadsheets	r6/spreadsheets/SKILL.md
spreadsheets:excel-live-control	r6/excel-live-control/SKILL.md
template-creator:template-creator	r5/template-creator/26.909.12148/skills/template-creator/SKILL.md
token-thrift	r0/token-thrift/SKILL.md
token-thrift:token-thrift	r1/token-thrift/skills/token-thrift/SKILL.md
turnstile-spin	r1/turnstile-spin/SKILL.md
ui-design-animation	r1/ui-design-animation/SKILL.md
ux-copy	r0/ux-copy/SKILL.md
visualize:visualize	r3/visualize/1.0.37/skills/visualize/SKILL.md
web-perf	r1/web-perf/SKILL.md
workers-best-practices	r1/workers-best-practices/SKILL.md
wrangler	r1/wrangler/SKILL.md
write-swift	r1/write-swift/SKILL.md
```

The new skills are the four added `r0` lines. The four reused skills changed
from their general user root (`r1`) to the explicit Mong1 profile root (`r0`),
which proves that the profile links are active rather than merely present on
disk.
