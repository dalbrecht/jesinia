# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from jesinia.core.db import DbClass, ModuleDb, open_db
from jesinia.services.github.application.prs import (
    INBOX_CONCURRENCY,
    SEARCH_MAX_PAGES,
    GitHubPayloadError,
    PrClient,
)
from jesinia.services.github.application.service import (
    GitHubRateLimitedError,
    GitHubRequestError,
    GitHubService,
    GitHubUnavailable,
)
from jesinia.services.github.domain.pr import (
    DraftComment,
    InvalidPullRequestReferenceError,
    PrCommit,
    PrFile,
    PullRequest,
    ReviewThread,
)
from jesinia.services.github.infrastructure.cache import SqliteGitHubCache

API = "https://api.github.com"
FIXTURES = Path(__file__).parent / "fixtures"
DB = ModuleDb("github", DbClass.CACHE, package_files("jesinia.services.github.migrations"))
SHA = "a" * 40


def fixture(name: str) -> object:
    loaded: object = json.loads((FIXTURES / f"{name}.json").read_text())
    return loaded


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[SqliteGitHubCache]:
    conn = open_db(tmp_path, DB)
    yield SqliteGitHubCache(conn)
    conn.close()


@pytest.fixture
def github(cache: SqliteGitHubCache) -> GitHubService:
    async def token() -> str | None:
        return "gho_test"

    return GitHubService(token, cache)


@pytest.fixture
def prs(github: GitHubService) -> PrClient:
    return PrClient(github)


def _mock_search(qualifier: str, fixture_name: str) -> respx.Route:
    return respx.get(
        f"{API}/search/issues",
        params__contains={"q": f"is:pr is:open {qualifier} repo:acme/api repo:acme/web"},
    ).mock(return_value=Response(200, json=fixture(fixture_name)))


def _mock_pulls() -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    for repo, number in (("acme/api", 7), ("acme/web", 8), ("acme/api", 9)):
        name = f"pull_{repo.split('/')[1]}_{number}"
        respx.get(f"{API}/repos/{repo}/pulls/{number}").mock(
            return_value=Response(200, json=fixture(name))
        )


@respx.mock
async def test_inbox_merges_requested_authored_and_reviewed(prs: PrClient) -> None:
    requested = _mock_search("review-requested:@me", "search_review_requested")
    _mock_search("author:@me", "search_authored")
    _mock_search("reviewed-by:@me", "search_reviewed")
    _mock_pulls()

    inbox = await prs.list_review_inbox(["acme/api", "acme/web"])

    assert [(pr.repo, pr.number) for pr in inbox] == [
        ("acme/api", 7),
        ("acme/web", 8),
        ("acme/api", 9),
    ]
    assert [pr.review_requested for pr in inbox] == [True, False, False]
    query = requested.calls[0].request.url.params["q"]
    assert query == "is:pr is:open review-requested:@me repo:acme/api repo:acme/web"


async def test_inbox_of_no_repos_is_empty(prs: PrClient) -> None:
    assert await prs.list_review_inbox([]) == []


@respx.mock
async def test_inbox_splits_long_repo_lists_across_searches(prs: PrClient) -> None:
    empty = {"total_count": 0, "incomplete_results": False, "items": []}
    route = respx.get(f"{API}/search/issues").mock(return_value=Response(200, json=empty))
    repos = [f"some-organisation/repository-number-{i:02d}" for i in range(12)]

    assert await prs.list_review_inbox(repos) == []

    queries = [call.request.url.params["q"] for call in route.calls]
    assert len(queries) > 3 and all(len(q) <= 256 for q in queries)
    searched = {term for q in queries for term in q.split() if term.startswith("repo:")}
    assert searched == {f"repo:{repo}" for repo in repos}


