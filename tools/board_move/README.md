# Offline board move

Move one board between stopped Central instances without using HTTP. Both
commands are dry-run by default and refuse a data directory held by a live
server.

```bash
python tools/board_move/board_move.py export \
  --data-dir /path/to/source-data \
  --board-id example-board \
  --archive /safe/path/example-board.json

python tools/board_move/board_move.py export \
  --data-dir /path/to/source-data \
  --board-id example-board \
  --archive /safe/path/example-board.json \
  --commit

python tools/board_move/board_move.py import \
  --data-dir /path/to/target-data \
  --archive /safe/path/example-board.json \
  --principal-map PR-old=PR-new \
  --require-full-map

python tools/board_move/board_move.py import \
  --data-dir /path/to/target-data \
  --archive /safe/path/example-board.json \
  --principal-map PR-old=PR-new \
  --require-full-map \
  --commit
```

The archive contains the board document (including memories), journal, and a
deterministic manifest with counts and SHA-256 hashes. Import preserves board,
ticket, memory, event IDs, and journal sequence numbers. It rotates the board
generation so pre-move clients must rejoin.

Dry-run reports unmapped principals and strict-profile scrub violations.
`--require-full-map` refuses incomplete maps. A committed import also refuses
scrub violations rather than mutating archived content. Existing target boards
and existing archive paths are never overwritten.
