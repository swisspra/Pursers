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
from contextlib import aclosing
from pathlib import Path
from typing import Any

ROLE = '{role}'
REPO_LEAF = {repo_leaf}
DEFAULT_WAIT_S = {wait_timeout}


def _load_client() -> tuple[Any, ...]:
    seat_root = Path(__file__).resolve().parents[1]
    if REPO_LEAF:
        source = seat_root / REPO_LEAF / "packages" / "client" / "src"
        if source.is_dir():
            sys.path.insert(0, str(source))
    try:
        from pursers_client import (
            BoardClient,
            PROJECT_REGISTRY_KEY,
            active_registry_boards,
            parse_project_registry,
            registry_project_work_dirs,
            registry_work_dirs,
            wait_for_boards,
        )
    except ImportError as exc:
        raise RuntimeError(
            "pursers_client is unavailable; generate with --repo or install pursers-client"
        ) from exc
    return (
        BoardClient,
        PROJECT_REGISTRY_KEY,
        active_registry_boards,
        parse_project_registry,
        registry_project_work_dirs,
        registry_work_dirs,
        wait_for_boards,
    )


def _parse_since(value: str) -> int | dict[str, int]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("--since must be an integer or JSON cursor map") from exc
    if type(parsed) is int:
        return max(0, parsed)
    if isinstance(parsed, dict) and all(
        isinstance(key, str) and type(cursor) is int
        for key, cursor in parsed.items()
    ):
        return {key: max(0, cursor) for key, cursor in parsed.items()}
    raise argparse.ArgumentTypeError("--since must be an integer or JSON cursor map")