@respx.mock
async def test_get_maps_pull_request_fields(prs: PrClient) -> None:
    _mock_pulls()

    pr = await prs.get("acme/web", 8)

    assert pr == PullRequest(
        repo="acme/web",
        number=8,
        title="Fix the header",
        body="Adds things.",
        author="me",
        author_is_bot=False,
        base_ref="main",
        head_ref="feat/x8",
        head_sha="8".rjust(40, "a"),
        base_sha="8".rjust(40, "b"),
        head_repo="me/web-fork",
        state="open",
        draft=False,
        additions=48,
        deletions=3,
        changed_files=2,
        url="https://github.com/acme/web/pull/8",
        review_requested=False,
        mergeable_state="clean",
        created_at=datetime(2026, 10, 14, 9, 30, tzinfo=UTC),
        commit_count=2,
    )
    seven = await prs.get("acme/api", 7)
    assert seven.review_requested is True


@respx.mock
async def test_get_reports_merged_state_and_deleted_head_repo(prs: PrClient) -> None:
    data = fixture("pull_api_9")
    assert isinstance(data, dict)
    data["merged_at"] = "2026-10-15T11:00:00Z"
    data["state"] = "closed"
    data["mergeable_state"] = None
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    respx.get(f"{API}/repos/acme/api/pulls/9").mock(return_value=Response(200, json=data))

    pr = await prs.get("acme/api", 9)

    assert (pr.state, pr.head_repo, pr.body, pr.mergeable_state) == ("merged", None, "", None)


@respx.mock
async def test_bot_author_detected(prs: PrClient) -> None:
    _mock_pulls()
    respx.get(f"{API}/repos/acme/api/pulls/7/reviews").mock(
        return_value=Response(200, json=fixture("reviews"))
    )
    respx.get(f"{API}/repos/acme/api/pulls/7/comments").mock(
        return_value=Response(200, json=fixture("review_comments"))
    )

    bot_pr = await prs.get("acme/api", 9)
    human_pr = await prs.get("acme/api", 7)
    reviews = await prs.reviews("acme/api", 7)

    assert bot_pr.author == "dependabot[bot]" and bot_pr.author_is_bot is True
    assert human_pr.author_is_bot is False
    assert [(r.author, r.author_is_bot) for r in reviews] == [
        ("copilot-pull-request-reviewer[bot]", True),
        ("bob", False),
        ("ghost", False),
    ]


@respx.mock
async def test_reviews_carry_state_commit_and_comment_counts(prs: PrClient) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/7/reviews").mock(
        return_value=Response(200, json=fixture("reviews"))
    )
    respx.get(f"{API}/repos/acme/api/pulls/7/comments").mock(
        return_value=Response(200, json=fixture("review_comments"))
    )

    reviews = await prs.reviews("acme/api", 7)

    assert [(r.state, r.commit_sha, r.comment_count) for r in reviews] == [
        ("COMMENTED", SHA, 2),
        ("APPROVED", "c" * 40, 1),
        ("PENDING", "c" * 40, 0),
    ]
    assert reviews[0].submitted_at == datetime(2026, 10, 15, 10, 10, tzinfo=UTC)
    assert reviews[2].submitted_at is None


@pytest.mark.parametrize(
    ("runs", "statuses", "expected"),
    [
        ("check_runs_mixed", "status_empty", "failing"),
        ("check_runs_pending", "status_empty", "pending"),
        ("check_runs_passing", "status_empty", "passing"),
        ("check_runs_empty", "status_empty", "none"),
        ("check_runs_passing", "status_pending", "pending"),
        ("check_runs_passing", "status_mixed", "failing"),
        ("check_runs_empty", "status_pending", "pending"),
    ],
)
@respx.mock
async def test_checks_rollup_maps_states(
    prs: PrClient, runs: str, statuses: str, expected: str
) -> None:
    respx.get(f"{API}/repos/acme/api/commits/{SHA}/check-runs").mock(
        return_value=Response(200, json=fixture(runs))
    )
    respx.get(f"{API}/repos/acme/api/commits/{SHA}/status").mock(
        return_value=Response(200, json=fixture(statuses))
    )

    state = await prs.checks("acme/api", SHA)

    assert state.state == expected


