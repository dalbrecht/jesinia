# SPDX-License-Identifier: AGPL-3.0-or-later
"""``IssuesStore`` over the module's own ``issues.sqlite`` connection (autocommit, from
``open_db``). Times are stored as fixed-width ISO 8601 text in UTC, so they compare as text.
Issue and comment text is untrusted and is stored and returned verbatim, as data; every
query is parameterised."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast

from jesinia.apps.issues.application.ports import (
    CachedIssue,
    RepoState,
    SchemaSource,
    StoredIssue,
)
from jesinia.apps.issues.domain.changes import InvalidChangeError
from jesinia.apps.issues.domain.labels import (
    PLAN_TTL,
    CreateLabel,
    DeleteLabel,
    LabelOp,
    LabelPlan,
    parse_plan_kind,
)
from jesinia.apps.issues.domain.schema import (
    DEFAULT_SCHEMA,
    OVERLAY_DIMENSIONS,
    Canonical,
    LabelSchema,
    OverlayDimension,
    SchemaEntry,
    with_overlay,
)
from jesinia.apps.issues.domain.suggestions import HUMAN_NEEDED
from jesinia.apps.issues.domain.triage import (
    IssueFilters,
    IssueRow,
    PreviousTriage,
    SyncCursor,
    Triage,
    ViewName,
    content_hash,
    stale_before,
)
from jesinia.core.db import transaction
from jesinia.services.github.domain.issue import (
    Issue,
    IssueComment,
    IssueRef,
    Label,
    Milestone,
    TimelineEvent,
)

_SOURCES: tuple[SchemaSource, ...] = ("default", "repo", "settings")
_IN_INBOX = "(issues.inbox = 1 AND issues.complete = 0)"
# An open (draft or edited) suggestion of ``kind`` about the issue, for the saved filters.
_HAS_OPEN_SUGGESTION = (
    "EXISTS (SELECT 1 FROM suggestions WHERE suggestions.repo = issues.repo "
    "AND suggestions.n = issues.number AND suggestions.state IN ('draft', 'edited') "
    "AND suggestions.kind = ?{extra})"
)
_REPO_COLUMNS = (
    "repo, watched, added_at, schema_source, schema_json, migrated, cursor_json, synced_at, "
    "sync_error, settings_schema_json"
)


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _optional_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _ref_json(ref: IssueRef | None) -> dict[str, object] | None:
    return None if ref is None else {"repo": ref.repo, "number": ref.number, "id": ref.id}


def _ref(data: Mapping[str, object] | None) -> IssueRef | None:
    if data is None:
        return None
    ref_id = data["id"]
    return IssueRef(str(data["repo"]), cast(int, data["number"]), cast(int | None, ref_id))


def _issue_json(issue: Issue) -> str:
    return json.dumps(
        {
            "repo": issue.repo,
            "number": issue.number,
            "id": issue.id,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "labels": list(issue.labels),
            "milestone": issue.milestone,
            "parent": _ref_json(issue.parent),
            "created_at": _time(issue.created_at),
            "updated_at": _time(issue.updated_at),
            "url": issue.url,
            "author": issue.author,
            "linked_pr_state": issue.linked_pr_state,
            "sub_issue_count": issue.sub_issue_count,
        }
    )


def _issue(text: str) -> Issue:
    data = json.loads(text)
    return Issue(
        repo=data["repo"],
        number=data["number"],
        id=data["id"],
        title=data["title"],
        body=data["body"],
        state=data["state"],
        state_reason=data["state_reason"],
        labels=tuple(data["labels"]),
        milestone=data["milestone"],
        parent=_ref(data["parent"]),
        sub_issues=(),
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        url=data["url"],
        author=data["author"],
        linked_pr_state=data["linked_pr_state"],
        sub_issue_count=data["sub_issue_count"],
    )


def _triage(text: str) -> Triage:
    return Triage(**json.loads(text))


def _canonicals(items: list[dict[str, object]]) -> tuple[Canonical, ...]:
    return tuple(_canonical(item) for item in items)


def _canonical(item: dict[str, object]) -> Canonical:
    return Canonical(str(item["name"]), tuple(cast(list[str], item["aliases"])))


def _schema(text: str | None) -> LabelSchema:
    if text is None:
        return DEFAULT_SCHEMA
    data = json.loads(text)
    return LabelSchema(
        readiness=_canonicals(data["readiness"]),
        priority=_canonicals(data["priority"]),
        type=_canonicals(data["type"]),
        area_prefix=data["area_prefix"],
        areas=_canonicals(data["areas"]),
        free_prefixes=tuple(data["free_prefixes"]),
        epic=_canonical(data["epic"]),
        initiative=_canonical(data["initiative"]),
        errors=tuple(data["errors"]),
    )


def _schema_json(schema: LabelSchema) -> str:
    return json.dumps(asdict(schema))


def _op_json(op: LabelOp) -> dict[str, object]:
    if isinstance(op, CreateLabel):
        return {"op": "create", "name": op.name, "color": op.color, "description": op.description}
    return {
        "op": "delete",
        "name": op.name,
        "replacement": op.replacement,
        "members": list(op.members),
    }


def _op(data: dict[str, object]) -> LabelOp:
    if data["op"] == "create":
        return CreateLabel(str(data["name"]), str(data["color"]), str(data["description"]))
    replacement = data["replacement"]
    return DeleteLabel(
        str(data["name"]),
        None if replacement is None else str(replacement),
        tuple(cast(list[int], data["members"])),
    )


def _repo_state(row: tuple[object, ...]) -> RepoState:
    name, watched, added_at, source, schema_json, migrated, cursor_json, synced, error, own = row
    return RepoState(
        repo=str(name),
        watched=bool(watched),
        added_at=datetime.fromisoformat(str(added_at)),
        schema_source=_source(str(source)),
        schema=_schema(cast(str | None, schema_json)),
        migrated=bool(migrated),
        cursor=SyncCursor() if cursor_json is None else SyncCursor.from_json(str(cursor_json)),
        synced_at=_optional_time(cast(str | None, synced)),
        sync_error=cast(str | None, error),
        settings_schema=None if own is None else _schema(str(own)),
    )


def _overlay_dimension(value: object) -> OverlayDimension:
    """Raises: InvalidChangeError: a stored overlay row with an unknown dimension."""
    for dimension in OVERLAY_DIMENSIONS:
        if value == dimension:
            return dimension
    raise InvalidChangeError(f"schema_overlay holds an unknown dimension {value!r}")


def _source(value: str) -> SchemaSource:
    return next(source for source in _SOURCES if source == value)


def _comment_json(comment: IssueComment) -> str:
    return json.dumps(
        {
            "id": comment.id,
            "author": comment.author,
            "body": comment.body,
            "created_at": _time(comment.created_at),
            "url": comment.url,
        }
    )


def _comment(text: str) -> IssueComment:
    data = json.loads(text)
    return IssueComment(
        data["id"], data["author"], data["body"], datetime.fromisoformat(data["created_at"]),
        data["url"],
    )  # fmt: skip


def _event_json(event: TimelineEvent) -> str:
    return json.dumps(
        {
            "id": event.id,
            "event": event.event,
            "actor": event.actor,
            "created_at": None if event.created_at is None else _time(event.created_at),
            "label": event.label,
            "state_reason": event.state_reason,
            "source": _ref_json(event.source),
            "commit_id": event.commit_id,
        }
    )


def _event(text: str) -> TimelineEvent:
    data = json.loads(text)
    return TimelineEvent(
        id=data["id"],
        event=data["event"],
        actor=data["actor"],
        created_at=_optional_time(data["created_at"]),
        label=data["label"],
        state_reason=data["state_reason"],
        source=_ref(data["source"]),
        commit_id=data["commit_id"],
    )


def _row(issue: Issue, result: Triage) -> IssueRow:
    parent = issue.parent
    return IssueRow(
        repo=issue.repo,
        number=issue.number,
        title=issue.title,
        url=issue.url,
        author=issue.author,
        labels=list(issue.labels),
        readiness=result.readiness,
        priority=result.priority,
        type=result.type,
        areas=result.areas,
        complete=result.complete,
        missing=result.missing,
        conflicts=result.conflicts,
        is_epic=result.is_epic,
        sub_issue_count=issue.sub_issue_count,
        parent=None if parent is None else f"{parent.repo}#{parent.number}",
        milestone=issue.milestone,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


_UPSERT_ISSUE = """
INSERT INTO issues (repo, number, json, complete, missing_json, triage_json, is_epic, state,
                    created_at, updated_at, sub_issue_count, first_seen_at, inbox)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (repo, number) DO UPDATE SET
    json = excluded.json,
    complete = excluded.complete,
    missing_json = excluded.missing_json,
    triage_json = excluded.triage_json,
    is_epic = excluded.is_epic,
    state = excluded.state,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    sub_issue_count = excluded.sub_issue_count,
    inbox = excluded.inbox
