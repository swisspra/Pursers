# Security Alert Verification on Foundation Main

## 1. Verification Metadata and Context

- **Verification Timestamp**: 2026-09-08T12:00:00Z
- **Target Repository**: `swisspra/Pursers`
- **Default Branch**: `main`
- **Exact Default-Branch SHA**: `133d5f5acd3bd1dca8121b9171879516bcfc3474`
- **Commit Message**: `chore: integrate reviewed next-train foundation`
- **Integration Base**: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (`release: consolidate 5.0.0a25 train`)
- **Original Remediation Tickets Inspected**:
  - `TK-01f2f9c530ba`: Dependabot alerts in `tools/dashboard-ui` (`package-lock.json`)
  - `TK-823656fc373f`: CodeQL code-scanning alerts in `tools/fleet-dashboard`
- **Verification Purpose**: Independent, read-only audit of live GitHub security alerts (Dependabot, CodeQL code-scanning, secret-scanning) and workflow execution post-integration of foundation commit `133d5f5acd3bd1dca8121b9171879516bcfc3474`.

---

## 2. Reproducible Read-Only Verification Commands

All checks were executed via read-only GitHub API and Git inspections without mutating code, triggering workflows, changing alert severities, or dismissing alerts.

```bash
# 1. Verify default-branch tip SHA and commit metadata
git rev-parse origin/main
# Output: 133d5f5acd3bd1dca8121b9171879516bcfc3474

# 2. Inspect latest workflow runs on main
gh api repos/swisspra/Pursers/actions/runs?branch=main \
  | jq '[.workflow_runs[] | {id, name, head_sha, event, status, conclusion, created_at, updated_at}]'

# 3. Query all live Dependabot alerts
gh api repos/swisspra/Pursers/dependabot/alerts --paginate \
  | jq '[.[] | {number, state, dependency: .dependency.package.name, advisory: .security_advisory.ghsa_id, cve: .security_advisory.cve_id, severity: .security_advisory.severity, created_at, fixed_at}]'

# 4. Query all live CodeQL code-scanning alerts
gh api repos/swisspra/Pursers/code-scanning/alerts --paginate \
  | jq '[.[] | {number, state, rule: .rule.id, severity: .rule.security_severity_level, path: .most_recent_instance.location.path, line: .most_recent_instance.location.start_line, created_at, fixed_at}]'

# 5. Query specific details of unresolved CodeQL Alert #10
gh api repos/swisspra/Pursers/code-scanning/alerts/10 \
  | jq '{number, state, rule: .rule.id, message: .most_recent_instance.message.text, location: .most_recent_instance.location, commit: .most_recent_instance.commit_sha}'

# 6. Query live secret-scanning alerts (safe metadata only, zero payload)
gh api repos/swisspra/Pursers/secret-scanning/alerts \
  | jq '[.[] | {number, state, secret_type, resolution, resolved_at}]'

# 7. Local verification of repository leak status and git diff
python3 tools/leak_scan.py
git diff --check
```

---

## 3. Workflow & Scan Runs on Default Branch (`main`)

### 3.1. Workflow Run Execution Table

| Run ID | Workflow Name | Head SHA | Status | Conclusion | Created At (UTC) | Updated At (UTC) |
|---|---|---|---|---|---|---|
| `34217271786` | `Push on main` (CodeQL) | `133d5f5` | `completed` | `success` | 2026-09-08T10:47:11Z | 2026-09-08T10:49:05Z |
| `34217273113` | `ci` | `133d5f5` | `completed` | `success` | 2026-09-08T10:47:11Z | 2026-09-08T10:53:39Z |
| `34217385935` | `npm_and_yarn in /tools/dashboard-ui for qs` | `133d5f5` | `completed` | `success` | 2026-09-08T10:48:29Z | 2026-09-08T10:49:04Z |
| `34217386885` | `npm_and_yarn in /tools/dashboard-ui for fast-uri` | `133d5f5` | `completed` | `success` | 2026-09-08T10:48:30Z | 2026-09-08T10:49:03Z |

### 3.2. CodeQL Scan Analysis Details

Workflow run `34217271786` (`Push on main`) executed three CodeQL analysis matrix jobs against commit `133d5f5acd3bd1dca8121b9171879516bcfc3474`:
- Job `102031886406` (`Analyze (python)`): Started `10:47:16Z`, Completed `10:49:04Z` (Result: `success`)
- Job `102031886592` (`Analyze (actions)`): Started `10:47:16Z`, Completed `10:48:02Z` (Result: `success`)
- Job `102031886695` (`Analyze (javascript-typescript)`): Started `10:47:15Z`, Completed `10:48:32Z` (Result: `success`)

### 3.3. Scan Lag and Timing Analysis