@respx.mock
async def test_checks_list_each_run_and_status(prs: PrClient) -> None:
    respx.get(f"{API}/repos/acme/api/commits/{SHA}/check-runs").mock(
        return_value=Response(200, json=fixture("check_runs_mixed"))
    )
    respx.get(f"{API}/repos/acme/api/commits/{SHA}/status").mock(
        return_value=Response(200, json=fixture("status_mixed"))
    )

    state = await prs.checks("acme/api", SHA)

    assert [(c.name, c.status) for c in state.checks] == [
        ("lint", "passing"),
        ("unit", "failing"),
        ("e2e", "pending"),
        ("docs", "passing"),
        ("ci/legacy", "passing"),
        ("ci/deploy", "failing"),
        ("ci/other", "failing"),
    ]
    assert state.checks[0].url == "https://github.com/acme/api/runs/lint"
    assert state.checks[4].url == "https://ci.example.com/ci/legacy"


@respx.mock
async def test_files_follow_pages(prs: PrClient) -> None:
    page_one = [
        {"filename": f"f{i}.py", "status": "added", "additions": 1, "deletions": 0, "patch": "+x"}
        for i in range(100)
    ]
    route = respx.get(f"{API}/repos/acme/api/pulls/7/files").mock(
        side_effect=[Response(200, json=page_one), Response(200, json=fixture("files"))]
    )

    found = await prs.files("acme/api", 7)

    assert len(found) == 102 and [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
    assert found[100] == PrFile(
        filename="src/app.py",
        status="modified",
        additions=30,
        deletions=2,
        patch="@@ -1,2 +1,3 @@\n import os\n+import sys\n",
        previous_filename=None,
    )
    assert found[101].previous_filename == "src/old_name.py" and found[101].patch is None


@respx.mock
async def test_commits_use_the_login_or_the_git_author_name(prs: PrClient) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/7/commits").mock(
        return_value=Response(200, json=fixture("commits"))
    )

    commits = await prs.commits("acme/api", 7)

    assert commits[0] == PrCommit(
        sha="d" * 40,
        message="feat: add limit\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        author="alice",
        authored_at=datetime(2026, 10, 14, 9, 0, tzinfo=UTC),
    )
    assert commits[1].author == "Unlinked Person"


@respx.mock
async def test_threads_follow_the_graphql_cursor(prs: PrClient) -> None:
    route = respx.post(f"{API}/graphql").mock(
        side_effect=[
            Response(200, json=fixture("threads_page_1")),
            Response(200, json=fixture("threads_page_2")),
        ]
    )

    threads = await prs.threads("acme/api", 7)

    assert threads == [
        ReviewThread(
            path="src/app.py",
            line=12,
            body="Is this bounded?",
            author="bob",
            resolved=False,
            commit_sha=SHA,
        ),
        ReviewThread(
            path="src/old.py",
            line=4,
            body="Outdated nit",
            author="ghost",
            resolved=True,
            commit_sha="c" * 40,
        ),
    ]
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first["variables"] == {"owner": "acme", "name": "api", "number": 7, "after": None}
    assert second["variables"]["after"] == "Y3Vyc29yOjE="


@respx.mock
async def test_viewer_is_the_token_login(prs: PrClient) -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    assert await prs.viewer() == "me"


@respx.mock
async def test_read_failure_carries_github_message(prs: PrClient) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/404/files").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(GitHubRequestError, match="Not Found") as caught:
        await prs.files("acme/api", 404)
    assert caught.value.status == 404


