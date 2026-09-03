#!/usr/bin/env python3
"""Generate a self-contained Pursers worker or reviewer seat."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BOARD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _repo_leaf(repo: str) -> str:
    leaf = repo.rstrip("/").rsplit("/", 1)[-1]
    if leaf.endswith(".git"):
        leaf = leaf[:-4]
    if not leaf or leaf in {".", ".."}:
        raise ValueError("--repo must have a usable repository name")
    return leaf


_BOARD_PYTHON = r'''#!/usr/bin/env python3
"""Generated Pursers seat CLI. Do not put credentials in this file."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROLE = '{role}'
REPO_LEAF = {repo_leaf}


def _load_client() -> type[Any]:
    seat_root = Path(__file__).resolve().parents[1]
    if REPO_LEAF:
        source = seat_root / REPO_LEAF / "packages" / "client" / "src"
        if source.is_dir():
            sys.path.insert(0, str(source))
    try:
        from pursers_client import BoardClient
    except ImportError as exc:
        raise RuntimeError(
            "pursers_client is unavailable; generate with --repo or install pursers-client"
        ) from exc
    return BoardClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="board.sh")
    commands = parser.add_subparsers(dest="command", required=True)
    if ROLE == "worker":
        commands.add_parser("list", help="list open tickets and this seat's claim")
        get = commands.add_parser("get")
        get.add_argument("ticket_id")
        claim = commands.add_parser("claim")
        claim.add_argument("ticket_id")
        renew = commands.add_parser("renew")
        renew.add_argument("ticket_id")
        submit = commands.add_parser("submit")
        submit.add_argument("ticket_id")
        submit.add_argument("summary")
        submit.add_argument("notes")
        submit.add_argument("files_csv")
        wait = commands.add_parser("wait", help="block until work arrives (subscriptions/listen)")
        wait.add_argument("--since", type=int, default=0, help="journal cursor to start from")
        wait.add_argument("--timeout", type=int, default=270, help="max wait seconds")
        wait.add_argument("--poll", action="store_true", default=False,
                          help="enable poll fallback (explicit opt-in, not default)")
    else:
        commands.add_parser("list", help="list submitted tickets")
        commands.add_parser("list-all", help="list all non-closed tickets")
        get = commands.add_parser("get")
        get.add_argument("ticket_id")
        approve = commands.add_parser("approve")
        approve.add_argument("ticket_id")
        approve.add_argument("notes")
        reject = commands.add_parser("reject")
        reject.add_argument("ticket_id")
        reject.add_argument("notes")
        reject.add_argument("fix")
        wait = commands.add_parser("wait", help="block until submitted tickets arrive")
        wait.add_argument("--since", type=int, default=0, help="journal cursor")
        wait.add_argument("--timeout", type=int, default=270, help="max wait seconds")
        wait.add_argument("--submitted", action="store_true", default=True,
                          help="wait for submitted tickets (default)")
        wait.add_argument("--poll", action="store_true", default=False,
                          help="enable poll fallback (explicit opt-in, not default)")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


async def _cmd_wait(
    client: Any,
    board_id: str,
    since: int,
    timeout_s: int,
    *,
    submitted: bool = False,
    poll_fallback: bool = False,
) -> None:
    """Block until work arrives using BoardClient.events() (subscriptions/listen).

    Returns a bounded JSON response with new_seq, events, timed_out.
    The caller re-arms by passing new_seq as --since.

    Default path: one long subscriptions/listen wait on the board journal.
    Never calls ticket_list during an idle wait. poll_fallback=True enables
    the explicit opt-in board_catchup poll loop as a last resort.
    """
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    cursor = since

    # Watch the board journal URI so events() subscribes to it
    journal_uri = f"board://{board_id}/journal"
    client.watch_resource(journal_uri)

    # Use BoardClient.events() for a long subscriptions/listen wait.
    # events() opens a subscription, drains existing events, then blocks
    # on subscription cues. It never calls ticket_list during the wait.
    if submitted:
        # Reviewer: watch for submitted tickets via ticket_status_changed
        kinds = {"ticket_status_changed"}
    else:
        kinds = {"ticket_created", "ticket_status_changed"}

    try:
        async with asyncio.timeout(timeout_s):
            async for event in client.events(
                from_cursor=cursor if cursor else None,
                kinds=kinds,
            ):
                # Filter reviewer events to status_to=submitted
                if submitted and event.get("status_to") not in ("submitted", None):
                    continue
                events.append(event)
                seq = event.get("seq", 0) or event.get("new_seq", 0)
                if seq:
                    cursor = max(cursor, seq)
                break
    except TimeoutError:
        pass
    except Exception:
        # events() unavailable - only use poll if explicitly opted in
        pass

    if not events and poll_fallback:
        # Explicit opt-in poll fallback
        deadline = started + max(1, timeout_s)
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(2.0, remaining))
            try:
                page = await client.board_catchup(
                    cursor=cursor, limit=50, ack=False
                )
                for ev in page.get("events", []):
                    if ev.get("kind") in kinds:
                        events.append(ev)
                        cursor = max(cursor, ev.get("seq", 0))
                if events:
                    break
            except Exception:
                pass

    timed_out = not events
    _print({
        "new_seq": cursor,
        "events": events,
        "waited_s": round(time.monotonic() - started, 2),
        "timed_out": timed_out,
    })