WHERE excluded.updated_at >= issues.updated_at
"""


class SqliteIssuesStore:
    """``issues.sqlite``: watched repos, the issue cache and its triage state."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # Repos ------------------------------------------------------------------------------

    def watch(self, repo: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO repos (repo, watched, added_at) VALUES (?, 1, ?) "
            "ON CONFLICT (repo) DO UPDATE SET watched = 1",
            (repo, _time(now)),
        )

    def unwatch(self, repo: str) -> None:
        self._conn.execute("UPDATE repos SET watched = 0 WHERE repo = ?", (repo,))

    def watched_repos(self) -> list[str]:
        rows = self._conn.execute("SELECT repo FROM repos WHERE watched = 1 ORDER BY repo")
        return [repo for (repo,) in rows]

    def repo(self, repo: str) -> RepoState | None:
        row = self._conn.execute(
            f"SELECT {_REPO_COLUMNS} FROM repos WHERE repo = ?", (repo,)
        ).fetchone()
        return None if row is None else self._overlaid(_repo_state(row))

    def all_repos(self) -> list[RepoState]:
        rows = self._conn.execute(f"SELECT {_REPO_COLUMNS} FROM repos ORDER BY repo")
        return [self._overlaid(_repo_state(row)) for row in rows]

    def _overlaid(self, state: RepoState) -> RepoState:
        """``state`` with its approved schema entries merged over the stored schema, as every
        read sees it (a labels.yml re-read by sync can't drop them)."""
        rows = self._conn.execute(
            "SELECT dimension, canonical, aliases_json FROM schema_overlay WHERE repo = ? "
            "ORDER BY approved_at, rowid",
            (state.repo,),
        )
        entries = tuple(
            SchemaEntry(_overlay_dimension(dimension), str(canonical), tuple(json.loads(aliases)))
            for dimension, canonical, aliases in rows
        )
        if not entries:
            return state
        return replace(state, schema=with_overlay(state.schema, entries), overlay=entries)

    def add_overlay(self, repo: str, entry: SchemaEntry, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO schema_overlay (repo, dimension, canonical, aliases_json, approved_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (repo, dimension, canonical) DO NOTHING",
            (repo, entry.dimension, entry.canonical, json.dumps(list(entry.aliases)), _time(now)),
        )

    def set_schema(self, repo: str, source: SchemaSource, schema: LabelSchema) -> None:
        self._conn.execute(
            "UPDATE repos SET schema_source = ?, schema_json = ? WHERE repo = ?",
            (source, _schema_json(schema), repo),
        )

    def set_settings_schema(self, repo: str, schema: LabelSchema | None) -> None:
        self._conn.execute(
            "UPDATE repos SET settings_schema_json = ? WHERE repo = ?",
            (None if schema is None else _schema_json(schema), repo),
        )

    def set_migrated(self, repo: str) -> None:
        self._conn.execute("UPDATE repos SET migrated = 1 WHERE repo = ?", (repo,))

    def record_sync(self, repo: str, synced_at: datetime | None, error: str | None) -> None:
        if synced_at is None:
            self._conn.execute("UPDATE repos SET sync_error = ? WHERE repo = ?", (error, repo))
        else:
            self._conn.execute(
                "UPDATE repos SET synced_at = ?, sync_error = ? WHERE repo = ?",
                (_time(synced_at), error, repo),
            )

    # Labels and milestones --------------------------------------------------------------

    def replace_labels(self, repo: str, labels: Sequence[Label]) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM labels WHERE repo = ?", (repo,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO labels (repo, name, json) VALUES (?, ?, ?)",
                [(repo, label.name, json.dumps(asdict(label))) for label in labels],
            )

    def labels(self, repo: str) -> list[Label]:
        rows = self._conn.execute("SELECT json FROM labels WHERE repo = ? ORDER BY name", (repo,))
        return [Label(**json.loads(text)) for (text,) in rows]

    def replace_milestones(self, repo: str, milestones: Sequence[Milestone]) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM milestones WHERE repo = ?", (repo,))
            self._conn.executemany(
                "INSERT INTO milestones (repo, number, json) VALUES (?, ?, ?)",
                [
                    (
                        repo,
                        m.number,
                        json.dumps(
                            {
                                "number": m.number,
                                "title": m.title,
                                "state": m.state,
                                "due_on": None if m.due_on is None else _time(m.due_on),
                            }
                        ),
                    )
                    for m in milestones
                ],
            )

    def milestones(self, repo: str) -> list[Milestone]:
        rows = self._conn.execute(
            "SELECT json FROM milestones WHERE repo = ? ORDER BY number", (repo,)
        )
        found: list[Milestone] = []
        for (text,) in rows:
            data = json.loads(text)
            found.append(
                Milestone(
                    data["number"], data["title"], data["state"], _optional_time(data["due_on"])
                )
            )
        return found

    # Issues -----------------------------------------------------------------------------

    def previous_triage(self, repo: str, numbers: Sequence[int]) -> dict[int, PreviousTriage]:
        if not numbers:
            return {}
        marks = ", ".join("?" for _ in numbers)
        rows = self._conn.execute(
            "SELECT number, complete, inbox, state, json FROM issues "
            f"WHERE repo = ? AND number IN ({marks})",
            (repo, *numbers),
        )
        found: dict[int, PreviousTriage] = {}
        for number, complete, inbox, state, text in rows:
            cached = _issue(text)
            found[number] = PreviousTriage(
                bool(complete),
                bool(inbox),
                is_open=state == "open",
                content=content_hash(cached.title, cached.body, cached.labels),
            )
        return found

    def is_unchanged(self, repo: str, issue: Issue) -> bool:
        row = self._conn.execute(
            "SELECT json FROM issues WHERE repo = ? AND number = ?", (repo, issue.number)
        ).fetchone()
        return row is not None and row[0] == _issue_json(issue)

    def save_page(
        self,
        repo: str,
        issues: Sequence[StoredIssue],
        links: Mapping[int, Sequence[Issue]],
        cursor: SyncCursor,
        now: datetime,
    ) -> None:
        with transaction(self._conn):
            for stored in issues:
                issue, result = stored.issue, stored.triage
                self._conn.execute(
                    _UPSERT_ISSUE,
                    (
                        repo,
                        issue.number,
                        _issue_json(issue),
                        int(result.complete),
                        json.dumps(result.missing),
                        json.dumps(asdict(result)),
                        int(result.is_epic),
                        issue.state,
                        _time(issue.created_at),
                        _time(issue.updated_at),
                        issue.sub_issue_count,
                        _time(now),
                        int(stored.inbox),
                    ),
                )
            for parent, children in links.items():
                self._put_links(repo, parent, children, now)
            # An issue GitHub reports with no sub-issues has none: links cached while it was an
            # epic go (its children fetched-as-empty, so the item shows none).
            for stored in issues:
                issue = stored.issue
                if issue.sub_issue_count == 0:
                    self._clear_links(repo, issue.number, now)
            self._conn.execute(
                "UPDATE repos SET cursor_json = ? WHERE repo = ?", (cursor.to_json(), repo)
            )

    def issues(self, repo: str) -> list[Issue]:
        rows = self._conn.execute("SELECT json FROM issues WHERE repo = ? ORDER BY number", (repo,))
        return [_issue(text) for (text,) in rows]

    def issue(self, repo: str, n: int) -> Issue | None:
        row = self._conn.execute(
            "SELECT json FROM issues WHERE repo = ? AND number = ?", (repo, n)
        ).fetchone()
        return None if row is None else _issue(row[0])

    def update_issue_labels(self, issue: Issue, labels: Sequence[str], triaged: Triage) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE issues SET json = ? WHERE repo = ? AND number = ?",
                (_issue_json(replace(issue, labels=tuple(labels))), issue.repo, issue.number),
            )
            self._write_triage(issue.repo, [(issue.number, triaged)])

    def refresh_issue(self, issue: Issue, triaged: Triage) -> None:
        with transaction(self._conn):
            updated = self._conn.execute(
                "UPDATE issues SET json = ?, state = ?, updated_at = ?, sub_issue_count = ? "
                "WHERE repo = ? AND number = ? AND updated_at <= ?",
                (
                    _issue_json(issue),
                    issue.state,
                    _time(issue.updated_at),
                    issue.sub_issue_count,
                    issue.repo,
                    issue.number,
                    _time(issue.updated_at),
                ),
            )
            if updated.rowcount:  # a newer row (a sync since) keeps its own triage
                self._write_triage(issue.repo, [(issue.number, triaged)])

    def update_triage(self, repo: str, triaged: Sequence[tuple[int, Triage]]) -> None:
        with transaction(self._conn):
            self._write_triage(repo, triaged)

    def _write_triage(self, repo: str, triaged: Sequence[tuple[int, Triage]]) -> None:
        self._conn.executemany(
            "UPDATE issues SET complete = ?, missing_json = ?, triage_json = ?, is_epic = ?, "
            "inbox = CASE WHEN ? THEN 0 ELSE inbox END WHERE repo = ? AND number = ?",
            [
                (
                    int(result.complete),
                    json.dumps(result.missing),
                    json.dumps(asdict(result)),
                    int(result.is_epic),
                    int(result.complete),
                    repo,
                    number,
                )
                for number, result in triaged
            ],
        )

    # Sub-issues, comments and timeline --------------------------------------------------

    def _put_links(self, repo: str, parent: int, children: Sequence[Issue], now: datetime) -> None:
        self._conn.execute(
            "DELETE FROM sub_issue_links WHERE repo = ? AND parent_number = ?", (repo, parent)
        )
        self._conn.executemany(
            "INSERT INTO sub_issue_links "
            "(repo, parent_number, position, child_repo, child_number, child_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (repo, parent, position, child.repo, child.number, child.id)
                for position, child in enumerate(children)
            ],
        )
        self._conn.execute(
            "UPDATE issues SET sub_issues_synced_at = ? WHERE repo = ? AND number = ?",
            (_time(now), repo, parent),
        )

    def _clear_links(self, repo: str, parent: int, now: datetime) -> None:
        cleared = self._conn.execute(
            "DELETE FROM sub_issue_links WHERE repo = ? AND parent_number = ?", (repo, parent)
        )
        if cleared.rowcount:
            self._conn.execute(
                "UPDATE issues SET sub_issues_synced_at = ? WHERE repo = ? AND number = ?",
                (_time(now), repo, parent),
            )

    def set_sub_issue_links(
        self, repo: str, parent: int, children: Sequence[Issue], now: datetime
    ) -> None:
        with transaction(self._conn):
            self._put_links(repo, parent, children, now)

    def sub_issue_links(self, repo: str, parent: int) -> list[IssueRef] | None:
        """The epic's children in order; ``None`` while they were never fetched."""
        fetched = self._conn.execute(
            "SELECT sub_issues_synced_at FROM issues WHERE repo = ? AND number = ?",
            (repo, parent),
        ).fetchone()
        if fetched is None or fetched[0] is None:
            return None
        rows = self._conn.execute(
            "SELECT child_repo, child_number, child_id FROM sub_issue_links "
            "WHERE repo = ? AND parent_number = ? ORDER BY position",
            (repo, parent),
        )
        return [IssueRef(child_repo, number, child_id) for child_repo, number, child_id in rows]

    def epics_without_links(self, repo: str, limit: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT number FROM issues WHERE repo = ? AND sub_issue_count > 0 "
            "AND sub_issues_synced_at IS NULL ORDER BY updated_at DESC, number LIMIT ?",
            (repo, limit),
        )
        return [number for (number,) in rows]

    def replace_details(
        self,
        repo: str,
        n: int,
        comments: Sequence[IssueComment],
        events: Sequence[TimelineEvent],
        now: datetime,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM comments WHERE repo = ? AND number = ?", (repo, n))
            self._conn.execute("DELETE FROM timeline WHERE repo = ? AND number = ?", (repo, n))
            self._conn.executemany(
                "INSERT OR REPLACE INTO comments (repo, number, comment_id, json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(repo, n, c.id, _comment_json(c), _time(c.created_at)) for c in comments],
            )
            self._conn.executemany(
                "INSERT INTO timeline (repo, number, position, json) VALUES (?, ?, ?, ?)",
                [(repo, n, position, _event_json(e)) for position, e in enumerate(events)],
            )
            self._conn.execute(
                "UPDATE issues SET details_synced_at = ? WHERE repo = ? AND number = ?",
                (_time(now), repo, n),
            )

    def comments(self, repo: str, n: int) -> list[IssueComment]:
        rows = self._conn.execute(
            "SELECT json FROM comments WHERE repo = ? AND number = ? "
            "ORDER BY created_at, comment_id",
            (repo, n),
        )
        return [_comment(text) for (text,) in rows]

    def timeline(self, repo: str, n: int) -> list[TimelineEvent]:
        rows = self._conn.execute(
            "SELECT json FROM timeline WHERE repo = ? AND number = ? ORDER BY position", (repo, n)
        )
        return [_event(text) for (text,) in rows]

    def cached_issue(self, repo: str, n: int) -> CachedIssue | None:
        row = self._conn.execute(
            "SELECT json, triage_json, inbox, details_synced_at FROM issues "
            "WHERE repo = ? AND number = ?",
            (repo, n),
        ).fetchone()
        if row is None:
            return None
        text, result, inbox, details = row
        return CachedIssue(_issue(text), _triage(result), bool(inbox), _optional_time(details))

    # Label plans ------------------------------------------------------------------------

    def save_plan(self, plan: LabelPlan, now: datetime) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "DELETE FROM label_plans WHERE created_at < ?", (_time(now - PLAN_TTL),)
            )
            self._conn.execute(
                "INSERT INTO label_plans (id, repo, kind, json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    plan.id,
                    plan.repo,
                    plan.kind,
                    json.dumps([_op_json(op) for op in plan.ops]),
                    _time(plan.created_at),
                ),
            )

    def take_plan(self, plan_id: str, repo: str) -> LabelPlan | None:
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT repo, kind, json, created_at FROM label_plans WHERE id = ? AND repo = ?",
                (plan_id, repo),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM label_plans WHERE id = ?", (plan_id,))
        stored_repo, kind, ops, created_at = row
        return LabelPlan(
            plan_id,
            stored_repo,
            parse_plan_kind(kind),
            tuple(_op(op) for op in json.loads(ops)),
            datetime.fromisoformat(created_at),
        )

    # Views ------------------------------------------------------------------------------

    def search(self, query: str, repo: str | None, limit: int) -> list[IssueRow]:
        # LIKE with its wildcards escaped: the query is matched literally.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where = ["repos.watched = 1"]
        params: list[object] = []
        if repo is not None:
            where.append("issues.repo = ?")
            params.append(repo)
        number = query.strip().removeprefix("#")
        text_match = " OR ".join(
            f"json_extract(issues.json, '$.{field}') LIKE ? ESCAPE '\\'"
            for field in ("title", "body", "labels")
        )
        params.extend([pattern] * 3)
        if number.isdigit():
            text_match += " OR issues.number = ?"
            params.append(int(number))
        query_sql = (
            "SELECT issues.json, issues.triage_json FROM issues "
            "JOIN repos ON repos.repo = issues.repo "
            f"WHERE {' AND '.join(where)} AND ({text_match}) "
            "ORDER BY issues.updated_at DESC, issues.repo, issues.number LIMIT ?"
        )
        params.append(limit)
        return [
            _row(_issue(text), _triage(result))
            for text, result in self._conn.execute(query_sql, params)
        ]

    def rows(self, view: ViewName, filters: IssueFilters, now: datetime) -> list[IssueRow]:
        # Mirrors domain/triage.py ``in_view``.
        where = [
            "repos.watched = 1",
            "issues.state = 'open'",
            _IN_INBOX if view == "inbox" else f"NOT {_IN_INBOX}",
        ]
        params: list[object] = []
        if filters.repo is not None:
            where.append("issues.repo = ?")
            params.append(filters.repo)
        if filters.saved == "incomplete":
            where.append("issues.complete = 0")
        elif filters.saved == "stale_90d":
            where.append("issues.updated_at < ?")
            params.append(_time(stale_before(now)))
        elif filters.saved == "human_needed_could_be_ready":
            where.append("json_extract(issues.triage_json, '$.readiness') = ?")
            where.append(
                _HAS_OPEN_SUGGESTION.format(
                    extra=" AND json_extract(suggestions.payload_json, '$.ready') = 1"
                )
            )
            params.extend([HUMAN_NEEDED, "readiness_gap"])
        elif filters.saved == "possible_duplicates":
            where.append(_HAS_OPEN_SUGGESTION.format(extra=""))
            params.append("duplicate")
        query = (
            "SELECT issues.json, issues.triage_json FROM issues "
            "JOIN repos ON repos.repo = issues.repo "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY issues.updated_at DESC, issues.repo, issues.number"
        )
        return [
            _row(_issue(text), _triage(result))
            for text, result in self._conn.execute(query, params)
        ]
