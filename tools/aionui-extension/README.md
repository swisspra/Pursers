# Pursers for AionUi

This extension adds Pursers Worker and Pursers Reviewer presets and a Pursers
settings tab for joining a seat with one coordinator-issued door.

## Build and install

From the repository root:

```sh
python tools/aionui-extension/build.py
```

Install `dist/pursers-aionui-0.1.0.zip` through AionUi's extension installer.
Do not unzip it into an existing AionUi data directory by hand.

The Join tab expects `pursers-wait-bridge` on AionUi's executable path. Install
it with `uv tool install pursers-wait-bridge` or
`pipx install pursers-wait-bridge` if the tab reports that it is missing.

## Join a seat

1. Open Settings, then Pursers.
2. Paste the door supplied by your coordinator and select Join.
3. Confirm the redacted board, role, seat name, push mode, key ID, and expiry.
4. Start a new conversation and pick the matching Worker or Reviewer preset.

The Join route passes the door directly to `pursers-wait-bridge join`, which
owns the private mode-0600 credential store. The extension does not log or
persist the door. It then registers an environment-free stdio bridge through
AionUi's local `POST /api/mcp/servers/import` endpoint.

## AionUi 2.2.1 limitations

- AionUi lists extension-declared MCP servers but does not inject them into
  Codex or Claude conversations. This extension deliberately has no
  `contributes.mcpServers` block and uses the authenticated REST import path.
- Extension assistant presets are contributed, but AionUi 2.2.1 may not expose
  them in the conversation preset picker. If a preset is not selectable, create
  the conversation explicitly and apply the matching context file.
- AionUi does not render Pursers MCP elicitation forms. Use the dashboard or the
  coordinator-provided fallback for human-input requests.
- A full GUI verification is an operator step and is not performed by the build
  or test suite.