@respx.mock
async def test_unexpected_payload_shape_is_a_typed_error(prs: PrClient) -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=[]))
    with pytest.raises(GitHubPayloadError, match="object"):
        await prs.viewer()
    respx.get(f"{API}/user").mock(return_value=Response(200, json={"login": 3}))
    with pytest.raises(GitHubPayloadError, match="login"):
        await prs.viewer()
    respx.get(f"{API}/repos/acme/api/pulls/7/commits").mock(
        return_value=Response(200, json={"not": "a list"})
    )
    with pytest.raises(GitHubPayloadError, match="list"):
        await prs.commits("acme/api", 7)
    respx.get(f"{API}/repos/acme/api/pulls/7/files").mock(
        return_value=Response(200, json=[{"filename": "a", "status": "added", "additions": "1"}])
    )
    with pytest.raises(GitHubPayloadError, match="additions"):
        await prs.files("acme/api", 7)
    bad_pull = fixture("pull_api_7")
    assert isinstance(bad_pull, dict)
    bad_pull["draft"] = "no"
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    respx.get(f"{API}/repos/acme/api/pulls/7").mock(return_value=Response(200, json=bad_pull))
    with pytest.raises(GitHubPayloadError, match="draft"):
        await prs.get("acme/api", 7)
    empty_thread = fixture("threads_page_2")
    assert isinstance(empty_thread, dict)
    connection = empty_thread["data"]["repository"]["pullRequest"]["reviewThreads"]
    connection["nodes"][0]["comments"]["nodes"] = []
    respx.post(f"{API}/graphql").mock(return_value=Response(200, json=empty_thread))
    with pytest.raises(GitHubPayloadError, match="first comment"):
        await prs.threads("acme/api", 7)


async def test_reads_reject_malformed_references(prs: PrClient) -> None:
    with pytest.raises(InvalidPullRequestReferenceError):
        await prs.get("../secrets", 1)
    with pytest.raises(InvalidPullRequestReferenceError):
        await prs.checks("acme/api", "main")
    with pytest.raises(InvalidPullRequestReferenceError):
        await prs.list_review_inbox(["acme/api", "nope"])


def _review_route(response: Response) -> respx.Route:
    return respx.post(f"{API}/repos/acme/api/pulls/7/reviews").mock(return_value=response)


@respx.mock
async def test_create_review_posts_one_atomic_review(github: GitHubService) -> None:
    route = _review_route(Response(200, json=fixture("create_review_ok")))
    comments = [
        DraftComment(path="src/app.py", line=12, side="RIGHT", body="BLOCKING: unbounded"),
        DraftComment(
            path="src/app.py",
            line=20,
            side="RIGHT",
            body="Extract this.",
            start_line=15,
            start_side="RIGHT",
        ),
    ]

    url = await github.create_review(
        "acme/api",
        7,
        commit_sha=SHA,
        event="REQUEST_CHANGES",
        body="Two notes.",
        comments=comments,
    )
    again = await github.create_review(
        "acme/api", 7, commit_sha=SHA, event="COMMENT", body="Two notes.", comments=comments
    )

    assert url == again == "https://github.com/acme/api/pull/7#pullrequestreview-80"
    assert route.call_count == 2
    request = route.calls[0].request
    assert "If-None-Match" not in request.headers
    assert request.headers["Authorization"] == "Bearer gho_test"
    assert json.loads(request.content) == {
        "commit_id": SHA,
        "event": "REQUEST_CHANGES",
        "body": "Two notes.",
        "comments": [
            {"path": "src/app.py", "line": 12, "side": "RIGHT", "body": "BLOCKING: unbounded"},
            {
                "path": "src/app.py",
                "line": 20,
                "side": "RIGHT",
                "body": "Extract this.",
                "start_line": 15,
                "start_side": "RIGHT",
            },
        ],
    }


@respx.mock
async def test_create_review_surfaces_github_error_cause(github: GitHubService) -> None:
    _review_route(Response(422, json=fixture("create_review_422")))
    with pytest.raises(GitHubRequestError) as caught:
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="APPROVE", body="", comments=[]
        )
    assert caught.value.status == 422
    assert "Pull request review thread line must be part of the diff" in str(caught.value)
    assert "Unprocessable Entity" in caught.value.github_message

    _review_route(
        Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"resource": "PullRequestReview", "code": "custom", "field": "event"}],
            },
        )
    )
    with pytest.raises(GitHubRequestError, match="Validation Failed.*custom"):
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="APPROVE", body="", comments=[]
        )

    _review_route(Response(502, text="Bad gateway"))
    with pytest.raises(GitHubRequestError, match="Bad gateway"):
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="APPROVE", body="", comments=[]
        )

    _review_route(Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubUnavailable):
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="APPROVE", body="", comments=[]
        )