def _target(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--board", help="target board (defaults to the home board)")
    return parser


def _wait_args(wait: argparse.ArgumentParser) -> None:
    wait.add_argument("--since", type=_parse_since, default=0,
                      help="integer cursor or JSON board-to-cursor map")
    wait.add_argument("--timeout", type=int, default=DEFAULT_WAIT_S,
                      help="max wait seconds")
    wait.add_argument("--boards", default="registry",
                      help="registry (default), home, or comma-separated board IDs")
    wait.add_argument("--poll", action="store_true", default=False,
                      help="enable poll fallback (explicit opt-in, not default)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="board.sh")
    commands = parser.add_subparsers(dest="command", required=True)
    if ROLE == "worker":
        _target(commands.add_parser("list", help="list open tickets and this seat's claim"))
        get = _target(commands.add_parser("get"))
        get.add_argument("ticket_id")
        claim = _target(commands.add_parser("claim"))
        claim.add_argument("ticket_id")
        renew = _target(commands.add_parser("renew"))
        renew.add_argument("ticket_id")
        submit = _target(commands.add_parser("submit"))
        submit.add_argument("ticket_id")
        submit.add_argument("summary")
        submit.add_argument("notes")
        submit.add_argument("files_csv")
        wait = commands.add_parser("wait", help="block until work arrives (subscriptions/listen)")
        _wait_args(wait)
    else:
        _target(commands.add_parser("list", help="list submitted tickets"))
        _target(commands.add_parser("list-all", help="list all non-closed tickets"))
        get = _target(commands.add_parser("get"))
        get.add_argument("ticket_id")
        approve = _target(commands.add_parser("approve"))
        approve.add_argument("ticket_id")
        approve.add_argument("notes")
        reject = _target(commands.add_parser("reject"))
        reject.add_argument("ticket_id")
        reject.add_argument("notes")
        reject.add_argument("fix")
        wait = commands.add_parser("wait", help="block until submitted tickets arrive")
        _wait_args(wait)
        wait.add_argument("--submitted", action="store_true",
                          help="wait for submitted tickets")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


async def _cmd_wait(
    client: Any,
    board_id: str,
    since: int | dict[str, int],
    timeout_s: int,
    *,
    submitted: bool = False,
    poll_fallback: bool = False,
    boards: str = "home",
    registry: dict[str, Any] | None = None,
    active_registry_boards: Any = None,
    registry_work_dirs: Any = None,
    registry_project_work_dirs: Any = None,
    wait_for_boards: Any = None,
) -> None:
    """Return one relevant event or a bounded timeout/re-arm response."""
    if timeout_s < 1:
        raise ValueError("--timeout must be at least 1 second")
    if client.identity is None:
        raise RuntimeError("wait requires a joined BoardClient identity")

    if boards != "home":
        if wait_for_boards is None:
            raise RuntimeError("pursers_client lacks registry wait support")
        if boards == "registry":
            if registry is None:
                raise RuntimeError("project_registry is unavailable")
            selected = active_registry_boards(registry, board_id)
        else:
            selected = [item.strip() for item in boards.split(",") if item.strip()]
            if not selected:
                raise ValueError("--boards must select at least one board")
        result = await wait_for_boards(
            client,
            selected,
            since,
            timeout_s,
            kinds=(
                frozenset({"ticket_status_changed"})
                if submitted
                else frozenset({"ticket_created", "ticket_status_changed"})
            ),
            submitted=submitted,
            work_dirs=registry_work_dirs(registry) if registry else {},
            project_work_dirs=registry_project_work_dirs(registry) if registry else {},
            poll_fallback=poll_fallback,
        )
        _print(result)
        return

    if isinstance(since, dict):
        since = int(since.get(board_id, 0))

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
                event_stream = client.events(
                    from_cursor=cursor,
                    only_mine=not submitted,
                    kinds=kinds,
                    resource_subscriptions=(journal_uri, seat_uri),
                    acknowledge=False,
                    touch=False,
                    cursor_callback=remember_cursor,
                )
                async with aclosing(event_stream):
                    async for event in event_stream:
                        if submitted and event.get("status_to") != "submitted":
                            continue
                        events.append(event)
                        remember_cursor(event.get("seq", cursor))
                        # One cue is intentional: callers refetch authoritative
                        # state, then re-arm from the returned cursor.
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
    loaded = _load_client()
    legacy_client_only = not isinstance(loaded, tuple)
    if legacy_client_only:
        BoardClient = loaded
        registry_key = active_boards = parse_registry = None
        project_work_dirs_for_registry = work_dirs_for_registry = wait_many = None
    else:
        (
            BoardClient,
            registry_key,
            active_boards,
            parse_registry,
            project_work_dirs_for_registry,
            work_dirs_for_registry,
            wait_many,
        ) = loaded
    central_url = os.environ["ONBOARD_CENTRAL_URL"]
    token = os.environ["ONBOARD_CENTRAL_TOKEN"]
    board_id = os.environ["ONBOARD_BOARD_ID"]
    agent_name = os.environ["ONBOARD_AGENT_NAME"]
    async with BoardClient(
        central_url, token, board_id, agent_name=agent_name
    ) as client:
        await client.board_join()
        registry = None
        if not legacy_client_only:
            try:
                registry = parse_registry(await client.board_state_get(key=registry_key))
            except (RuntimeError, ValueError):
                if args.command == "wait" and args.boards == "registry":
                    raise RuntimeError("project_registry is unavailable")
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
                boards="home" if legacy_client_only else args.boards,
                registry=registry,
                active_registry_boards=active_boards,
                registry_work_dirs=work_dirs_for_registry,
                registry_project_work_dirs=project_work_dirs_for_registry,
                wait_for_boards=wait_many,
            )
            return
        target_board = getattr(args, "board", None) or board_id
        board_work_dirs = work_dirs_for_registry(registry) if registry else {}
        project_work_dirs = project_work_dirs_for_registry(registry) if registry else {}

        async def run(target: Any) -> None:
            def emit(value: dict[str, Any]) -> None:
                ticket = value.get("ticket", {})
                project = str(ticket.get("target_url", "")).split("/", 1)[0].casefold()
                work_dir = project_work_dirs.get(project, board_work_dirs.get(target_board))
                _print({**value, "board_id": target_board, "work_dir": work_dir})

            if ROLE == "worker":
                if args.command == "list":
                    open_result = await target.ticket_list(status="open", limit=100)
                    mine_result = await target.ticket_list(
                        assigned_to=agent_name, include_closed=False, limit=100
                    )
                    combined: dict[str, Any] = {}
                    for result in (open_result, mine_result):
                        for ticket in result.get("tickets", []):
                            ticket_id = ticket.get("ticket_id")
                            if ticket_id:
                                combined[ticket_id] = ticket
                    emit({"tickets": list(combined.values())})
                    return
                if args.command == "get":
                    emit(await target.ticket_get(args.ticket_id))
                    return
                if args.command == "claim":
                    emit(await target.ticket_claim(args.ticket_id))
                    return
                if args.command == "renew":
                    emit(await target.lease_renew(args.ticket_id))
                    return
                if args.command == "submit":
                    files = [item.strip() for item in args.files_csv.split(",") if item.strip()]
                    if not files:
                        raise ValueError("files-csv must contain at least one path")
                    emit(await target.ticket_submit(
                        args.ticket_id, summary=args.summary, notes=args.notes,
                        files_changed=files, stay_active=True,
                    ))
                    return
            else:
                if args.command == "list":
                    emit(await target.ticket_list(status="submitted", limit=100))
                    return
                if args.command == "list-all":
                    emit(await target.ticket_list(include_closed=False, limit=100))
                    return
                if args.command == "get":
                    emit(await target.ticket_get(args.ticket_id))
                    return
                if args.command == "approve":
                    emit(await target.ticket_review(
                        args.ticket_id, "approve", review_notes=args.notes
                    ))
                    return
                if args.command == "reject":
                    emit(await target.ticket_review(
                        args.ticket_id, "reject", review_notes=args.notes,
                        fix_instructions=args.fix,
                    ))
                    return
            raise RuntimeError(f"unsupported command: {args.command}")

        if target_board == board_id:
            await run(client)
        else:
            async with BoardClient(
                central_url, token, target_board, agent_name=agent_name
            ) as target:
                await run(target)
        return


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
        commands = """bin/board.sh list [--board <id>]
bin/board.sh get <TK> --board <id>
bin/board.sh claim <TK> --board <id>
bin/board.sh renew <TK> --board <id>
bin/board.sh submit <TK> <summary> <notes> <files-csv> --board <id>
bin/board.sh wait --since '<cursor-or-json-map>' [--boards registry|home|<id,id>]"""
        loop = """Run this loop continuously:

1. **WAIT** -- `bin/board.sh wait --since '<cursor-or-json-map>'` subscribes to every active registry board. Returns `{new_seq, events, timed_out, boards, skipped_boards}`; each event carries `board_id` and `work_dir`. Re-arm with the entire returned `new_seq` map.
2. **UNDERSTAND** -- `bin/board.sh get <TK> --board <id>` using the event's board. Its output repeats the registered `work_dir`; never guess or use another project tree.
3. **CLAIM** -- `bin/board.sh claim <TK> --board <id>`. If the claim fails (race lost), go back to WAIT.
4. **DO** -- Work only in the returned `work_dir`. Run `bin/board.sh renew <TK> --board <id>` every ~10 minutes.
5. **SUBMIT** -- `bin/board.sh submit <TK> <summary> <notes> <files-csv> --board <id>`.
6. **AWAIT REVIEW** -- Keep the same ticket slot occupied. WAIT, then GET that ticket after a cue. If rejected, follow fix instructions and resubmit; if approved/closed, release the slot.
7. **RE-ARM** -- Return to WAIT for the next ticket only after approval/closure.

Never poll `bin/board.sh list` in a loop. Polling exists only behind the explicit `wait --poll` fallback. The default wait blocks on Central's subscriptions/listen, using zero model turns except the re-arm."""
    else:
        commands = """bin/board.sh list [--board <id>]
bin/board.sh list-all [--board <id>]
bin/board.sh get <TK> --board <id>
bin/board.sh approve <TK> <notes> --board <id>
bin/board.sh reject <TK> <notes> <fix> --board <id>
bin/board.sh wait --submitted --since '<cursor-or-json-map>' [--boards registry|home|<id,id>]"""
        loop = """Run this loop continuously:

1. **WAIT** -- `bin/board.sh wait --submitted --since '<cursor-or-json-map>'` fans out across every active registry board. Re-arm with the entire returned `new_seq` map.
2. **REVIEW** -- Use the event's board: `bin/board.sh get <TK> --board <id>` for details and registered `work_dir`.
3. **APPROVE/REJECT** -- `bin/board.sh approve <TK> <notes> --board <id>` or `bin/board.sh reject <TK> <notes> <fix> --board <id>`.
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

`PURSERS_BOARD` selects the home board. Registry wait is the default; use each event's `board_id` with `--board` for subsequent commands.

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
