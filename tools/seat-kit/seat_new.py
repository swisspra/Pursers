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
CLIENT_PROFILES = {
    "goose": (300, 270),
    "codex": (620, 560),
    "claude": (21_600, 21_540),
    "generic": (180, 150),
}


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
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROLE = '{role}'
REPO_LEAF = {repo_leaf}
DEFAULT_WAIT_S = {wait_timeout}


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
        wait.add_argument("--timeout", type=int, default=DEFAULT_WAIT_S,
                          help="max wait seconds")
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
        wait.add_argument("--timeout", type=int, default=DEFAULT_WAIT_S,
                          help="max wait seconds")
        wait.add_argument("--submitted", action="store_true",
                          help="wait for submitted tickets")
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
    """Return one relevant event or a bounded timeout/re-arm response."""
    if timeout_s < 1:
        raise ValueError("--timeout must be at least 1 second")
    if client.identity is None:
        raise RuntimeError("wait requires a joined BoardClient identity")

    started = time.monotonic()
    events: list[dict[str, Any]] = []
    cursor = since
    journal_uri = f"board://{board_id}/journal"
    seat_uri = f"board://{board_id}/agent/{client.identity.agent_id}"
    kinds = (
        frozenset({"ticket_status_changed"})
        if submitted
        else frozenset({"ticket_created", "ticket_status_changed"})
    )

    def remember_cursor(value: int) -> None:
        nonlocal cursor
        cursor = max(cursor, int(value))

    if poll_fallback:
        deadline = started + max(1, timeout_s)
        while time.monotonic() < deadline and not events:
            page = await client.board_catchup(
                cursor=cursor, limit=50, ack=False, touch=False
            )
            remember_cursor(page.get("next_cursor", cursor))
            for event in page.get("events", []):
                if event.get("kind") not in kinds:
                    continue
                if submitted and event.get("status_to") != "submitted":
                    continue
                if not submitted and client.identity.agent_id not in event.get(
                    "recipient_identities", []
                ):
                    continue
                events.append(event)
                break
            if not events:
                await asyncio.sleep(min(2.0, max(0, deadline - time.monotonic())))
    else:
        required_parameters = {
            "resource_subscriptions", "acknowledge", "touch", "cursor_callback"
        }
        available_parameters = set(inspect.signature(client.events).parameters)
        missing = sorted(required_parameters - available_parameters)
        if missing:
            raise RuntimeError(
                "pursers_client lacks the approved pure subscription API: "
                + ", ".join(missing)
            )
        try:
            async with asyncio.timeout(timeout_s):
                async for event in client.events(
                    from_cursor=cursor,
                    only_mine=not submitted,
                    kinds=kinds,
                    resource_subscriptions=(journal_uri, seat_uri),
                    acknowledge=False,
                    touch=False,
                    cursor_callback=remember_cursor,
                ):
                    if submitted and event.get("status_to") != "submitted":
                        continue
                    events.append(event)
                    remember_cursor(event.get("seq", cursor))
                    break
        except TimeoutError:
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
            if ROLE != "worker" and not args.submitted:
                raise ValueError("reviewer wait requires --submitted")
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


def _board_python(role: str, repo_leaf: str | None, wait_timeout: int) -> str:
    repo_leaf_val = repr(repo_leaf) if repo_leaf else "None"
    # Substitute placeholders using simple replacement to avoid brace conflicts
    result = _BOARD_PYTHON.replace("{role}", role, 1)
    result = result.replace("{repo_leaf}", repo_leaf_val, 1)
    result = result.replace("{wait_timeout}", str(wait_timeout), 1)
    return result


