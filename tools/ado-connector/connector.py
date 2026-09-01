#!/usr/bin/env python3
"""Generic Azure DevOps PR-to-Pursers connector and faithful fake fixture."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


LOG = logging.getLogger("ado-connector")
CONFIG_MODE = 0o600
STATE_SCHEMA_VERSION = 1
MAX_THREAD_SUMMARY_CHARS = 1_000
MAX_COMMENT_SUMMARY_CHARS = 2_000
COMMIT_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,64})(?![0-9a-fA-F])")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class ConfigError(ValueError):
    pass


class AdoError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdoSettings:
    base_url: str
    project: str
    repo: str
    pat_env: str


@dataclass(frozen=True)
class CentralSettings:
    url: str
    token_path: Path
    create_mode: str = "intake"


@dataclass(frozen=True)
class BoardSettings:
    board_id: str
    target_url_prefix: str
    agent_name: str = "ado-connector"


@dataclass(frozen=True)
class FilterSettings:
    authors: tuple[str, ...]
    labels: tuple[str, ...]
    vote_reviewer_id: str = "ado-connector"
    closed_vote: int = 0


@dataclass(frozen=True)
class ConnectorConfig:
    ado: AdoSettings
    central: CentralSettings
    board: BoardSettings
    filters: FilterSettings
    poll_seconds: int
    state_file: Path

    @classmethod
    def load(cls, path: Path) -> "ConnectorConfig":
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ConfigError("config path must name an existing file")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != CONFIG_MODE:
            raise ConfigError("config file mode must be 0600")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError("config must be valid JSON") from exc
        if not isinstance(raw, Mapping):
            raise ConfigError("config must be a JSON object")
        ado = _mapping(raw, "ado")
        central = _mapping(raw, "central")
        board = _mapping(raw, "board")
        filters = _mapping(raw, "filters")
        pat_env = _text(ado, "pat_env")
        if not ENV_NAME_RE.fullmatch(pat_env):
            raise ConfigError("ado.pat_env must be an environment variable name")
        base_url = _text(ado, "base_url").rstrip("/")
        parsed_url = urllib.parse.urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigError("ado.base_url must be an HTTP(S) URL")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ConfigError("ado.base_url must not contain credentials, query, or fragment")
        central_url = _text(central, "url")
        central_parsed = urllib.parse.urlsplit(central_url)
        if central_parsed.scheme not in {"http", "https"} or not central_parsed.netloc:
            raise ConfigError("central.url must be an HTTP(S) URL")
        if central_parsed.username or central_parsed.password or central_parsed.query or central_parsed.fragment:
            raise ConfigError("central.url must not contain credentials, query, or fragment")
        token_path = Path(_text(central, "token_path")).expanduser()
        if not token_path.is_absolute() or not token_path.is_file():
            raise ConfigError("central.token_path must be an existing absolute file")
        create_mode = central.get("create_mode", "intake")
        if create_mode not in {"intake", "writer"}:
            raise ConfigError("central.create_mode must be intake or writer")
        authors = _text_list(filters, "authors")
        labels = _text_list(filters, "labels")
        if not authors and not labels:
            raise ConfigError("filters.authors or filters.labels must be configured")
        vote = filters.get("closed_vote", 0)
        if isinstance(vote, bool) or not isinstance(vote, int) or vote not in {-5, 0}:
            raise ConfigError("filters.closed_vote must be -5 or 0; approval votes are forbidden")
        poll_seconds = raw.get("poll_seconds", 60)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int) or poll_seconds < 1:
            raise ConfigError("poll_seconds must be a positive integer")
        state_value = raw.get("state_file", "ado-connector-state.json")
        if not isinstance(state_value, str) or not state_value.strip():
            raise ConfigError("state_file must be a non-empty path")
        state_file = Path(state_value).expanduser()
        if not state_file.is_absolute():
            state_file = path.parent / state_file
        agent_name = board.get("agent_name", "ado-connector")
        reviewer_id = filters.get("vote_reviewer_id", "ado-connector")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ConfigError("board.agent_name must be non-empty")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ConfigError("filters.vote_reviewer_id must be non-empty")
        return cls(
            ado=AdoSettings(
                base_url=base_url,
                project=_text(ado, "project"),
                repo=_text(ado, "repo"),
                pat_env=pat_env,
            ),
            central=CentralSettings(central_url, token_path.resolve(), create_mode),
            board=BoardSettings(
                board_id=_text(board, "id"),
                target_url_prefix=_text(board, "target_url_prefix").strip("/"),
                agent_name=agent_name.strip(),
            ),
            filters=FilterSettings(
                authors=tuple(value.casefold() for value in authors),
                labels=tuple(value.casefold() for value in labels),
                vote_reviewer_id=reviewer_id.strip(),
                closed_vote=vote,
            ),
            poll_seconds=poll_seconds,
            state_file=state_file.resolve(),
        ).validated()

    def validated(self) -> "ConnectorConfig":
        if not self.board.agent_name:
            raise ConfigError("board.agent_name must be non-empty")
        if not self.board.target_url_prefix:
            raise ConfigError("board.target_url_prefix must be non-empty")
        if not self.filters.vote_reviewer_id:
            raise ConfigError("filters.vote_reviewer_id must be non-empty")
        return self


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be an object")
    return value


def _text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _text_list(raw: Mapping[str, Any], key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f"filters.{key} must be a list of non-empty strings")
    return [item.strip() for item in value]


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"schema_version": STATE_SCHEMA_VERSION, "items": {}}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != STATE_SCHEMA_VERSION
                or not isinstance(value.get("items"), dict)
            ):
                raise ValueError("unsupported state schema")
            for key, item in value["items"].items():
                if (
                    not isinstance(key, str)
                    or not isinstance(item, dict)
                    or isinstance(item.get("pr_id"), bool)
                    or not isinstance(item.get("pr_id"), int)
                    or not isinstance(item.get("source_commit"), str)
                    or not re.fullmatch(r"[0-9a-f]{7,64}", item["source_commit"])
                    or not isinstance(item.get("ticket_id"), str)
                    or not item["ticket_id"]
                    or not isinstance(item.get("commented", False), bool)
                    or not isinstance(item.get("voted", False), bool)
                ):
                    raise ValueError("invalid state item")
            return value
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            suffix = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            recovered = self.path.with_name(f"{self.path.name}.corrupt-{suffix}")
            counter = 1
            while recovered.exists():
                recovered = self.path.with_name(f"{self.path.name}.corrupt-{suffix}-{counter}")
                counter += 1
            os.replace(self.path, recovered)
            LOG.warning("state file was corrupt; moved aside as %s", recovered.name)
            return self.empty()

    def save(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()


class BoardGateway(Protocol):
    def create_ticket(self, ticket_id: str, body: Mapping[str, Any]) -> str: ...

    def get_ticket(self, ticket_id: str) -> Mapping[str, Any]: ...


class PursersBoardGateway:
    def __init__(self, config: ConnectorConfig):
        self.config = config
        self.token = config.central.token_path.read_text(encoding="utf-8").strip()
        if not self.token:
            raise ConfigError("central token file is empty")

    async def _create(self, ticket_id: str, body: Mapping[str, Any]) -> str:
        from pursers_client import BoardClient, BoardClientError

        async with BoardClient(
            self.config.central.url,
            self.token,
            self.config.board.board_id,
            agent_name=self.config.board.agent_name,
        ) as client:
            try:
                if self.config.central.create_mode == "intake":
                    result = await client._call(  # noqa: SLF001 - additive narrow capability.
                        "ticket_create",
                        {
                            "agent_name": self.config.board.agent_name,
                            "ticket_id": ticket_id,
                            "title": str(body["title"]),
                            "description": str(body["description"]),
                            "scope": "interactive-no-send",
                            "required_fields": ["commit_hash", "test_output"],
                            "tags": list(body["tags"]),
                            "target_url": str(body["target_url"]),
                            "unassigned": True,
                            "coordinator_op_key": create_op_key(ticket_id),
                        },
                    )
                else:
                    result = await client.ticket_create(
                        ticket_id,
                        str(body["title"]),
                        description=str(body["description"]),
                        scope="interactive-no-send",
                        required_fields=["commit_hash", "test_output"],
                        tags=list(body["tags"]),
                        target_url=str(body["target_url"]),
                        unassigned=True,
                    )
            except BoardClientError as exc:
                try:
                    existing = (await client.ticket_get(ticket_id))["ticket"]
                except BoardClientError:
                    raise exc
                if (
                    existing.get("target_url") != body["target_url"]
                    or "connector-ado" not in existing.get("tags", [])
                ):
                    raise RuntimeError("deterministic ticket id collision") from exc
                return ticket_id
            return str(result["ticket"]["ticket_id"])

    async def _get(self, ticket_id: str) -> Mapping[str, Any]:
        from pursers_client import BoardClient

        async with BoardClient(
            self.config.central.url,
            self.token,
            self.config.board.board_id,
            agent_name=self.config.board.agent_name,
        ) as client:
            return (await client.ticket_get(ticket_id))["ticket"]

    def create_ticket(self, ticket_id: str, body: Mapping[str, Any]) -> str:
        return asyncio.run(self._create(ticket_id, body))

    def get_ticket(self, ticket_id: str) -> Mapping[str, Any]:
        return asyncio.run(self._get(ticket_id))


class AdoClient:
    def __init__(self, settings: AdoSettings, pat: str):
        if not pat:
            raise ConfigError(f"environment variable {settings.pat_env} is empty")
        self.settings = settings
        encoded = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        self._authorization = f"Basic {encoded}"

    def _repository_url(self) -> str:
        project = urllib.parse.quote(self.settings.project, safe="")
        repo = urllib.parse.quote(self.settings.repo, safe="")
        return f"{self.settings.base_url}/{project}/_apis/git/repositories/{repo}"

    def _request(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": self._authorization,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise AdoError(f"ADO {method} failed with HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise AdoError(f"ADO {method} transport failed: {type(exc.reason).__name__}") from None
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise AdoError("ADO returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise AdoError("ADO returned a non-object response")
        return result

    def list_pull_requests(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"searchCriteria.status": "active", "api-version": "7.1"})
        result = self._request("GET", f"{self._repository_url()}/pullrequests?{query}")
        return [item for item in result.get("value", []) if isinstance(item, dict)]

    def list_threads(self, pr_id: int) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"{self._repository_url()}/pullRequests/{pr_id}/threads?api-version=7.1",
        )
        return [item for item in result.get("value", []) if isinstance(item, dict)]

    def post_comment(self, pr_id: int, content: str) -> None:
        self._request(
            "POST",
            f"{self._repository_url()}/pullRequests/{pr_id}/threads?api-version=7.1",
            {"comments": [{"parentCommentId": 0, "content": content, "commentType": 1}], "status": 1},
        )

    def set_vote(self, pr_id: int, reviewer_id: str, vote: int) -> None:
        reviewer = urllib.parse.quote(reviewer_id, safe="")
        self._request(
            "PUT",
            f"{self._repository_url()}/pullRequests/{pr_id}/reviewers/{reviewer}?api-version=7.1",
            {"vote": vote},
        )


def matches_filters(pr: Mapping[str, Any], filters: FilterSettings) -> bool:
    author = pr.get("createdBy", {})
    author_values = {
        str(author.get("id", "")).casefold(),
        str(author.get("displayName", "")).casefold(),
        str(author.get("uniqueName", "")).casefold(),
    } if isinstance(author, Mapping) else set()
    labels = {
        str(item.get("name", "")).casefold()
        for item in pr.get("labels", [])
        if isinstance(item, Mapping)
    }
    author_match = not filters.authors or bool(author_values.intersection(filters.authors))
    label_match = not filters.labels or bool(labels.intersection(filters.labels))
    return author_match and label_match


def pr_identity(pr: Mapping[str, Any]) -> tuple[int, str]:
    pr_id = pr.get("pullRequestId")
    commit = pr.get("lastMergeSourceCommit", {})
    commit_id = commit.get("commitId") if isinstance(commit, Mapping) else None
    if isinstance(pr_id, bool) or not isinstance(pr_id, int) or pr_id < 1:
        raise AdoError("PR is missing a valid pullRequestId")
    if not isinstance(commit_id, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_id):
        raise AdoError("PR is missing a valid source commit")
    return pr_id, commit_id.lower()


def deterministic_ticket_id(pr_id: int, commit: str) -> str:
    return f"TK-ado-{pr_id}-{commit[:12]}"


def create_op_key(ticket_id: str) -> str:
    digest = hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:24]
    return f"ado-create-{digest}"


def state_key(pr_id: int, commit: str) -> str:
    return f"{pr_id}:{commit}"


def pr_link(pr: Mapping[str, Any]) -> str:
    links = pr.get("_links", {})
    web = links.get("web", {}) if isinstance(links, Mapping) else {}
    value = web.get("href") if isinstance(web, Mapping) else None
    if not isinstance(value, str) or not value:
        value = pr.get("url")
    return str(value or "unavailable")[:500]


def thread_summary(threads: Sequence[Mapping[str, Any]]) -> str:
    snippets: list[str] = []
    for thread in threads:
        for comment in thread.get("comments", []) if isinstance(thread, Mapping) else []:
            if not isinstance(comment, Mapping):
                continue
            content = " ".join(str(comment.get("content", "")).split())
            if content:
                snippets.append(content[:180])
            if len(snippets) >= 3:
                break
        if len(snippets) >= 3:
            break
    rendered = f"{len(threads)} thread(s)"
    if snippets:
        rendered += "; " + " | ".join(snippets)
    return rendered[:MAX_THREAD_SUMMARY_CHARS]


def ticket_body(
    config: ConnectorConfig,
    pr: Mapping[str, Any],
    threads: Sequence[Mapping[str, Any]],
    previous_ticket_ids: Sequence[str],
) -> dict[str, Any]:
    pr_id, commit = pr_identity(pr)
    title = " ".join(str(pr.get("title", "Untitled PR")).split())[:170]
    source = str(pr.get("sourceRefName", "unknown"))[:300]
    target = str(pr.get("targetRefName", "unknown"))[:300]
    author = pr.get("createdBy", {})
    author_name = str(author.get("displayName", "unknown")) if isinstance(author, Mapping) else "unknown"
    prior = ", ".join(sorted(previous_ticket_ids)) or "none"
    description = "\n".join(
        (
            "Azure DevOps PR connector intake (automated; does not merge or approve).",
            f"PR: {pr_link(pr)}",
            f"PR id: {pr_id}",
            f"Author: {author_name[:200]}",
            f"Branch: {source} -> {target}",
            f"Source commit: {commit}",
            f"Finding threads: {thread_summary(threads)}",
            f"Previous connector tickets for this PR: {prior}",
        )
    )
    return {
        "title": f"[ADO PR {pr_id}] {title}"[:200],
        "description": description[:5_000],
        "tags": ["connector-ado", f"ado-pr-{pr_id}"],
        "target_url": f"{config.board.target_url_prefix}/{pr_id}/{commit[:12]}",
    }


def submitted_commit(ticket: Mapping[str, Any]) -> str:
    direct = ticket.get("commit_hash")
    if isinstance(direct, str) and COMMIT_RE.fullmatch(direct):
        return direct.lower()
    histories = ticket.get("submission_history", [])
    sources: list[str] = []
    if isinstance(histories, list):
        for item in reversed(histories):
            if isinstance(item, Mapping):
                sources.extend(str(item.get(key, "")) for key in ("notes", "summary"))
    sources.extend(str(ticket.get(key, "")) for key in ("notes", "summary"))
    for source in sources:
        match = COMMIT_RE.search(source)
        if match:
            return match.group(1).lower()
    return "not-reported"


def writeback_marker(pr_id: int, ticket_id: str) -> str:
    digest = hashlib.sha256(f"{pr_id}\0{ticket_id}".encode("utf-8")).hexdigest()[:20]
    return f"<!-- pursers-ado:{digest} -->"


def threads_contain_marker(threads: Sequence[Mapping[str, Any]], marker: str) -> bool:
    return any(
        marker in str(comment.get("content", ""))
        for thread in threads
        if isinstance(thread, Mapping)
        for comment in thread.get("comments", [])
        if isinstance(comment, Mapping)
    )


class Connector:
    def __init__(
        self,
        config: ConnectorConfig,
        ado: AdoClient,
        board: BoardGateway,
        state_store: StateStore,
    ):
        self.config = config
        self.ado = ado
        self.board = board
        self.state_store = state_store

    def cycle(self) -> list[str]:
        state = self.state_store.load()
        items: dict[str, Any] = state["items"]
        actions: list[str] = []
        for pr in self.ado.list_pull_requests():
            if not matches_filters(pr, self.config.filters):
                continue
            pr_id, commit = pr_identity(pr)
            key = state_key(pr_id, commit)
            if key in items:
                continue
            previous = [
                str(item.get("ticket_id"))
                for item in items.values()
                if isinstance(item, Mapping)
                and item.get("pr_id") == pr_id
                and item.get("ticket_id")
            ]
            ticket_id = deterministic_ticket_id(pr_id, commit)
            body = ticket_body(self.config, pr, self.ado.list_threads(pr_id), previous)
            created = self.board.create_ticket(ticket_id, body)
            items[key] = {
                "pr_id": pr_id,
                "source_commit": commit,
                "ticket_id": created,
                "commented": False,
                "voted": False,
            }
            self.state_store.save(state)
            actions.append(f"created {created} for PR {pr_id}@{commit[:12]}")

        for key in sorted(items):
            item = items[key]
            if not isinstance(item, dict) or not item.get("ticket_id"):
                continue
            ticket = self.board.get_ticket(str(item["ticket_id"]))
            if ticket.get("status") != "closed":
                continue
            pr_id = int(item["pr_id"])
            marker = writeback_marker(pr_id, str(item["ticket_id"]))
            if not item.get("commented"):
                threads = self.ado.list_threads(pr_id)
                if not threads_contain_marker(threads, marker):
                    summary = " ".join(str(ticket.get("summary", "Ticket completed")).split())
                    commit = submitted_commit(ticket)
                    comment = "\n".join(
                        (
                            marker,
                            "Automated Pursers connector update (not a merge or human approval).",
                            f"Ticket: {item['ticket_id']}",
                            f"Summary: {summary[:MAX_COMMENT_SUMMARY_CHARS]}",
                            f"Submitted commit: {commit}",
                        )
                    )
                    self.ado.post_comment(pr_id, comment)
                item["commented"] = True
                self.state_store.save(state)
                actions.append(f"commented PR {pr_id} for {item['ticket_id']}")
            if not item.get("voted"):
                self.ado.set_vote(
                    pr_id,
                    self.config.filters.vote_reviewer_id,
                    self.config.filters.closed_vote,
                )
                item["voted"] = True
                self.state_store.save(state)
                actions.append(
                    f"set connector vote {self.config.filters.closed_vote} on PR {pr_id} "
                    f"for {item['ticket_id']}"
                )
        return actions


class FakeAdoFixture:
    """Thread-safe data model behind the fake ADO REST server."""

    def __init__(self, pat: str, project: str = "demo", repo: str = "repo"):
        self.pat = pat
        self.project = project
        self.repo = repo
        self.pull_requests: dict[int, dict[str, Any]] = {}
        self.threads: dict[int, list[dict[str, Any]]] = {}
        self.votes: dict[tuple[int, str], int] = {}
        self.comment_posts: list[tuple[int, str]] = []
        self.auth_failures = 0
        self._lock = threading.Lock()

    def add_pr(
        self,
        pr_id: int,
        title: str,
        commit: str,
        *,
        author: str = "scanner-bot",
        labels: Sequence[str] = ("finding",),
        source: str = "refs/heads/scanner/finding",
        target: str = "refs/heads/main",
    ) -> None:
        with self._lock:
            self.pull_requests[pr_id] = {
                "pullRequestId": pr_id,
                "title": title,
                "url": f"https://example.invalid/pr/{pr_id}",
                "_links": {"web": {"href": f"https://example.invalid/pr/{pr_id}"}},
                "sourceRefName": source,
                "targetRefName": target,
                "lastMergeSourceCommit": {"commitId": commit},
                "createdBy": {"id": author, "displayName": author, "uniqueName": author},
                "labels": [{"name": label} for label in labels],
                "status": "active",
            }
            self.threads.setdefault(pr_id, [])

    def update_commit(self, pr_id: int, commit: str) -> None:
        with self._lock:
            self.pull_requests[pr_id]["lastMergeSourceCommit"] = {"commitId": commit}

    def add_thread(self, pr_id: int, content: str) -> None:
        with self._lock:
            rows = self.threads.setdefault(pr_id, [])
            rows.append({"id": len(rows) + 1, "comments": [{"id": 1, "content": content}]})


class _FakeAdoHandler(BaseHTTPRequestHandler):
    server: "_FakeAdoHttpServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(f":{self.server.fixture.pat}".encode()).decode()
        if self.headers.get("Authorization") == expected:
            return True
        self.server.fixture.auth_failures += 1
        self._send(401, {"message": "unauthorized"})
        return False

    def _send(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> tuple[str, int | None, str | None] | None:
        path = urllib.parse.urlsplit(self.path).path
        fixture = self.server.fixture
        prefix = (
            f"/{urllib.parse.quote(fixture.project, safe='')}/_apis/git/repositories/"
            f"{urllib.parse.quote(fixture.repo, safe='')}/pullrequests"
        )
        if path.casefold() == prefix.casefold():
            return "prs", None, None
        suffix = path[len(prefix):] if path.casefold().startswith(prefix.casefold()) else ""
        match = re.fullmatch(r"/(\d+)/threads", suffix, re.IGNORECASE)
        if match:
            return "threads", int(match.group(1)), None
        match = re.fullmatch(r"/(\d+)/reviewers/([^/]+)", suffix, re.IGNORECASE)
        if match:
            return "reviewer", int(match.group(1)), urllib.parse.unquote(match.group(2))
        return None

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            value = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if not self._authorized():
            return
        route = self._route()
        if route is None:
            self._send(404, {"message": "not found"})
            return
        kind, pr_id, _reviewer = route
        fixture = self.server.fixture
        with fixture._lock:
            if kind == "prs":
                rows = [dict(item) for item in fixture.pull_requests.values() if item.get("status") == "active"]
            elif kind == "threads" and pr_id in fixture.pull_requests:
                rows = json.loads(json.dumps(fixture.threads.get(pr_id, [])))
            else:
                self._send(404, {"message": "PR not found"})
                return
        self._send(200, {"count": len(rows), "value": rows})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if not self._authorized():
            return
        route = self._route()
        if route is None or route[0] != "threads" or route[1] is None:
            self._send(404, {"message": "not found"})
            return
        payload = self._payload()
        comments = payload.get("comments", [])
        content = str(comments[0].get("content", "")) if comments and isinstance(comments[0], Mapping) else ""
        fixture = self.server.fixture
        with fixture._lock:
            if route[1] not in fixture.pull_requests:
                self._send(404, {"message": "PR not found"})
                return
            rows = fixture.threads.setdefault(route[1], [])
            thread = {"id": len(rows) + 1, "comments": [{"id": 1, "content": content}]}
            rows.append(thread)
            fixture.comment_posts.append((route[1], content))
        self._send(200, thread)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if not self._authorized():
            return
        route = self._route()
        if route is None or route[0] != "reviewer" or route[1] is None or route[2] is None:
            self._send(404, {"message": "not found"})
            return
        payload = self._payload()
        vote = payload.get("vote")
        if vote not in {-5, 0}:
            self._send(400, {"message": "fake fixture forbids approval votes"})
            return
        with self.server.fixture._lock:
            self.server.fixture.votes[(route[1], route[2])] = int(vote)
        self._send(200, {"id": route[2], "vote": vote})


class _FakeAdoHttpServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], fixture: FakeAdoFixture):
        self.fixture = fixture
        super().__init__(address, _FakeAdoHandler)


class FakeAdoServer:
    def __init__(self, fixture: FakeAdoFixture, host: str = "127.0.0.1", port: int = 0):
        self.fixture = fixture
        self.httpd = _FakeAdoHttpServer((host, port), fixture)
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeAdoServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)


def run_connector(config: ConnectorConfig, once: bool) -> None:
    pat = os.environ.get(config.ado.pat_env, "")
    connector = Connector(
        config,
        AdoClient(config.ado, pat),
        PursersBoardGateway(config),
        StateStore(config.state_file),
    )
    while True:
        try:
            for action in connector.cycle():
                LOG.info("%s", action)
        except Exception as exc:
            if once:
                raise
            LOG.error("connector cycle failed: %s", type(exc).__name__)
        if once:
            return
        time.sleep(config.poll_seconds)


def run_fake_server(host: str, port: int, pat_env: str, project: str, repo: str) -> None:
    pat = os.environ.get(pat_env, "")
    if not pat:
        raise ConfigError(f"environment variable {pat_env} is empty")
    fixture = FakeAdoFixture(pat, project, repo)
    server = FakeAdoServer(fixture, host, port)
    print(json.dumps({"base_url": server.base_url, "project": project, "repo": repo}))
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the connector poll loop")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--once", action="store_true")
    fake = subparsers.add_parser("fake-server", help="run an empty fake ADO REST server")
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--port", type=int, default=8089)
    fake.add_argument("--pat-env", default="ADO_PAT")
    fake.add_argument("--project", default="demo")
    fake.add_argument("--repo", default="repo")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        if args.command == "run":
            run_connector(ConnectorConfig.load(args.config), args.once)
        else:
            run_fake_server(args.host, args.port, args.pat_env, args.project, args.repo)
    except (ConfigError, AdoError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(2) from None
    except Exception as exc:
        # Third-party transport failures may carry request internals. Keep the
        # process diagnostic useful without ever echoing credentials or headers.
        LOG.error("connector failed: %s", type(exc).__name__)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
