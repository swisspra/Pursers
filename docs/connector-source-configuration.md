# Connector and source configuration contract

Board Butler exposes a versioned, read-only configuration contract for Fleet
Dashboard adapters. The contract validates configuration with the same parsers
used by the resident, normalizes legacy spellings, applies defaults, redacts
credentials and private paths, and compares desired with effective values. It
does not connect to MCP servers, resolve credential files, call tools, onboard a
project, enable a connector, or perform writeback.

The public contract identifier is
`pursers.board_butler.configuration`, version `1`. There are two kinds:

- `connector_source` for the private `--connector-config` file.
- `source_onboarding` for the private `--intake-onboarding-config` file.

Both return `desired`, `effective`, `capabilities`, and `unsupported`. Exported
documents replace secret-file locations and other private paths with
`[redacted]`. Comparisons use the unredacted values only to determine equality;
changed sensitive values are reported as `[redacted]` on both sides.

## Public Python API

Load and inspect the connector/source file through the resident module:

```python
contract = inspect_connector_source_configuration(
    config_path,
    default_board_id="pursers",
    default_project_id="registry",
    default_actor_id="board-butler",
)
public_document = contract.export()
desired_effective_diff = contract.compare()
```

Inspect onboarding policy without touching the filesystem:

```python
contract = inspect_source_onboarding_configuration(document)
```

Or read an owned private file with an optional directory boundary:

```python
contract = load_source_onboarding_configuration(path, root=config_root)
```

The file reader requires an absolute, owner-matched, non-symlink, mode-0600
regular file of at most 1 MiB. When `root` is supplied, both the lexical and
resolved file paths must remain below that directory. It never opens any
credential reference inside the document.

## Connector and source fields

`schema_version` must be `1`. Unknown fields in connector declarations, source
declarations, routing, grouping, observation, preflight, or writeback are
rejected. The effective view always uses the canonical `connectors` array and
expands legacy `tools_read_only`, `tools_risky_mutating`, `tools_denied`,
singular declaration forms, and omitted defaults.

Supported connector transports are `stdio` and `streamable_http`; the supported
MCP revision is `2026-07-28`. Tools must be classified as `read_only` or
`mutating`, with replay `never` or `safe_with_stable_call_id`. A source list
tool and observation tool must be declared read-only. A writeback tool must be
both mutating and listed in `risky_tools`.

Source modes are `ask` and `auto`; free-text content is restricted to `ask`.
`routing` selects exactly one of an explicit `project_map` or
`project_hint_is_registry_key: true`. This preserves source membership and
deduplication: the configured `source_id`, `external_id`, `revision`, and
project route remain the durable identity inputs. Changing a source ID requires
an explicit private-index migration.

Grouping currently supports only `kind: sonar`, with `max_in_flight` and
`max_admitted_groups` from 1 through 100. `max_pages` is from 1 through 100 and
requires `page_arg` when greater than one. Unknown grouping or adapter kinds are
reported as unsupported rather than inferred.

Writeback is separate from intake configuration and external mutation. The
contract validates `on` (`approved` or `closed`), the declared risky tool,
bounded argument templates, supported placeholders, and optional read-only
preflight mapping. Contract inspection never grants the runtime policy gate and
never calls that tool. It therefore cannot automatically enable a connector or
broaden authorization.

## Onboarding fields and repository mapping

The onboarding document contains only `sources`, with at most 100 entries. Each
source requires `domain`, absolute `projects_root`, `auto_onboard`, one per-cycle
cap spelling, and one repository-map spelling. Legacy
`max_new_projects_per_cycle` and `repository_map` remain accepted and normalize
to `per_cycle_cap` and `repositories`.

Repository URLs must be credential-free HTTPS URLs. Each mapping has an
`integration_ref`, defaulting to `main`; the existing lifecycle parser validates
the repository/branch pair. Fuzzy repository selection is not supported.
Discovery supports only `sonar_ado`, with `refresh_seconds` from 300 through
86,400. Retry limits are 1 through 20 and retry backoff is 1 through 86,400
seconds.

Delivery workflow and policy fields continue to use the shared project-registry
parsers. Saving configuration does not activate batch or branch-only delivery.
Activation remains an explicit operator-owned setting, and unsupported runtime
features appear in the contract instead of being represented as invented
adapters.

## Migration and rollback

Existing version-1 files remain valid. Read the effective view, replace legacy
aliases with their canonical fields, preserve source IDs, then compare again.
A stale schema version or unknown field fails closed before any connector or
onboarding action.

Rollback by restoring the previous mode-0600 configuration file and restarting
the resident. Do not edit the private intake index to roll back configuration.
Disabling or removing a source with durable index entries requires the existing
explicit source-ID migration; the contract will not discard membership or
writeback state.
