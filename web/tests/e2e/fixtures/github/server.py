# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake GitHub API for the Review and Issues e2e (stdlib only; started by ``make e2e-server``).

Serves the recorded-shape JSON beside this file for one repository, ``e2e-dev/greeter``
(two open PRs), and records every request so a test can ask what reached "GitHub" at
``GET /__requests``. For Issues it keeps the repo's labels, issues, sub-issue links and comments
in memory: a test opens an issue with ``POST /__issues`` (``{title, body, labels, parent}``, opened
now; ``parent`` makes it a sub-issue of that number, as a fixture), and writes change them as
GitHub would: labels added and removed (an unknown label is refused rather than created, so a
write that relies on GitHub creating a label fails loudly), labels created and deleted, sub-issues
added (``replace_parent``), removed and reordered, issues created and closed, comments posted. It binds 127.0.0.1 on an ephemeral port and writes the
port to ``--port-file`` once it is listening; nothing else is reachable and no real credential is
ever read or needed (any ``Authorization: Bearer ...`` is accepted, none is a 401).

Fixture text may hold ``{{sha:<branch>}}``, replaced by that branch's commit in the bare
repository ``--repo`` (made by ``fixture.sh``), so the PRs name commits the review
workspace can really fetch.

    python server.py --repo <bare.git> --port-file <file>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
