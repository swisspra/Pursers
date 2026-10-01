# Host paths and verification

Use one selected host. Installation never configures MCP, creates credentials,
or installs the bundle into every host.

## Zed Agent

Zed discovers project skills from `<worktree>/.agents/skills/` and user skills
from `~/.agents/skills/`. Prefer a project install. In Zed, open **AI > Skills**
or run `agent: manage skills`, select the Project scope, and confirm the four
skills. A newly cloned worktree must be trusted before project skills load.

These skills apply to the native Zed Agent. External ACP threads use the
external agent's own skill discovery. The Pursers MCP extension also belongs in
a native Zed Agent thread; follow the canonical Zed guide for that distinction.

Official host reference: <https://zed.dev/docs/ai/skills>

## Codex CLI

Codex uses `<project>/.codex/skills/` for project scope and
`~/.codex/skills/` for user scope. Prefer a project install and start Codex in
that project. Verify MCP separately with `codex mcp list` and `/mcp`; skill
presence does not prove that Central authentication works.

Official format reference:
<https://developers.openai.com/api/docs/guides/tools-skills>

## goose CLI or Desktop

Current goose releases discover shared project skills from
`<project>/.agents/skills/` and user skills from `~/.agents/skills/` when the
Skills extension is enabled. Prefer a project install. Verify that the Skills
extension is enabled and ask goose to list or explicitly load one installed
skill before relying on automatic selection.

The MCP connection and the skill catalog are independent checks. Follow the
Pursers client guide for file-backed authentication.

Official host references:

- <https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/using-extensions.md>
- <https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/context-engineering/using-skills.md>

## Unsupported host or version

Do not guess a directory or copy instructions globally. Keep the bundle in its
source checkout, use the canonical guides and installed MCP tool descriptions,
and report the host name, version, and missing capability for later validation.
