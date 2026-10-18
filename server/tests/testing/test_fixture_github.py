# SPDX-License-Identifier: AGPL-3.0-or-later
"""The e2e fixture GitHub (``web/tests/e2e/fixtures/github/server.py``, plan Task 14): the issue,
label and sub-issue fixtures the Issues e2e drives, and the request record it asserts on."""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

SERVER = Path(__file__).resolve().parents[3] / "web/tests/e2e/fixtures/github/server.py"
REPO = "/repos/e2e-dev/greeter"
AUTH = {"Authorization": "Bearer e2e-token"}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fixture_github", SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def github(tmp_path: Path) -> Iterator[httpx.Client]:
    fixture = _load()
    server = fixture.Server(("127.0.0.1", 0), fixture.make_handler(fixture.State(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=AUTH) as client:
        yield client
    server.shutdown()
    server.server_close()


def _open(github: httpx.Client, title: str, **extra: Any) -> dict[str, Any]:
    made = github.post("/__issues", json={"title": title, "labels": ["bug"], **extra})
    assert made.status_code == 201
    return dict(made.json())


def test_sub_issues_are_added_moved_listed_and_removed(github: httpx.Client) -> None:
    epic = _open(github, "Epic", labels=["epic"])
    child = _open(github, "Child", parent=epic["number"])
    other = _open(github, "Other")
    assert github.get(f"{REPO}/issues/{child['number']}/parent").json()["number"] == epic["number"]
    assert github.get(f"{REPO}/issues/{other['number']}/parent").status_code == 404
    listed = github.get(f"{REPO}/issues/{child['number']}").json()
    assert listed["parent_issue_url"].endswith(f"/issues/{epic['number']}")

    added = github.post(
        f"{REPO}/issues/{epic['number']}/sub_issues", json={"sub_issue_id": other["id"]}
    )
    assert added.status_code == 201
    children = github.get(f"{REPO}/issues/{epic['number']}/sub_issues").json()
    assert [c["number"] for c in children] == [child["number"], other["number"]]
    assert github.get(f"{REPO}/issues/{epic['number']}").json()["sub_issues_summary"]["total"] == 2

    moved = github.patch(
        f"{REPO}/issues/{epic['number']}/sub_issues/priority",
        json={"sub_issue_id": other["id"], "before_id": child["id"]},
    )
    assert moved.status_code == 200
    children = github.get(f"{REPO}/issues/{epic['number']}/sub_issues").json()
    assert [c["number"] for c in children] == [other["number"], child["number"]]
    after = github.patch(
        f"{REPO}/issues/{epic['number']}/sub_issues/priority",
        json={"sub_issue_id": other["id"], "after_id": child["id"]},
    )
    assert after.status_code == 200
    bad = github.patch(
        f"{REPO}/issues/{epic['number']}/sub_issues/priority",
        json={"sub_issue_id": epic["id"], "after_id": child["id"]},
    )
    assert bad.status_code == 422

    removed = github.request(
        "DELETE", f"{REPO}/issues/{epic['number']}/sub_issue", json={"sub_issue_id": child["id"]}
    )
    assert removed.status_code == 200
    gone = github.request(
        "DELETE", f"{REPO}/issues/{epic['number']}/sub_issue", json={"sub_issue_id": child["id"]}
    )
    assert gone.status_code == 404
    assert "parent_issue_url" not in github.get(f"{REPO}/issues/{child['number']}").json()


def test_a_child_with_a_parent_moves_only_with_replace_parent(github: httpx.Client) -> None:
    first, second = _open(github, "First"), _open(github, "Second")
    child = _open(github, "Child", parent=first["number"])
    path = f"{REPO}/issues/{second['number']}/sub_issues"
    refused = github.post(path, json={"sub_issue_id": child["id"]})
    assert refused.status_code == 422
    unknown = github.post(path, json={"sub_issue_id": 1})
    assert unknown.status_code == 404
    moved = github.post(path, json={"sub_issue_id": child["id"], "replace_parent": True})
    assert moved.status_code == 201
    assert github.get(f"{REPO}/issues/{first['number']}/sub_issues").json() == []
    parent = github.get(f"{REPO}/issues/{child['number']}/parent").json()
    assert parent["number"] == second["number"]


def test_issues_close_and_take_comments(github: httpx.Client) -> None:
    epic = _open(github, "Epic")
    issue = _open(github, "Task", parent=epic["number"])
    path = f"{REPO}/issues/{issue['number']}"
    closed = github.patch(path, json={"state": "closed", "state_reason": "completed"})
    assert closed.status_code == 200 and closed.json()["state"] == "closed"
    summary = github.get(f"{REPO}/issues/{epic['number']}").json()["sub_issues_summary"]
    assert summary["completed"] == 1 and summary["percent_completed"] == 100
    edited = github.patch(f"{REPO}/issues/{epic['number']}", json={"title": "ignored"})
    assert edited.json()["state"] == "open"
    posted = github.post(f"{path}/comments", json={"body": "Done."})
    assert posted.status_code == 201 and posted.json()["html_url"].endswith(
        f"#issuecomment-{posted.json()['id']}"
    )
    assert [c["body"] for c in github.get(f"{path}/comments").json()] == ["Done."]
    assert github.get(f"{path}/timeline").json() == []
    # Open issues by default, every state on request, least recently updated first.
    assert [i["number"] for i in github.get(f"{REPO}/issues").json()] == [epic["number"]]
    listed = github.get(f"{REPO}/issues", params={"state": "all"}).json()
    assert sorted(i["number"] for i in listed) == [epic["number"], issue["number"]]
    assert github.get(f"{REPO}/issues", params={"state": "all", "page": "2"}).json() == []
    assert github.get(f"{path}/nothing").status_code == 404


def test_labels_are_created_deleted_and_never_made_implicitly(github: httpx.Client) -> None:
    issue = _open(github, "Task")
    labels = f"{REPO}/issues/{issue['number']}/labels"
    assert github.post(labels, json={"labels": ["epic"]}).status_code == 422
    assert github.post(f"{REPO}/issues", json={"title": "x", "labels": ["epic"]}).status_code == 422

    made = github.post(f"{REPO}/labels", json={"name": "epic", "color": "5319e7"})
    assert made.status_code == 201 and made.json()["color"] == "5319e7"
    assert github.post(f"{REPO}/labels", json={"name": "epic"}).status_code == 422
    assert github.get(f"{REPO}/labels/epic").status_code == 200
    assert "epic" in [lab["name"] for lab in github.get(f"{REPO}/labels").json()]
    assert github.post(labels, json={"labels": ["epic"]}).status_code == 200
    created = github.post(f"{REPO}/issues", json={"title": "New", "labels": ["epic"]})
    assert created.status_code == 201 and created.json()["labels"] == [{"name": "epic"}]

    assert github.delete(f"{REPO}/labels/epic").status_code == 204
    assert github.delete(f"{REPO}/labels/epic").status_code == 404
    assert github.get(f"{REPO}/labels/epic").status_code == 404
    assert github.get(f"{REPO}/issues/{issue['number']}").json()["labels"] == [{"name": "bug"}]


def test_every_api_request_is_recorded_for_the_e2e(github: httpx.Client) -> None:
    issue = _open(github, "Task")
    github.patch(f"{REPO}/issues/{issue['number']}", json={"state": "closed"})
    recorded = github.get("/__requests").json()
    assert recorded == [
        {
            "method": "PATCH",
            "path": f"{REPO}/issues/{issue['number']}",
            "query": "",
            "body": {"state": "closed"},
        }
    ]