async def _execute(args: argparse.Namespace) -> None:
    BoardClient = _load_client()
    central_url = os.environ["ONBOARD_CENTRAL_URL"]
    token = os.environ["ONBOARD_CENTRAL_TOKEN"]
    board_id = os.environ["ONBOARD_BOARD_ID"]
    agent_name = os.environ["ONBOARD_AGENT_NAME"]
    async with BoardClient(
        central_url, token, board_id, agent_name=agent_name
    ) as client:
        await client.board_join()
        if args.command == "wait":
            poll = getattr(args, "poll", False)
            await _cmd_wait(
                client,
                board_id,
                since=args.since,
                timeout_s=args.timeout,
                submitted=ROLE != "worker",
                poll_fallback=poll,
            )
            return
        if ROLE == "worker":
            if args.command == "list":
                open_result = await client.ticket_list(status="open", limit=100)
                mine_result = await client.ticket_list(
                    assigned_to=agent_name, include_closed=False, limit=100
                )
                combined: dict[str, Any] = {}
                for result in (open_result, mine_result):
                    for ticket in result.get("tickets", []):
                        ticket_id = ticket.get("ticket_id")
                        if ticket_id:
                            combined[ticket_id] = ticket
                _print({"tickets": list(combined.values())})
                return
            if args.command == "get":
                _print(await client.ticket_get(args.ticket_id))
                return
            if args.command == "claim":
                _print(await client.ticket_claim(args.ticket_id))
                return
            if args.command == "renew":
                _print(await client.lease_renew(args.ticket_id))
                return
            if args.command == "submit":
                files = [item.strip() for item in args.files_csv.split(",") if item.strip()]
                if not files:
                    raise ValueError("files-csv must contain at least one path")
                _print(
                    await client.ticket_submit(
                        args.ticket_id,
                        summary=args.summary,
                        notes=args.notes,
                        files_changed=files,
                        stay_active=True,
                    )
                )
                return
        else:
            if args.command == "list":
                _print(await client.ticket_list(status="submitted", limit=100))
                return
            if args.command == "list-all":
                _print(await client.ticket_list(include_closed=False, limit=100))
                return
            if args.command == "get":
                _print(await client.ticket_get(args.ticket_id))
                return
            if args.command == "approve":
                _print(
                    await client.ticket_review(
                        args.ticket_id, "approve", review_notes=args.notes
                    )
                )
                return
            if args.command == "reject":
                _print(
                    await client.ticket_review(
                        args.ticket_id,
                        "reject",
                        review_notes=args.notes,
                        fix_instructions=args.fix,
                    )
                )
                return
        raise RuntimeError(f"unsupported command: {args.command}")


