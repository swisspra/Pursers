---
name: pursers-install
description: Plan and create a new private local Pursers installation from the pinned released package. Use for a fresh installation; use pursers-start to connect an existing installation or verify identity and authorization.
---

# Install Pursers

Use the bundled `scripts/bootstrap.py` helper for a fresh local installation. It does not connect to a board, start Central, configure an IDE, or grant authority. A completed installation is not ready for work until an authorized connection and identity are verified with `pursers-start`.

Choose one explicit private directory that does not already exist. Keep credentials and SQLite data there; do not place it in a shared checkout or a host configuration directory.

```sh
python scripts/bootstrap.py plan --root /PATH/TO/private/pursers
python scripts/bootstrap.py apply --root /PATH/TO/private/pursers --apply
python scripts/bootstrap.py doctor --root /PATH/TO/private/pursers
```

Review the JSON plan before applying it. `plan` is read-only. `apply` requires the explicit `--apply` flag and creates a private virtual environment, installs `pursers==5.2.1`, and runs the released `pursers-central init` command for a fresh Central instance. The helper records progress in a private receipt so an owned installation can resume safely. It preserves existing, foreign, and symlinked roots; do not delete or replace a blocker to make the plan pass.

`doctor` reports package installation and Central initialization separately; runtime state is `unknown` because it does not probe or start a process. A successful installation reports `ready_for_work: false`: service activation, host/MCP configuration, Fleet and autonomous harness activation, board authorization, and connection setup remain separate work. Do not describe the unpackaged Fleet or harness as installed.

For an existing Pursers installation, an existing endpoint, or the first authorized board connection, use [`pursers-start`](../pursers-start/SKILL.md). Never print, copy, or log generated credential contents.