- **Dependabot Scan Lag**: Foundation commit `133d5f5` landed at `10:47:11Z`. GitHub Dependabot processed the updated `package-lock.json` and marked all 6 alerts `fixed` at `10:47:13Z` (lag: ~2 seconds).
- **CodeQL Scan Lag**: CodeQL analysis started at `10:47:15Z`. GitHub updated alert statuses at `10:48:38Z` (lag: ~87 seconds). Full analysis completed at `10:49:05Z`.
- **Scan Currency**: 100% current. All workflows on default branch `main` have completed. Zero jobs or scans are queued or in progress.

---

## 4. Dependabot Alert Disposition (Ticket `TK-01f2f9c530ba`)

All six baseline Dependabot alerts were addressed in ticket `TK-01f2f9c530ba` via commit `e7d4e8e6c60f48df8b8ba317b77211c87e33ec99`, integrated into `main` at `133d5f5acd3bd1dca8121b9171879516bcfc3474`.

### 4.1. Dependabot Alerts Inventory & Live State

| Alert # | Package | Advisory / CVE | Severity | Vulnerable Version | Patched Version | Fixed At (UTC) | Live State | Fixed Revision Attribution |
|---|---|---|---|---|---|---|---|---|
| **#1** | `qs` | GHSA-4mjr-xmp4-gh2g (CVE-2026-82417) | Medium | `6.15.3` | `6.16.0` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |
| **#2** | `qs` | GHSA-x5fp-wj9c-mxmx (CVE-2026-82562) | Medium | `6.15.3` | `6.16.0` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |
| **#3** | `fast-uri` | GHSA-jqff-g426-hqxp (CVE-2026-76172) | High | `3.1.5` | `3.1.7` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |
| **#4** | `fast-uri` | GHSA-fph4-wmhf-6fwf (CVE-2026-75899) | High | `3.1.5` | `3.1.7` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |
| **#5** | `fast-uri` | GHSA-f65p-4m7j-42xc (CVE-2026-75975) | High | `3.1.5` | `3.1.7` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |
| **#6** | `fast-uri` | GHSA-5jgf-p345-68v8 (CVE-2026-75931) | High | `3.1.5` | `3.1.7` | 2026-09-08T10:47:13Z | `fixed` | `e7d4e8e` in `133d5f5` |

### 4.2. Code Merge vs. Rescan Closure

- **Code Fix Merged**: `tools/dashboard-ui/package-lock.json` was updated to specify `qs@6.16.0` and `fast-uri@3.1.7`.
- **GitHub Rescan Confirmation**: GitHub Dependabot scanner ran automatically upon push to `main` and verified that the manifest contains safe versions outside all vulnerability windows.
- **Actionable Findings**: Zero. All 6 alerts are closed and confirmed resolved.

---

## 5. Code Scanning Alert Disposition (Ticket `TK-823656fc373f`)

Baseline audit at claim of `TK-823656fc373f` identified 10 CodeQL alerts. Commit `a93dd1ff97cfe62e25dc9fbb80baf2d176d91276` was integrated into `main` at `133d5f5acd3bd1dca8121b9171879516bcfc3474`.

### 5.1. Code Scanning Alerts Inventory & Live State

| Alert # | Rule ID | Path and Line | Severity | Live State | Fixed At (UTC) | Resolution Status & Attribution |
|---|---|---|---|---|---|---|
| **#4** | `py/clear-text-logging-sensitive-data` | `tools/fleet-dashboard/release_ops.py:1265` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: logging rewritten to static message; confirmed by CodeQL in `133d5f5`. |
| **#5** | `py/incomplete-url-substring-sanitization` | `tools/fleet-dashboard/tests/test_release_ops.py:155` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: hostname comparison using `urlsplit`; confirmed by CodeQL in `133d5f5`. |
| **#6** | `py/bad-tag-filter` | `tools/fleet-dashboard/tests/test_human_dashboard.py:261` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: regex updated with `re.IGNORECASE`; confirmed by CodeQL in `133d5f5`. |
| **#7** | `py/bad-tag-filter` | `tools/fleet-dashboard/tests/test_fleet_dashboard.py:1564` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: regex updated with `re.IGNORECASE`; confirmed by CodeQL in `133d5f5`. |
| **#8** | `py/bad-tag-filter` | `tools/fleet-dashboard/tests/test_fleet_dashboard.py:4791` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: regex updated with `re.IGNORECASE`; confirmed by CodeQL in `133d5f5`. |
| **#9** | `py/bad-tag-filter` | `tools/fleet-dashboard/tests/test_fleet_dashboard.py:4879` | High | `fixed` | 2026-09-08T10:48:38Z | Fixed: regex updated with `re.IGNORECASE`; confirmed by CodeQL in `133d5f5`. |
| **#1** | `py/clear-text-storage-sensitive-data` | `tools/worker-runtime/tests/test_pursers_worker.py:1472` | High | `open` | — | Synthetic test fixture write. Recommended disposition: "Used in tests". |
| **#2** | `py/clear-text-storage-sensitive-data` | `tools/fleet-dashboard/tests/test_seat_config.py:1435` | High | `open` | — | Synthetic test fixture write. Recommended disposition: "Used in tests". |
| **#3** | `py/clear-text-storage-sensitive-data` | `tools/fleet-dashboard/tests/test_seat_config.py:1451` | High | `open` | — | Synthetic test fixture write. Recommended disposition: "Used in tests". |
| **#10** | `py/polynomial-redos` | `tools/fleet-dashboard/fleet_dashboard.py:4684` | High | `open` | — | **Actionable finding**: CodeQL re-analysis flagged regex at line 4684 on `133d5f5`. |