@respx.mock
async def test_create_review_keeps_the_transport_error_as_cause(github: GitHubService) -> None:
    lost = httpx.ReadTimeout("read timed out")
    respx.post(f"{API}/repos/acme/api/pulls/7/reviews").mock(side_effect=lost)
    with pytest.raises(GitHubRequestError, match="read timed out") as caught:
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="COMMENT", body="x", comments=[]
        )
    assert caught.value.status is None and caught.value.__cause__ is lost


@respx.mock
async def test_create_review_without_a_review_url_is_an_error(github: GitHubService) -> None:
    _review_route(Response(200, json={"id": 1}))
    with pytest.raises(GitHubRequestError, match="html_url"):
        await github.create_review(
            "acme/api", 7, commit_sha=SHA, event="COMMENT", body="x", comments=[]
        )


async def test_create_review_rejects_malformed_references(github: GitHubService) -> None:
    with pytest.raises(InvalidPullRequestReferenceError):
        await github.create_review(
            "acme/api", 7, commit_sha="HEAD", event="COMMENT", body="x", comments=[]
        )
    with pytest.raises(InvalidPullRequestReferenceError):
        await github.create_review(
            "acme", 7, commit_sha=SHA, event="COMMENT", body="x", comments=[]
        )


@respx.mock
async def test_graphql_errors_are_typed(github: GitHubService) -> None:
    respx.post(f"{API}/graphql").mock(
        return_value=Response(200, json={"errors": [{"message": "Could not resolve"}]})
    )
    with pytest.raises(GitHubRequestError, match="Could not resolve"):
        await github.graphql("{ x }", {})


def _status(context: str, state: str) -> dict[str, object]:
    return {"context": context, "state": state, "target_url": None}


@respx.mock
async def test_checks_page_commit_statuses_so_a_late_failure_counts(prs: PrClient) -> None:
    respx.get(f"{API}/repos/acme/api/commits/{SHA}/check-runs").mock(
        return_value=Response(200, json=fixture("check_runs_passing"))
    )
    first = {"state": "failure", "statuses": [_status(f"ci/{i}", "success") for i in range(100)]}
    second = {"state": "failure", "statuses": [_status("ci/late", "failure")]}
    route = respx.get(f"{API}/repos/acme/api/commits/{SHA}/status").mock(
        side_effect=[Response(200, json=first), Response(200, json=second)]
    )

    state = await prs.checks("acme/api", SHA)

    assert state.state == "failing" and len(state.checks) == 103
    assert state.checks[-1].name == "ci/late"
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
    assert route.calls[0].request.url.params["per_page"] == "100"


@respx.mock
async def test_inbox_keeps_a_pr_that_fails_to_load_with_its_cause(prs: PrClient) -> None:
    _mock_search("review-requested:@me", "search_review_requested")
    _mock_search("author:@me", "search_authored")
    _mock_search("reviewed-by:@me", "search_reviewed")
    _mock_pulls()
    respx.get(f"{API}/repos/acme/web/pulls/8").mock(
        return_value=Response(403, json={"message": "Resource not accessible by integration"})
    )

    inbox = await prs.list_review_inbox(["acme/api", "acme/web"])

    assert [(pr.repo, pr.number) for pr in inbox] == [
        ("acme/api", 7),
        ("acme/web", 8),
        ("acme/api", 9),
    ]
    assert inbox[0].fetch_error is None and inbox[2].fetch_error is None
    broken = inbox[1]
    assert broken.fetch_error is not None
    assert "403" in broken.fetch_error and "not accessible" in broken.fetch_error
    assert (broken.title, broken.author, broken.url) == (
        "Fix the header",
        "me",
        "https://github.com/acme/web/pull/8",
    )
    assert broken.head_sha == "" and broken.review_requested is False


