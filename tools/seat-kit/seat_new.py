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


def _board_python(role: str, repo_leaf: str | None) -> str:
    return f'''#!/usr/bin/env python3
"""Generated Pursers seat CLI. Do not put credentials in this file."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROLE = {role!r}
REPO_LEAF = {repo_leaf!r}


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
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


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
        if ROLE == "worker":
            if args.command == "list":
                open_result = await client.ticket_list(status="open", limit=100)
                mine_result = await client.ticket_list(
                    assigned_to=agent_name, include_closed=False, limit=100
                )
                combined: dict[str, Any] = {{}}
                for result in (open_result, mine_result):
                    for ticket in result.get("tickets", []):
                        ticket_id = ticket.get("ticket_id")
                        if ticket_id:
                            combined[ticket_id] = ticket
                _print({{"tickets": list(combined.values())}})
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
        raise RuntimeError(f"unsupported command: {{args.command}}")


def main() -> int:
    try:
        asyncio.run(_execute(_parser().parse_args()))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"board.sh: {{exc}}", file=sys.stderr)
        return 1
    except Exception as exc:  # Keep transport/client failures concise.
        print(f"board.sh: {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


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
bin/board.sh submit <TK> <summary> <notes> <files-csv>"""
        loop = """Poll every board via `PURSERS_BOARD`. Claim an open ticket immediately. If nothing is open, sleep 90-120 seconds and poll again. Work one ticket through submission and review before taking another. Renew the lease about every 10 minutes. Submit honestly with all required fields and name the model in use in the notes. If rejected, follow the fix instructions, renew or reclaim as required, and resubmit; otherwise re-arm for the next ticket."""
    else:
        commands = """bin/board.sh list
bin/board.sh list-all
bin/board.sh get <TK>
bin/board.sh approve <TK> <notes>
bin/board.sh reject <TK> <notes> <fix>"""
        loop = """Poll every board via `PURSERS_BOARD`. Review a submitted ticket immediately. If nothing is submitted, sleep 90-120 seconds and poll again. Inspect the evidence, run relevant verification, and approve or reject honestly. Continue until stopped."""
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