def main() -> int:
    try:
        asyncio.run(_execute(_parser().parse_args()))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"board.sh: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Keep transport/client failures concise.
        print(f"board.sh: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _board_python(role: str, repo_leaf: str | None) -> str:
    repo_leaf_val = repr(repo_leaf) if repo_leaf else "None"
    # Substitute placeholders using simple replacement to avoid brace conflicts
    result = _BOARD_PYTHON.replace("{role}", role, 1)
    result = result.replace("{repo_leaf}", repo_leaf_val, 1)
    return result


def _board_shell(
    *, name: str, board: str, central_url: str, token_file: Path, ca_file: Path
) -> str:
    values = {
        "agent": shlex.quote(name),
        "board": shlex.quote(board),
        "url": shlex.quote(central_url),
        "token": shlex.quote(str(token_file)),
        "ca": shlex.quote(str(ca_file)),
    }
    return f'''#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TOKEN_FILE=${{PURSERS_TOKEN_FILE:-{values["token"]}}}
CA_FILE=${{PURSERS_CA_FILE:-{values["ca"]}}}

if [ ! -r "$TOKEN_FILE" ]; then
  echo "board.sh: token file is not readable: $TOKEN_FILE" >&2
  exit 1
fi
if [ ! -r "$CA_FILE" ]; then
  echo "board.sh: CA file is not readable: $CA_FILE" >&2
  exit 1
fi

ONBOARD_CENTRAL_TOKEN=$(tr -d '\\r\\n' < "$TOKEN_FILE")
if [ -z "$ONBOARD_CENTRAL_TOKEN" ]; then
  echo "board.sh: token file is empty: $TOKEN_FILE" >&2
  exit 1
fi

export ONBOARD_CENTRAL_TOKEN
export ONBOARD_CENTRAL_URL=${{PURSERS_CENTRAL_URL:-{values["url"]}}}
export ONBOARD_BOARD_ID=${{PURSERS_BOARD:-{values["board"]}}}
export ONBOARD_AGENT_NAME={values["agent"]}
export SSL_CERT_FILE="$CA_FILE"

exec python3 "$SCRIPT_DIR/board.py" "$@"
'''


def _instructions(*, role: str, name: str, client: str) -> str:
    if role == "worker":
        commands = """bin/board.sh list
bin/board.sh get <TK>
bin/board.sh claim <TK>
bin/board.sh renew <TK>
bin/board.sh submit <TK> <summary> <notes> <files-csv>
bin/board.sh wait --since <cursor>"""
        loop = """Run this loop continuously:

1. **WAIT** -- `bin/board.sh wait --since <cursor>` blocks on Central's subscriptions/listen until claimable work arrives. Returns `{new_seq, events, timed_out}`. On `timed_out=true`, re-arm immediately: `bin/board.sh wait --since <new_seq>`.
2. **CLAIM** -- `bin/board.sh claim <TK>`. If the claim fails (race lost), go back to WAIT.
3. **UNDERSTAND** -- `bin/board.sh get <TK>` for full description + required_fields.
4. **DO** -- Perform the work. Run `bin/board.sh renew <TK>` every ~10 minutes.
5. **SUBMIT** -- `bin/board.sh submit <TK> <summary> <notes> <files-csv>`.
6. **RE-ARM** -- Return to WAIT. If rejected, follow fix instructions and resubmit.

Never poll `bin/board.sh list` in a loop. The wait verb blocks on Central's subscriptions/listen, using zero model turns except the re-arm."""
    else:
        commands = """bin/board.sh list
bin/board.sh list-all
bin/board.sh get <TK>
bin/board.sh approve <TK> <notes>
bin/board.sh reject <TK> <notes> <fix>
bin/board.sh wait --since <cursor>"""
        loop = """Run this loop continuously:

1. **WAIT** -- `bin/board.sh wait --since <cursor>` blocks on Central's subscriptions/listen until submitted tickets arrive. Returns `{new_seq, events, timed_out}`. On `timed_out=true`, re-arm immediately.
2. **REVIEW** -- `bin/board.sh list` shows submitted tickets. `bin/board.sh get <TK>` for details.
3. **APPROVE/REJECT** -- `bin/board.sh approve <TK> <notes>` or `bin/board.sh reject <TK> <notes> <fix>`.
4. **RE-ARM** -- Return to WAIT.

Never poll `bin/board.sh list` in a loop. The wait verb blocks on Central's subscriptions/listen, using zero model turns except the re-arm."""
    return f"""# Pursers seat: {name}

this folder IS your identity: {name}

Role: `{role}`

Client: `{client}`

## CLI

```sh
{commands}
```

Set `PURSERS_BOARD=<board-id>` for each board in the pool. The generated default is used when the variable is absent.

## Relentless loop

{loop}

## Governance

- one ticket at a time
- never review your own work
- workers never call ticket_review
- reviewers never claim/submit/write code/push
- stay in ticket scope
- report faithfully
- never push main / never force-push
"""


def _write(path: Path, content: str, mode: int) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def generate(args: argparse.Namespace) -> Path:
    if not NAME_RE.fullmatch(args.name):
        raise ValueError("--name must be a safe 1-80 character agent name")
    if not BOARD_RE.fullmatch(args.board):
        raise ValueError("--board must be a safe 1-80 character board ID")

    dest = Path(args.dest).expanduser().resolve()
    token_file = Path(args.token_file).expanduser().resolve()
    ca_file = Path(args.ca_file).expanduser().resolve()
    repo_leaf = _repo_leaf(args.repo) if args.repo else None

    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"destination is not empty: {dest}")
    dest.mkdir(parents=True, mode=0o700, exist_ok=True)
    dest.chmod(0o700)

    if args.repo:
        clone_dest = dest / repo_leaf
        subprocess.run(
            ["git", "clone", "--", args.repo, str(clone_dest)],
            check=True,
        )

    bin_dir = dest / "bin"
    bin_dir.mkdir(mode=0o755)
    bin_dir.chmod(0o755)
    _write(
        bin_dir / "board.sh",
        _board_shell(
            name=args.name,
            board=args.board,
            central_url=args.central_url,
            token_file=token_file,
            ca_file=ca_file,
        ),
        0o755,
    )
    _write(bin_dir / "board.py", _board_python(args.role, repo_leaf), 0o644)
    instructions = _instructions(role=args.role, name=args.name, client=args.client)
    _write(dest / "AGENTS.md", instructions, 0o644)
    _write(dest / ".goosehints", instructions, 0o644)
    return dest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("worker", "reviewer"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--central-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--repo")
    parser.add_argument("--board", default="pursers")
    parser.add_argument(
        "--client", choices=("goose", "codex", "claude", "generic"), default="generic"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        dest = generate(build_parser().parse_args(argv))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"seat_new.py: {exc}", file=sys.stderr)
        return 1
    print(f"Seat folder: {dest}")
    print("open your client here and type goal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
