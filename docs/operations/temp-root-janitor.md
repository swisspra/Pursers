# Reclaiming abandoned seat temp roots

Seat test runs put their `TMPDIR` under an operator-owned cache root. A run that
is killed mid-suite leaves that root behind. One observed abandoned root was
7.7 GB; enough of them filled the disk to 117 MB free on 2026-09-17 and made
`tools/ci_manifest.py run` fail with `OSError(28, 'No space left on device')`
in a way that looked like a test failure.

Two things now guard that:

- `tools/ci_manifest.py` refuses to start when the checkout volume has less than
  10 GB free, and says so instead of failing inside a suite. Override with
  `PURSERS_CI_MIN_FREE_BYTES`.
- `tools/tmp_janitor.py` removes abandoned roots, and only those.

## Running the janitor

It is a dry run unless you pass `--delete`, and it fails closed: a root it
cannot prove is both old and unused is skipped, not removed.

```
python3 tools/tmp_janitor.py --root /PATH/TO/SEAT_CACHE/tmp-a --root /PATH/TO/SEAT_CACHE/tmp-b

# Explicitly discover known Pursers review/gate roots directly under one parent.
python3 tools/tmp_janitor.py --discover-pursers-under /private/tmp
```

| Flag | Meaning |
| --- | --- |
| `--root` | An exact directory to inspect. Repeat it once per managed root. Mutually exclusive with discovery. |
| `--discover-pursers-under` | Explicitly scan the direct children of one absolute, real parent. Only `pursers-review-*`, `pursers-fullgate-*`, and `pursers-packaging-gate.*` basenames are eligible. Unrelated names, nested paths, symlinks, and filesystem roots are excluded. |
| `--older-than-hours` | Minimum age before a root is eligible. Default `1.0`. |
| `--delete` | Actually remove the selected roots. Without it the command only reports. |

Read the dry-run output first, then repeat the same command with `--delete`.
There is no implicit discovery parent. In particular, the board butler reports
storage pressure but does not invoke deletion.

## What makes a root "abandoned"

A root is only eligible when every check passes:

- it is older than `--older-than-hours`;
- its `.pursers-tmp-owner-pid` owner process is gone, or it has no owner file;
- any `.owner.json` owner record is valid and its advisory lock is not held;
- no live process has a file open under it (`lsof`);
- no live process names it in a `TMPDIR`/`TMP`/`TEMP`/`TEMPDIR`/
  `PYTEST_DEBUG_TEMPROOT` environment assignment, and no process has it as its
  working directory.

The last two checks exist because an earlier revision deleted a directory a
running test was still using. Anything the janitor cannot prove is dead is
reported and left alone.

Immediately before deletion the janitor repeats every check, atomically moves
the same inspected inode to a private quarantine name under the same parent,
verifies it again, and only then removes it. A substituted symlink or directory
is restored when the original name is still free, or retained under the reported
quarantine path if another entry now owns the name. Reclaimed-byte totals include
only trees that completed this final removal.