@respx.mock
async def test_inbox_bounds_concurrent_pull_fetches(prs: PrClient) -> None:
    items = [
        {
            "repository_url": f"{API}/repos/acme/api",
            "number": n,
            "title": f"PR {n}",
            "html_url": f"https://github.com/acme/api/pull/{n}",
            "user": {"login": "alice", "type": "User"},
            "state": "open",
            "created_at": "2026-10-14T09:30:00Z",
        }
        for n in range(1, 31)
    ]
    respx.get(f"{API}/search/issues").mock(
        return_value=Response(200, json={"total_count": 30, "items": items})
    )
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    in_flight = 0
    peak = 0
    template = fixture("pull_api_7")
    assert isinstance(template, dict)

    async def slow_pull(request: httpx.Request) -> Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        number = int(request.url.path.rsplit("/", 1)[1])
        return Response(200, json={**template, "number": number})

    respx.get(url__regex=rf"{API}/repos/acme/api/pulls/\d+$").mock(side_effect=slow_pull)

    inbox = await prs.list_review_inbox(["acme/api"])

    assert len(inbox) == 30 and 1 < peak <= INBOX_CONCURRENCY


@respx.mock
async def test_inbox_search_stops_at_githubs_thousand_result_cap(prs: PrClient) -> None:
    item = fixture("search_review_requested")
    assert isinstance(item, dict)
    full_page = {"total_count": 5000, "items": item["items"] * 100}
    route = respx.get(f"{API}/search/issues").mock(return_value=Response(200, json=full_page))
    _mock_pulls()

    inbox = await prs.list_review_inbox(["acme/api"])

    assert [(pr.repo, pr.number) for pr in inbox] == [("acme/api", 7)]
    pages = [int(c.request.url.params["page"]) for c in route.calls]
    assert max(pages) == SEARCH_MAX_PAGES == 10


@respx.mock
async def test_pr_repo_is_githubs_canonical_spelling(prs: PrClient) -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    respx.get(f"{API}/repos/ACME/Web/pulls/8").mock(
        return_value=Response(200, json=fixture("pull_web_8"))
    )
    pr = await prs.get("ACME/Web", 8)
    assert pr.repo == "acme/web"


@pytest.mark.parametrize(
    "url",
    [f"{API}/orgs/acme", f"{API}/repos/acme", f"{API}/repos/../../user", f"{API}/repos/a/b/c"],
)
@respx.mock
async def test_search_hits_with_a_bad_repository_url_are_typed_errors(
    prs: PrClient, url: str
) -> None:
    item = {
        "repository_url": url,
        "number": 1,
        "title": "t",
        "html_url": "https://github.com/acme/api/pull/1",
        "user": {"login": "alice", "type": "User"},
        "state": "open",
        "created_at": "2026-10-14T09:30:00Z",
    }
    respx.get(f"{API}/search/issues").mock(
        return_value=Response(200, json={"total_count": 1, "items": [item]})
    )
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    with pytest.raises(GitHubPayloadError, match="repository_url"):
        await prs.list_review_inbox(["acme/api"])


def _commit_items(count: int) -> list[dict[str, object]]:
    template = fixture("commits")
    assert isinstance(template, list)
    return [{**template[0], "sha": f"{n:040x}"} for n in range(count)]


def _mock_commit_pages(repo: str, number: int, fetched: int) -> None:
    items = _commit_items(fetched)
    pages = [items[i : i + 100] for i in range(0, max(fetched, 1), 100)]

    def page(request: httpx.Request) -> Response:
        index = int(request.url.params.get("page", "1")) - 1
        return Response(200, json=pages[index] if index < len(pages) else [])

    respx.get(f"{API}/repos/{repo}/pulls/{number}/commits").mock(side_effect=page)


@pytest.mark.parametrize(("in_pr", "fetched", "truncated"), [(251, 250, True), (250, 250, False)])
@respx.mock
async def test_commit_lists_capped_by_github_are_flagged(
    prs: PrClient, caplog: pytest.LogCaptureFixture, in_pr: int, fetched: int, truncated: bool
) -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    payload = fixture("pull_web_8")
    assert isinstance(payload, dict)
    respx.get(f"{API}/repos/acme/web/pulls/8").mock(
        return_value=Response(200, json={**payload, "commits": in_pr})
    )
    _mock_commit_pages("acme/web", 8, fetched)
    pr = await prs.get("acme/web", 8)
    assert pr.commit_count == in_pr and pr.commits_truncated is False

    flagged, commits = await prs.commits_of(pr)

    assert len(commits) == fetched
    assert flagged.commits_truncated is truncated
    assert ("commit list truncated" in caplog.text) is truncated


