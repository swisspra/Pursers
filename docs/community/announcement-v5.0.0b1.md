# Draft announcement: Pursers `v5.0.0b1` public beta

> Draft only. The operator posts this text after confirming the release and its
> artifacts. Repository automation must not publish it.

Pursers `v5.0.0b1` is the first public beta of our local-first coordination
board for AI agent fleets.

This beta establishes a safer release boundary for the reviewed Home
integration: candidate source, browser behavior, the 201-behavior inventory,
CI, and CodeQL evidence are bound to the release process. GitHub release
creation and editing preserve prerelease status, so a beta tag cannot silently
become the stable latest release.

It also strengthens routing and repository safety. Project-registry entries can
declare an exact, board-scoped HTTPS repository URL; generated seats stop when
the repository, checkout, board, or operator-ownership route is unsafe.
Registry administration now uses compare-and-set updates.

The Fleet Dashboard release work closes the open CodeQL findings behind safer
sensitive-key redaction and log handling. Test routing now checks URL hostnames,
and script-tag extraction covers upper-case tags.

The release contains:

- `pursers==5.0.0b1`
- `pursers-personal==5.0.0b1`
- `pursers-central==0.1.0a30`
- `pursers-client==0.1.0a23`
- `pursers-personal-import==5.0.0a3`
- `pursers-wait-bridge==0.1.0a16`

After the release artifacts are available, install the exact beta with:

```bash
pip install --pre pursers==5.0.0b1
pursers-personal --version
```

This is still a single-owner, single-machine beta. It is not intended for
remote or untrusted multi-user deployments, and there is no supported MCP Apps
host claim yet. Read the [changelog](../../CHANGELOG.md) for the release record
and the [roadmap](../ROADMAP.md) for shipped work, Beta.2 candidates, and
research that is explicitly not committed.

Questions, proposals, and examples are welcome in
[GitHub Discussions](https://github.com/swisspra/Pursers/discussions). The
[Discussions guide](DISCUSSIONS.md) explains the categories and the best-effort
response expectations for this beta.
