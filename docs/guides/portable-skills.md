# Portable Pursers skills

Pursers ships four small Agent Skills for role-specific workflows:

| Skill | Use it for | Does not grant |
| --- | --- | --- |
| `pursers-start` | Connect, choose an authorized board and role, verify identity and capabilities, and make a harmless read | Credentials, board membership, or write access |
| `pursers-work` | Claim an offered ticket, maintain its lease, validate an isolated branch, and submit exact evidence | Self-assignment, review, merge, or delivery |
| `pursers-review` | Independently verify an exact submission and record a verdict | Self-review, merge, or release authority |
| `pursers-operate` | Explicit coordinator, Butler, integration, and release-operator workflows | Administrative or release authority by itself |

The bundle manifest is `integrations/skills/manifest.json`. It carries its own
schema and bundle version; it is not a Pursers product-version declaration.
Each `SKILL.md` stays focused and links to role-specific references or canonical
guides instead of copying the full manuals into every model context.

## Zed first: preview, install, and verify

Zed's native Agent loads project skills from `.agents/skills/`. From a Pursers
source checkout, preview the exact filesystem changes for the target project:

```sh
python3 integrations/skills/manage.py plan \
  --host zed \
  --scope project \
  --project-root /PATH/TO/PROJECT
```

Review the JSON plan, then apply it explicitly:

```sh
python3 integrations/skills/manage.py install \
  --host zed \
  --scope project \
  --project-root /PATH/TO/PROJECT \
  --apply
```

Open **AI > Skills** in Zed, select the Project scope, and confirm all four
skills. A new worktree must be trusted before Zed loads its project skills.
Use a native **Zed Agent** thread: external ACP agents have their own skill
discovery and do not inherit Zed Agent skills.

The paths and behavior were checked against the official
[Zed Skills documentation](https://zed.dev/docs/ai/skills) on 2026-10-02.

## Codex and goose

Codex discovers project skills from `.agents/skills/` directories between the
current working directory and repository root, and user skills from
`$HOME/.agents/skills/`:

```sh
python3 integrations/skills/manage.py plan \
  --host codex --scope project --project-root /PATH/TO/PROJECT
python3 integrations/skills/manage.py install \
  --host codex --scope project --project-root /PATH/TO/PROJECT --apply
python3 integrations/skills/manage.py check \
  --host codex --scope project --project-root /PATH/TO/PROJECT
```

goose uses the shared project `.agents/skills/` path when its built-in Skills
extension is enabled:

```sh
python3 integrations/skills/manage.py plan \
  --host goose --scope project --project-root /PATH/TO/PROJECT
python3 integrations/skills/manage.py install \
  --host goose --scope project --project-root /PATH/TO/PROJECT --apply
python3 integrations/skills/manage.py check \
  --host goose --scope project --project-root /PATH/TO/PROJECT
goose skills list
```

`manage.py check` and `goose skills list` confirm that goose can discover the
installed files; they do not prove that a particular session activated the
Skills extension. Managed event seats use `--no-profile` for isolation, so the
runner explicitly starts the `developer,skills` built-ins and names
`$pursers-work` for workers or `$pursers-review` for reviewers. Goose then loads
that role's instructions on demand. Set `"enable_role_skills": false` in the
private event-seat configuration only when an operator intentionally needs the
legacy Developer-only session; the MCP role and all existing authorization
boundaries remain unchanged.

These paths were checked against the official
[Codex product skill documentation](https://learn.chatgpt.com/docs/build-skills)
and goose's
[built-in extension documentation](https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/using-extensions.md)
on 2026-10-02. Host support can change independently of Pursers. If the named
skill root or Skills extension is unavailable, keep using the canonical guides
and installed MCP tool descriptions and report the exact host version. Do not
guess another global directory.

User-scope installation is available only when explicitly selected with
`--scope user`. The manager installs into one named host target at a time and
never edits `AGENTS.md` or any MCP configuration.

## Collision, upgrade, and removal behavior

`plan`, `install` without `--apply`, and `remove` without `--apply` are previews.
An install is idempotent when the installed directory exactly matches the
bundle. If a skill directory already differs, the manager stops before changing
any files. After reviewing the conflict, an explicit
`--replace-conflicts --apply` preserves the old directory under
`.pursers-backups/` before installing the bundle.

Removal deletes only an exact current bundle copy. A modified directory is
preserved and reported as a conflict for manual resolution:

```sh
python3 integrations/skills/manage.py remove \
  --host zed --scope project --project-root /PATH/TO/PROJECT
python3 integrations/skills/manage.py remove \
  --host zed --scope project --project-root /PATH/TO/PROJECT --apply
```

Rollback is therefore either an exact removal or restoration of the preserved
collision backup. The manager never recursively removes the skill root, home
directory, project root, or unrelated skills.

## Onboarding sequence

The `pursers-start` workflow keeps the first connection narrow:

1. Identify the selected host and role.
2. Connect an existing endpoint with supported file-backed authentication;
   never print the credential.
3. Select only an authorized board or WORK project.
4. Onboard the stable identity and confirm the returned principal, agent ID,
   role, and capabilities.
5. Prefer `pursers://help/index` and the role/workflow resources when the
   connected server supports their schema. Otherwise use tool descriptions and
   the [connecting clients](connecting-clients.md) guide.
6. Run a harmless bounded read and preserve omissions and cursor information.
7. Explain the next action permitted by the verified capabilities. Read-only
   identities are not directed to claim work.

The discovery URIs are an additive contract owned by the discovery stream. A
missing or unsupported index does not block onboarding to a released server and
must not be presented as an empty healthy board.

## Review walkthroughs

Independent review should exercise applicable failure paths, not only inspect
the Markdown:

| Scenario | Expected evidence |
| --- | --- |
| Credentials absent | Connection fails without displaying or requesting a pasted credential. |
| Wrong board or read-only identity | The write is refused and the missing membership or capability is named. |
| Existing skill collision | Preview reports `conflict`; install makes no change without explicit replacement. |
| Expired or resumed lease | The ticket is refetched and no unowned claim or review continues. |
| Blocked baseline | The selected suite is actually run and compared with the approved base. |
| Review conflict | Approval stops and the authorized resolution path is requested. |
| Unauthorized release | Merge, tag, publish, deploy, restart, and credential changes are refused. |

Package validation must also exercise an isolated temporary target through
preview, install, repeat install, check, conflict preservation, and removal.

## Generated artifacts

This source change can make generated integration files stale. The integration
operator owns any required refresh of:

- `tools/aionui-extension/INTEGRATION_FILES.sha256`;
- `packages/personal/src/pursers_personal/resources/component-lock.json`;
- generated reference documentation under `docs/reference/`.

Workers document this drift and do not regenerate those files manually.