def _mock_file_pages(repo: str, number: int, fetched: int) -> None:
    items = [
        {"filename": f"f{n}.py", "status": "added", "additions": 1, "deletions": 0}
        for n in range(fetched)
    ]

    def page(request: httpx.Request) -> Response:
        start = (int(request.url.params.get("page", "1")) - 1) * 100
        return Response(200, json=items[start : start + 100])

    respx.get(f"{API}/repos/{repo}/pulls/{number}/files").mock(side_effect=page)


@pytest.mark.parametrize(
    ("in_pr", "fetched", "truncated"), [(3001, 3000, True), (3000, 3000, False)]
)
@respx.mock
async def test_file_lists_capped_by_github_are_flagged(
    prs: PrClient, caplog: pytest.LogCaptureFixture, in_pr: int, fetched: int, truncated: bool
) -> None:
    respx.get(f"{API}/user").mock(return_value=Response(200, json=fixture("user")))
    payload = fixture("pull_web_8")
    assert isinstance(payload, dict)
    respx.get(f"{API}/repos/acme/web/pulls/8").mock(
        return_value=Response(200, json={**payload, "changed_files": in_pr})
    )
    _mock_file_pages("acme/web", 8, fetched)
    pr = await prs.get("acme/web", 8)
    assert pr.files_truncated is False

    flagged, files = await prs.files_of(pr)

    assert len(files) == fetched
    assert flagged.files_truncated is truncated
    assert ("file list truncated" in caplog.text) is truncated


@respx.mock
async def test_inbox_reraises_a_rate_limit_instead_of_degrading_the_pr(prs: PrClient) -> None:
    _mock_search("review-requested:@me", "search_review_requested")
    _mock_search("author:@me", "search_authored")
    _mock_search("reviewed-by:@me", "search_reviewed")
    _mock_pulls()
    respx.get(f"{API}/repos/acme/web/pulls/8").mock(
        return_value=Response(
            429, headers={"retry-after": "60"}, json={"message": "secondary rate limit"}
        )
    )

    with pytest.raises(GitHubRateLimitedError) as caught:
        await prs.list_review_inbox(["acme/api", "acme/web"])
    assert caught.value.reset_at is not None


def _items(name: str) -> list[dict[str, object]]:
    data = fixture(name)
    assert isinstance(data, dict) and isinstance(data["items"], list)
    return [item for item in data["items"] if isinstance(item, dict)]


@respx.mock
async def test_inbox_repos_finds_requested_and_authored_prs_anywhere(prs: PrClient) -> None:
    requested = respx.get(
        f"{API}/search/issues",
        params__contains={"q": "is:pr is:open review-requested:@me archived:false"},
    ).mock(return_value=Response(200, json=fixture("search_review_requested")))
    shouty = {**_items("search_reviewed")[0], "repository_url": f"{API}/repos/ACME/Api"}
    authored = respx.get(
        f"{API}/search/issues", params__contains={"q": "is:pr is:open author:@me archived:false"}
    ).mock(
        return_value=Response(
            200, json={"total_count": 2, "items": [*_items("search_authored"), shouty]}
        )
    )

    assert await prs.inbox_repos() == ["acme/api", "acme/web"]
    assert requested.called and authored.called


@respx.mock
async def test_viewer_orgs_follow_pages_and_lowercase(prs: PrClient) -> None:
    first = [{"login": f"Org{i}"} for i in range(100)]
    route = respx.get(f"{API}/user/orgs").mock(
        side_effect=[Response(200, json=first), Response(200, json=[{"login": "Acme"}])]
    )

    orgs = await prs.viewer_orgs()

    assert len(orgs) == 101 and orgs[0] == "org0" and orgs[-1] == "acme"
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]