def _board_shell(
    *,
    name: str,
    board: str,
    central_url: str,
    token_file: Path,
    ca_file: Path,
    python: Path,
) -> str:
    values = {
        "agent": shlex.quote(name),
        "board": shlex.quote(board),
        "url": shlex.quote(central_url),
        "token": shlex.quote(str(token_file)),
        "ca": shlex.quote(str(ca_file)),
        "python": shlex.quote(str(python)),
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

exec {values["python"]} "$SCRIPT_DIR/board.py" "$@"
'''


def _profile_guidance(client: str) -> str:
    host_timeout, wait_timeout = CLIENT_PROFILES[client]
    if client == "goose":
        return (
            f"Default host/wait profile: {host_timeout}s/{wait_timeout}s. "
            "For an opt-in one-hour Goose profile, set the exact config.yaml "
            "line `timeout: 3600`, then call `board.sh wait --timeout 3540`."
        )
    if client == "codex":
        return (
            f"Default host/wait profile: {host_timeout}s/{wait_timeout}s. "
            "Codex MCP config must set `tool_timeout_sec = 620`."
        )
    if client == "claude":
        return (
            f"Default host/wait profile: {host_timeout}s/{wait_timeout}s. "
            "Keep the MCP hard deadline above the generated wait duration."
        )
    return (
        f"Conservative generic host/wait profile: {host_timeout}s/{wait_timeout}s. "
        "Override `--timeout` only after verifying the host deadline."
    )


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
6. **AWAIT REVIEW** -- Keep the same ticket slot occupied. WAIT, then GET that ticket after a cue. If rejected, follow fix instructions and resubmit; if approved/closed, release the slot.
7. **RE-ARM** -- Return to WAIT for the next ticket only after approval/closure.

Never poll `bin/board.sh list` in a loop. Polling exists only behind the explicit `wait --poll` fallback. The default wait blocks on Central's subscriptions/listen, using zero model turns except the re-arm."""
    else:
        commands = """bin/board.sh list
bin/board.sh list-all
bin/board.sh get <TK>
bin/board.sh approve <TK> <notes>
bin/board.sh reject <TK> <notes> <fix>
bin/board.sh wait --submitted --since <cursor>"""
        loop = """Run this loop continuously:

1. **WAIT** -- `bin/board.sh wait --submitted --since <cursor>` blocks on Central's subscriptions/listen until submitted tickets arrive. Returns `{new_seq, events, timed_out}`. On `timed_out=true`, re-arm immediately with `<new_seq>`.
2. **REVIEW** -- `bin/board.sh list` shows submitted tickets. `bin/board.sh get <TK>` for details.
3. **APPROVE/REJECT** -- `bin/board.sh approve <TK> <notes>` or `bin/board.sh reject <TK> <notes> <fix>`.
4. **RE-ARM** -- Return to WAIT.

Never poll `bin/board.sh list` in a loop. Polling exists only behind the explicit `wait --submitted --poll` fallback. The default wait blocks on Central's subscriptions/listen, using zero model turns except the re-arm."""
    profile = _profile_guidance(client)
    return f"""# Pursers seat: {name}

this folder IS your identity: {name}

Role: `{role}`

Client: `{client}`

## CLI

```sh
{commands}
```

Set `PURSERS_BOARD=<board-id>` for each board in the pool. The generated default is used when the variable is absent.

Wait profile: {profile}

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
    python = Path(args.python).expanduser().resolve()

    if dest.exists() and any(dest.iterdir()) and not args.upgrade:
        raise ValueError(f"destination is not empty: {dest}")
    dest.mkdir(parents=True, mode=0o700, exist_ok=True)
    dest.chmod(0o700)

    if args.repo:
        clone_dest = dest / repo_leaf
        if not clone_dest.exists():
            subprocess.run(
                ["git", "clone", "--", args.repo, str(clone_dest)],
                check=True,
            )
        elif args.upgrade:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=clone_dest,
                check=False,
                text=True,
                capture_output=True,
            )
            if status.returncode == 0 and not status.stdout.strip():
                subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=clone_dest,
                    check=True,
                )

    bin_dir = dest / "bin"
    bin_dir.mkdir(mode=0o755, exist_ok=True)
    bin_dir.chmod(0o755)
    _write(
        bin_dir / "board.sh",
        _board_shell(
            name=args.name,
            board=args.board,
            central_url=args.central_url,
            token_file=token_file,
            ca_file=ca_file,
            python=python,
        ),
        0o755,
    )
    wait_timeout = CLIENT_PROFILES[args.client][1]
    _write(
        bin_dir / "board.py",
        _board_python(args.role, repo_leaf, wait_timeout),
        0o644,
    )
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
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="known interpreter used by generated board.sh",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="regenerate managed seat files in place and preserve all other files",
    )
    parser.add_argument("--repo")
    parser.add_argument("--board", default="pursers")
    parser.add_argument(
        "--client", choices=("goose", "codex", "claude", "generic"), default="generic"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dest = generate(args)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"seat_new.py: {exc}", file=sys.stderr)
        return 1
    print(f"Seat folder: {dest}")
    if args.client == "goose":
        print("Goose one-hour profile config.yaml line: timeout: 3600")
        print("Then use: bin/board.sh wait --timeout 3540 --since <cursor>")
    print("open your client here and type goal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