### 5.2. Detailed Analysis of Synthetic Test Fixture Alerts (#1, #2, #3)

- **Alert #1** (`tools/worker-runtime/tests/test_pursers_worker.py:1472`): Tests that the seat worker does not leak token storage in reports or logs. The token is a hardcoded synthetic string written to an ephemeral pytest temporary directory (`tmp_path`).
- **Alert #2** (`tools/fleet-dashboard/tests/test_seat_config.py:1435`): Tests seat configuration parsing and validates that sensitive token keys are not output in plain text.
- **Alert #3** (`tools/fleet-dashboard/tests/test_seat_config.py:1451`): Tests redacting seat configuration values in diagnostics.
- **Evidence-Backed Recommended Disposition**: "Used in tests". Each instance is strictly confined to test fixture setup, uses non-production synthetic values, and runs in isolated temporary test files.
- **Policy Enforcement**: Worker seats do NOT have dismissal authorization. Alerts remain `open` on GitHub and require coordinator/operator disposition via the GitHub UI.

### 5.3. Analysis of Concrete Unresolved Finding: CodeQL Alert #10

- **Alert Details**:
  - Rule: `py/polynomial-redos`
  - Location: `tools/fleet-dashboard/fleet_dashboard.py:4684`
  - CodeQL Analysis Message: `"This regular expression that depends on a user-provided value may run slow on strings starting with ':' and with many repetitions of ' '."`
- **Root Cause**:
  In `tools/fleet-dashboard/fleet_dashboard.py`, the remediation commit introduced:
  ```python
  sensitive = re.compile(r"(?im)^([^:=\n]*)([:=]\s*)(.*)$")
  # ...
  value = sensitive.sub(redact, value)  # line 4684
  ```
  The regex subpattern `([:=]\s*)(.*)$` contains ambiguity between `\s*` and `.*`, allowing polynomial backtracking when matched against inputs beginning with `:` or `=` and followed by repeated whitespace characters.
- **Status on Default Branch**:
  Although TK-823656fc373f ran micro-reproduction tests, CodeQL static analysis in workflow run `34217271786` on commit `133d5f5acd3bd1dca8121b9171879516bcfc3474` did NOT mark Alert #10 as fixed. The alert remains `open`.
- **Action Required**:
  Reported to coordinator and documented as an unresolved finding. A follow-up fix should eliminate backtracking ambiguity (e.g. using line splitting or bounded non-overlapping regex patterns).

---

## 6. Secret Scanning Status

- **Live Alert Count**: 1 total alert.
- **Safe Identifier / Category**: Google API Key (`google_api_key`), Alert #1.
- **Status**: `resolved` (Resolution: `used_in_tests`).
- **Resolved By**: `swisspra` at `2026-09-08T04:19:17Z`.
- **Detection Location**: Commit `be830ee17f570e756013930448f984c07311dd4c` in `tools/tests/test_leak_scan.py:80`.
- **Payload Sanitization**: The synthetic test pattern was removed from repository HEAD; zero secret payload strings exist on default branch `main`. No open secret scanning alerts exist.

---

## 7. Safe Result Summary & Status Matrix

| Category | Total Baseline Alerts | Confirmed Closed (`fixed` / `resolved`) | Open Alerts (Recommended Disposition) | Open Alerts (Actionable) |
|---|---|---|---|---|
| **Dependabot** | 6 | 6 (Alerts #1, #2, #3, #4, #5, #6) | 0 | 0 |
| **Code Scanning (CodeQL)** | 10 | 6 (Alerts #4, #5, #6, #7, #8, #9) | 3 (Alerts #1, #2, #3: "Used in tests") | 1 (Alert #10: ReDoS) |
| **Secret Scanning** | 1 | 1 (Alert #1: "Used in tests") | 0 | 0 |
| **Total** | 17 | 13 | 3 | 1 |

---

## 8. Exact Limitations

1. **Read-Only Scope**: This verification is strictly read-only. No source code or production logic was modified.
2. **Scanner Dismissal Authority**: Seats lack authorization to dismiss alerts on GitHub. Recommendations for Alerts #1, #2, and #3 are submitted for coordinator/operator action.
3. **Scanner Independence**: Local CodeQL CLI is unavailable on this host; scanner results are derived directly from the GitHub CodeQL Action run on `133d5f5acd3bd1dca8121b9171879516bcfc3474`.
4. **Actionable ReDoS Alert #10**: Remains open on `main` and requires follow-up code remediation.
5. **Release Train Gate**: Final release still requires revalidation against the final train SHA.
