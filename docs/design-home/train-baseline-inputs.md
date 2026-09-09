# Reviewed train baseline inputs

This cumulative integration starts from frozen `origin/main`
`b06ce6627edb62fc588eee541fa568445b709054`. Each input below was independently
approved and closed, and its recorded SHA matched the exact remote branch tip
before integration.

| Ticket | Remote branch | Approved tip | Imported scope |
| --- | --- | --- | --- |
| `TK-c7eace5014e3` | `codex/TK-c7eace5014e3` | `ad042cbdae529a52868bbbc22d35229fe62708e4` | Fleet routing, extension, scanner, and tests; 18 files |
| `TK-034e85d4ae4d` | `codex/TK-034e85d4ae4d-resubmit-2` | `936e681ad0b4f1d32db323558fce9c65b628abf5` | Complete Canvas/Superdesign handoff; 26 paths |
| `TK-4f1f01ba50eb` | `codex/TK-4f1f01ba50eb-resubmit-8` | `9d1febab90034b3cf8b45a25640a85ef1166b9d3` | Verifier-owned external-observer acceptance harness; 5 files |
| `TK-80e362bdcd96` | `codex/TK-80e362bdcd96-resubmit-2` | `165dfd9f83aa6e783164f47125ae3002c6a7b804` | Submission preflight and evidence preservation; 3 files |
| `TK-14449dad2afb` | `codex/TK-14449dad2afb-resubmit-3` | `60a048f9df0b8b91ea9b967e3670796d87a3e469` | Both cumulative commits: argument-file rejection and clean reviewer suite environment; 3 files |
| `TK-fce1144ef977` | `codex/TK-fce1144ef977-resubmit-7` | `bf50aec4cb23ecdb3c9447eab711b422e983fe9d` | Reviewed quiet-timeout cursor diagnosis; 1 file |

The union contains 53 input paths. Fifty non-overlapping paths are byte-exact
with their approved tips. The only overlap is:

- `tools/seat-kit/README.md`
- `tools/seat-kit/seat_new.py`
- `tools/seat-kit/tests/test_seat_new.py`

The `seat_new.py` conflict was resolved by retaining the submission source
selection, exact remote-tip preflight, and canonical immutable evidence from
`TK-80e362bdcd96`, while retaining the repository-aware safe suite parser,
argument-file rejection, path restrictions, and isolated replay environment
from both commits of `TK-14449dad2afb`. The README and tests retain both input
contracts. The submission-preflight regression now also proves that a safe,
relative `PYTHONPATH` suite command survives canonical note truncation and is
replayed with user-site and plugin autoload disabled.

The rejected integration exposed one additional verifier-side write boundary:
pytest output paths were checked only when the final file already existed. The
rework allow-lists supported pytest options and resolves every positional or
output path even when its leaf is absent. Existing symlink ancestors must still
resolve inside the detached worktree. A regression covers
`--junitxml=link/report.xml` through an outward symlink and proves the external
report is never created; contained `--junitxml` and `--basetemp` remain usable.

The acceptance harness originally read an older `contributes.webui.apiRoutes`
shape. The approved extension uses AionCore's `contributes.webui[].routes`
shape, so discovery now reads that exact schema and reports all declared door
and Team routes. The semantic gate remains conservative: partial Team and seat
route families do not satisfy complete lifecycle capability, and source route
declarations remain distinct from installed-host execution evidence.

`train-baseline-files.sha256` hashes all imported paths plus this provenance
document. It deliberately excludes only itself; its own hash is reported in
the immutable ticket submission.

This baseline does not import unapproved runtime, observer, Personal, or O1
revisions and does not claim real-host or whole-train acceptance.
