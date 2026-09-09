# Next-train CodeQL receipt

This receipt audits the assembled source at
`ad55c14a38e28fe49253d974d96d1771141a7607`, based on frozen `main` commit
`b06ce6627edb62fc588eee541fa568445b709054`. It documents evidence only. It does
not dismiss alerts, change security behavior, or claim browser acceptance.

## Exact scanner evidence

GitHub CodeQL run
[`34377422518`](https://github.com/swisspra/Pursers/actions/runs/34377422518)
completed successfully for `refs/pull/4/head` at exact commit
`ad55c14a38e28fe49253d974d96d1771141a7607`. Its three uploaded analyses all
reported zero results and an empty warning:

| Language | Analysis ID | Results | Rules |
|---|---:|---:|---:|
| Actions | `1749312796` | `0` | `17` |
| JavaScript/TypeScript | `1749315820` | `0` | `87` |
| Python | `1749317733` | `0` | `43` |

CI run
[`34377425751`](https://github.com/swisspra/Pursers/actions/runs/34377425751)
also completed successfully at the same exact commit. These run and analysis IDs
refer only to `ad55c14a38e28fe49253d974d96d1771141a7607`; they are not attributed to
this receipt commit.

## Four alert dispositions

The GitHub API still reports all four alerts as open, undismissed, and attached
only to `refs/heads/main` at
`b06ce6627edb62fc588eee541fa568445b709054`.

| Alert | Rule | Main location | Disposition in assembled source |
|---:|---|---|---|
| `#1` | `py/clear-text-storage-sensitive-data` | `tools/worker-runtime/tests/test_pursers_worker.py:1472` | Test-only synthetic fixture. `SYNTHETIC_SEAT_TOKEN_MUST_NOT_ESCAPE` is written inside a temporary directory with mode `0600`; the test proves shell output and logs do not expose it. No production secret storage occurs here. |
| `#2` | `py/clear-text-storage-sensitive-data` | `tools/fleet-dashboard/tests/test_seat_config.py:1435` | Test-only synthetic fixture. `SUPER_SECRET_TOKEN_NOT_JWT` is written under `pytest`'s `tmp_path` to exercise invalid-JWT handling and is asserted absent from the doctor report. |
| `#3` | `py/clear-text-storage-sensitive-data` | `tools/fleet-dashboard/tests/test_seat_config.py:1451` | Test-only synthetic fixture. `SECRET_PAYLOAD_CONTENT` is embedded in a synthetic JWT under `pytest`'s `tmp_path` and is asserted absent from the doctor report. |
| `#10` | `py/polynomial-redos` | `tools/fleet-dashboard/fleet_dashboard.py:4686` | Real finding already remediated. The assembled source replaces the overlapping regex with bounded `splitlines`/`find` scanning in `SeatConfigManager._redact_sensitive_assignments`. |

For alert `#1`, the candidate and frozen-main test blobs are identical Git blob
`dd6b2952ce29e17814d7b0ca605475dc1545979b`. For alerts `#2` and `#3`, the
candidate and frozen-main test blobs are identical Git blob
`d4c501c12fdcc152958d8c626200c28b28f5f1ec`. The literals are fixed synthetic
test values, not credentials.

Alert `#10` was independently approved in `TK-3887c4b0a7c9` at exact commit
`770f27fcabd4b3333faec692668d1e88661f2afe`. The ticket's accepted evidence
records SHA-256
`00c86397b81f0fe24b3524eaf9639c3b72b48ff8404a6d196c9901e1012b38f1` for
the remediated function and SHA-256
`856252584c1814eee4078b0a248aaf1629048b3cee491eaa699ef548bac90d55` for
the million-character and line-ending regression. The accepted and assembled
source fragments are byte-identical. An independent AST-node extraction also
matches between the two commits: SHA-256
`e0d36992ceeeee42127f157c145033d905fd3b64ad844c346555937ce08699cf` for
the function body and SHA-256
`d207d9d3f03fbfbdd22e782ad05a6a1b20d8f0f0f78a4eb58f1630ff54ab1da3` for
the regression test.

## Focused checks

The affected checks for this receipt are:

```text
python3 -m pytest -q tools/worker-runtime/tests/test_pursers_worker.py::test_shell_cannot_return_or_log_seat_token
1 passed

PYTHONPATH=tools/fleet-dashboard python3 -m pytest -q tools/fleet-dashboard/tests/test_seat_config.py::test_doctor_token_file_validation_and_redaction
1 passed

PYTHONPATH=tools/fleet-dashboard python3 -m pytest -q tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_clean_text_redaction_is_linear_time_and_behavior_preserved
1 passed

python3 tools/leak_scan.py docs/security/next-train-codeql-receipt.md tools/fleet-dashboard/ tools/worker-runtime/
leak_scan: clean (0 violations)
```

## Disclosed platform failure and limits

The separate GitHub Advanced Security agentic-review run
[`34377426871`](https://github.com/swisspra/Pursers/actions/runs/34377426871)
failed before producing a review. Its service selected `gpt-5.3-codex` and
returned HTTP `400`: `The requested model is not supported.` This is a platform
failure, not a CodeQL failure or a successful security review.

Alerts `#1`, `#2`, `#3`, and `#10` remain visibly open on frozen `main`. Final
merge and exact-main CodeQL analysis, or an authorized maintainer disposition
for the three test-only alerts, remain outside this receipt. No alert was
dismissed, suppressed, or waived. No version, tag, release, publication,
production change, or browser acceptance is represented here.
