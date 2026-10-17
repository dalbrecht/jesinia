# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake GitHub API for the Review e2e (stdlib only; started by ``make e2e-server``).

Serves the recorded-shape JSON beside this file for one repository, ``e2e-dev/greeter``
(two open PRs), and records every request so a test can ask what reached "GitHub" at
``GET /__requests``. It binds 127.0.0.1 on an ephemeral port and writes the port to
``--port-file`` once it is listening; nothing else is reachable and no real credential is
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
REPO = "e2e-dev/greeter"
VIEWER = "e2e-dev"
PLACEHOLDER = re.compile(r"\{\{sha:([^}]+)\}\}")
PULL = re.compile(rf"^/repos/{REPO}/pulls/(\d+)(?:/(files|commits|reviews|comments))?$")
COMMIT = re.compile(rf"^/repos/{REPO}/commits/([0-9a-f]{{40}})/(check-runs|status)$")


class State:
    """What the server serves and has seen; guarded by one lock."""

    def __init__(self, repo: Path) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.repo = repo
        self.posted_reviews: dict[int, list[dict[str, Any]]] = {}

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
            data = json.dumps(body).encode()
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
            if method != "GET" and not path.endswith("/reviews"):
                return missing
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
                name = f"pull-{number}-check-runs" if commit.group(2) == "check-runs" else "status-empty"
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