REPO = "e2e-dev/greeter"
VIEWER = "e2e-dev"
PLACEHOLDER = re.compile(r"\{\{sha:([^}]+)\}\}")
PULL = re.compile(rf"^/repos/{REPO}/pulls/(\d+)(?:/(files|commits|reviews|comments))?$")
COMMIT = re.compile(rf"^/repos/{REPO}/commits/([0-9a-f]{{40}})/(check-runs|status)$")
ISSUE = re.compile(
    rf"^/repos/{REPO}/issues/(\d+)"
    r"(?:/(labels|comments|timeline|sub_issues|sub_issue|parent)(?:/(.+))?)?$"
)
LABEL = re.compile(rf"^/repos/{REPO}/labels/(.+)$")
# The default label convention (Issues spec §2) plus an area, all present on the fake repo.
LABELS = (
    "agent-ready human-needed information-needed design-needed blocked icebox".split()
    + ["priority: high", "priority: medium", "priority: low"]
    + "bug enhancement tech-debt test performance security documentation".split()
    + ["area:cli"]
)
FIRST_ISSUE = 3  # 1 and 2 are the PRs
API = f"https://api.github.com/repos/{REPO}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """What the server serves and has seen; guarded by one lock."""

    def __init__(self, repo: Path) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.repo = repo
        self.posted_reviews: dict[int, list[dict[str, Any]]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.labels: list[str] = list(LABELS)
        # Each parent's sub-issues in priority order, and each comment posted.
        self.children: dict[int, list[int]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}

    def open_issue(self, body: Any) -> dict[str, Any]:
        number = FIRST_ISSUE + len(self.issues)
        data = body if isinstance(body, dict) else {}
        now = _now()
        issue = {
            "id": 7000 + number,
            "number": number,
            "title": str(data.get("title", f"Issue {number}")),
            "body": str(data.get("body", "")),
            "state": "open",
            "state_reason": None,
            "labels": [{"name": name} for name in data.get("labels", [])],
            "milestone": None,
            "created_at": now,
            "updated_at": now,
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "repository_url": API,
            "user": {"login": VIEWER, "type": "User"},
            "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
        }
        self.issues[number] = issue
        parent = data.get("parent")
        if isinstance(parent, int) and parent in self.issues:
            self.link(parent, number)
        return issue

    def relabel(self, issue: dict[str, Any], names: list[str]) -> list[dict[str, str]]:
        issue["labels"] = [{"name": name} for name in names]
        issue["updated_at"] = _now()
        return [{"name": name, "color": "ededed", "description": ""} for name in names]

    def by_id(self, issue_id: object) -> dict[str, Any] | None:
        return next((i for i in self.issues.values() if i["id"] == issue_id), None)

    def parent_of(self, number: int) -> int | None:
        return next((p for p, kids in self.children.items() if number in kids), None)

    def touch(self, *numbers: int) -> None:
        for n in numbers:
            issue = self.issues[n]
            issue["updated_at"] = _now()
            kids = self.children.get(n, [])
            done = sum(1 for k in kids if self.issues[k]["state"] == "closed")
            issue["sub_issues_summary"] = {
                "total": len(kids),
                "completed": done,
                "percent_completed": done * 100 // len(kids) if kids else 0,
            }

    def link(self, parent: int, child: int) -> None:
        former = self.parent_of(child)
        if former is not None:
            self.children[former].remove(child)
            self.touch(former)
        self.children.setdefault(parent, []).append(child)
        self.issues[child]["parent_issue_url"] = f"{API}/issues/{parent}"
        self.touch(parent, child)

    def unlink(self, parent: int, child: int) -> None:
        self.children[parent].remove(child)
        self.issues[child].pop("parent_issue_url", None)
        self.touch(parent, child)

    def create_issue(self, body: Any) -> tuple[int, Any]:
        data = body if isinstance(body, dict) else {}
        if any(name not in self.labels for name in data.get("labels", [])):
            return 422, {"message": "Validation Failed", "errors": ["unknown label"]}
        return 201, self.open_issue(data)

    def label_of(self, name: str) -> dict[str, str]:
        return {"name": name, "color": "ededed", "description": ""}

    def sha(self, branch: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def fixture(self, name: str) -> Any:
        text = (HERE / f"{name}.json").read_text(encoding="utf-8")
        return json.loads(PLACEHOLDER.sub(lambda m: self.sha(m.group(1)), text))

    def head_of(self, number: int) -> str:
        return str(self.fixture(f"pull-{number}")["head"]["sha"])

    def pull_for_sha(self, sha: str) -> int | None:
        for number in (1, 2):
            if self.head_of(number) == sha:
                return number
        return None


class Server(ThreadingHTTPServer):
    # A sync fetches many PR details at once; the default backlog of 5 refuses some connects.
    request_queue_size = 128
    daemon_threads = True


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send(self, status: int, body: Any) -> None:
            data = b"" if status == 204 else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode() if length else ""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return raw

        def _handle(self, method: str) -> None:
            url = urlsplit(self.path)
            body = self._body()
            if url.path == "/__requests":
                with state.lock:
                    self._send(200, state.requests)
                return
            if url.path == "/__issues" and method == "POST":
                with state.lock:
                    self._send(201, state.open_issue(body))
                return
            with state.lock:
                state.requests.append(
                    {"method": method, "path": url.path, "query": url.query, "body": body}
                )
                status, payload = self._route(method, url.path, parse_qs(url.query), body)
            self._send(status, payload)

        def _route(
            self, method: str, path: str, query: dict[str, list[str]], body: Any
        ) -> tuple[int, Any]:
            if not str(self.headers.get("Authorization", "")).startswith("Bearer "):
                return 401, {"message": "Requires authentication"}
            missing = (404, state.fixture("not-found"))
            if method == "POST" and path == "/graphql":
                return 200, state.fixture("threads-empty")
            if issue := ISSUE.match(path):
                found = state.issues.get(int(issue.group(1)))
                if found is not None:
                    return self._issue(method, found, issue.group(2), issue.group(3), body)
            if method == "POST" and path == f"/repos/{REPO}/issues":
                return state.create_issue(body)
            if method == "POST" and path == f"/repos/{REPO}/labels" and isinstance(body, dict):
                name = str(body.get("name", ""))
                if name in state.labels:
                    return 422, {"message": "Validation Failed", "errors": ["already_exists"]}
                state.labels.append(name)
                return 201, {**state.label_of(name), "color": str(body.get("color", "ededed"))}
            if method == "DELETE" and (label := LABEL.match(path)):
                name = unquote(label.group(1))
                if name not in state.labels:
                    return missing
                state.labels.remove(name)
                for each in state.issues.values():
                    each["labels"] = [lab for lab in each["labels"] if lab["name"] != name]
                return 204, None
            if method != "GET" and not path.endswith("/reviews"):
                return missing
            if path == f"/repos/{REPO}":
                return 200, {"full_name": REPO, "default_branch": "main"}
            if path == f"/repos/{REPO}/issues":
                wanted = query.get("labels", [None])[0]
                kept = query.get("state", ["open"])[0]
                listed = sorted(
                    (
                        i
                        for i in state.issues.values()
                        if (wanted is None or {"name": wanted} in i["labels"])
                        and kept in ("all", i["state"])
                    ),
                    key=lambda i: str(i["updated_at"]),
                )
                return 200, listed if query.get("page", ["1"])[0] == "1" else []
            if path == f"/repos/{REPO}/labels":
                labels = [state.label_of(name) for name in state.labels]
                return 200, labels if query.get("page", ["1"])[0] == "1" else []
            if label := LABEL.match(path):
                name = unquote(label.group(1))
                return (200, state.label_of(name)) if name in state.labels else missing
            if path == f"/repos/{REPO}/milestones":
                return 200, []
            if path == "/user":
                return 200, state.fixture("user")
            if path == "/user/orgs":
                return 200, state.fixture("orgs")
            if path == "/search/issues":
                text = " ".join(query.get("q", []))
                found = "review-requested:@me" in text
                return 200, state.fixture("search-review-requested" if found else "search-empty")
            if commit := COMMIT.match(path):
                number = state.pull_for_sha(commit.group(1))
                if number is None:
                    return missing
                name = (
                    f"pull-{number}-check-runs"
                    if commit.group(2) == "check-runs"
                    else "status-empty"
                )
                return 200, state.fixture(name)
            pull = PULL.match(path)
            if pull is None:
                return missing
            number, part = int(pull.group(1)), pull.group(2)
            if number not in (1, 2):
                return missing
            if method == "POST":
                return self._post_review(number, body)
            if part is None:
                return 200, state.fixture(f"pull-{number}")
            if part == "reviews":
                return 200, state.posted_reviews.get(number, [])
            if part == "comments":
                return 200, state.fixture("comments-empty")
            return 200, state.fixture(f"pull-{number}-{part}")

        def _issue(
            self, method: str, issue: dict[str, Any], part: str | None, rest: str | None, body: Any
        ) -> tuple[int, Any]:
            """An issue of the fake repo: read it, its comments, parent and sub-issues; change its
            labels the way GitHub does (except that an unknown label is refused), close it,
            comment on it, and add, remove or reorder its sub-issues."""
            number = int(issue["number"])
            names = [label["name"] for label in issue["labels"]]
            data = body if isinstance(body, dict) else {}
            missing = (404, state.fixture("not-found"))
            if method == "GET" and part is None:
                return 200, issue
            if method == "PATCH" and part is None:
                if data.get("state") == "closed":
                    issue["state"] = "closed"
                    issue["state_reason"] = data.get("state_reason", "completed")
                    issue["closed_at"] = _now()
                parent = state.parent_of(number)
                state.touch(number, *(() if parent is None else (parent,)))
                return 200, issue
            if method == "GET" and part == "timeline":
                return 200, []
            if part == "comments":
                if method == "POST":
                    posted = {
                        "id": 8000 + sum(len(c) for c in state.comments.values()) + 1,
                        "body": str(data.get("body", "")),
                        "user": {"login": VIEWER, "type": "User"},
                        "created_at": _now(),
                    }
                    posted["html_url"] = f"{issue['html_url']}#issuecomment-{posted['id']}"
                    state.comments.setdefault(number, []).append(posted)
                    return 201, posted
                return 200, state.comments.get(number, [])
            if method == "GET" and part == "parent":
                parent = state.parent_of(number)
                return missing if parent is None else (200, state.issues[parent])
            if method == "GET" and part == "sub_issues" and rest is None:
                return 200, [state.issues[k] for k in state.children.get(number, [])]
            if method == "POST" and part == "sub_issues" and rest is None:
                child = state.by_id(data.get("sub_issue_id"))
                if child is None:
                    return missing
                former = state.parent_of(int(child["number"]))
                if former is not None and not data.get("replace_parent"):
                    return 422, {"message": "Validation Failed", "errors": ["has a parent"]}
                state.link(number, int(child["number"]))
                return 201, issue
            if method == "DELETE" and part == "sub_issue":
                child = state.by_id(data.get("sub_issue_id"))
                if child is None or int(child["number"]) not in state.children.get(number, []):
                    return missing
                state.unlink(number, int(child["number"]))
                return 200, child
            if method == "PATCH" and part == "sub_issues" and rest == "priority":
                return self._reprioritize(number, data)
            if method == "POST" and part == "labels" and isinstance(body, dict):
                added = [str(name) for name in body.get("labels", [])]
                if any(name not in state.labels for name in added):
                    return 422, {"message": "Validation Failed", "errors": ["unknown label"]}
                return 200, state.relabel(issue, names + [n for n in added if n not in names])
            if method == "DELETE" and part == "labels" and rest is not None:
                name = unquote(rest)
                if name not in names:
                    return missing
                return 200, state.relabel(issue, [n for n in names if n != name])
            return missing

        def _reprioritize(self, number: int, data: dict[str, Any]) -> tuple[int, Any]:
            """Move a sub-issue after ``after_id`` or before ``before_id``, as GitHub does."""
            kids = state.children.get(number, [])
            child = state.by_id(data.get("sub_issue_id"))
            after = "after_id" in data
            anchor = state.by_id(data.get("after_id" if after else "before_id"))
            if child is None or anchor is None or int(child["number"]) not in kids:
                return 422, {"message": "Validation Failed", "errors": ["not a sub-issue"]}
            kids.remove(int(child["number"]))
            at = kids.index(int(anchor["number"])) + (1 if after else 0)
            kids.insert(at, int(child["number"]))
            state.touch(number)
            return 200, state.issues[number]

        def _post_review(self, number: int, body: Any) -> tuple[int, Any]:
            if not isinstance(body, dict) or body.get("commit_id") != state.head_of(number):
                return 422, {
                    "message": "Unprocessable Entity",
                    "errors": ["commit_id is not the head of this pull request"],
                }
            review_id = 9000 + sum(len(v) for v in state.posted_reviews.values()) + 1
            review = {
                "id": review_id,
                "user": {"login": VIEWER, "type": "User"},
                "body": body.get("body", ""),
                "state": {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
                    str(body.get("event")), "COMMENTED"
                ),
                "html_url": f"https://github.com/{REPO}/pull/{number}#pullrequestreview-{review_id}",
                "commit_id": body["commit_id"],
                "submitted_at": "2026-10-17T12:00:00Z",
            }
            state.posted_reviews.setdefault(number, []).append(review)
            return 200, review

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle("PATCH")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="the bare repository")
    parser.add_argument("--port-file", type=Path, required=True)
    args = parser.parse_args()
    server = Server(("127.0.0.1", 0), make_handler(State(args.repo)))
    port = server.server_address[1]
    # Written after the socket is listening, and renamed into place so a reader never sees
    # a half-written number.
    partial = args.port_file.with_suffix(".partial")
    partial.write_text(str(port))
    partial.rename(args.port_file)
    server.serve_forever()


if __name__ == "__main__":
    main()
