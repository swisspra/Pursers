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
```

| Flag | Meaning |
| --- | --- |
| `--root` | An exact directory to inspect. Repeat it once per managed root. Required; there is no glob and no implicit default, so nothing is ever deleted that you did not name. |
| `--older-than-hours` | Minimum age before a root is eligible. Default `1.0`. |
| `--delete` | Actually remove the selected roots. Without it the command only reports. |

Read the dry-run output first, then repeat the same command with `--delete`.

## What makes a root "abandoned"

A root is only eligible when every check passes:

- it is older than `--older-than-hours`;
- its `.pursers-tmp-owner-pid` owner process is gone, or it has no owner file;
- no live process has a file open under it (`lsof`);
- no live process names it in a `TMPDIR`/`TMP`/`TEMP`/`TEMPDIR`/
  `PYTEST_DEBUG_TEMPROOT` environment assignment, and no process has it as its
  working directory.

The last two checks exist because an earlier revision deleted a directory a
running test was still using. Anything the janitor cannot prove is dead is
reported and left alone.
