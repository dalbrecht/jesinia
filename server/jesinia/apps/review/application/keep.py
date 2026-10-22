# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kept review pages and "How it works" (spec §6.2, §8; plan Task 16).

Keeping is secondary work, so it is best effort and error-isolated: ``keep_finished`` never
raises, and a failure is logged with its cause and stored on the round (``kept``), where the
PWA shows it with Retry. Writes go through the KB service like Notes and RSS: a review page
is found again by the round's own record or by its source (the PR's URL), and rewritten
against its current revision, so keeping a round again updates its section and never
duplicates the page.

The repo's ``How it works`` page is written only when the author accepts a draft, and only
over the revision the draft was made against (an edit since is a ``SystemPageChanged``).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from jesinia.apps.review.application.jobs import agent_artifact
from jesinia.apps.review.domain.jobs import JobKind
from jesinia.apps.review.domain.kept import (
    KEPT,
    REBASE_KINDS,
    REVIEW_KIND,
    REVIEW_TAG,
    REVIEWS_FOLDER,
    SYSTEM_DRAFT,
    SYSTEM_KIND,
    SYSTEM_PAGE_TITLE,
    Authors,
    KeptComment,
    KeptRound,
    KeptState,
    NoSystemDraft,
    NotKeepable,
    Provenance,
    SystemDraft,
    SystemPageChanged,
    merge_round_section,
    repo_folder,
    review_front_matter,
    review_title,
    round_section,
    system_page_path,
    system_seed,
    system_source,
    system_update,
)
from jesinia.apps.review.domain.posting import Draft, NoReviewRound, Posting
from jesinia.apps.review.domain.queue import PrRef, RoundRecord, StoredPr
from jesinia.apps.review.domain.workspace import canonical_repo
from jesinia.services.kb.application.errors import NearDuplicate, StaleWrite
from jesinia.services.kb.application.service import KbService, PageDraft, PageWithRev
from jesinia.services.kb.application.tags import TagService
from jesinia.services.kb.domain.page import PageId
from jesinia.services.kb.domain.tags import is_valid_tag, normalise_tag

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# Rounds from before the agent record don't say which model ran; the marker says so.
UNKNOWN_MODEL = "unknown"


class KeepStore(Protocol):
    """What keeping reads from (and records in) ``review.sqlite``."""

    def round(self, round_id: int) -> RoundRecord | None: ...

    def latest_round(self, ref: PrRef) -> RoundRecord | None: ...

    def pr(self, ref: PrRef) -> StoredPr | None: ...

    def artifact(self, round_id: int, kind: str) -> object: ...

    def put_artifact(self, round_id: int, kind: str, data: object) -> None: ...

    def drafts(self, round_id: int) -> list[Draft]: ...

    def review_summary(self, round_id: int) -> str: ...

    def postings(self, round_id: int) -> list[Posting]: ...

    def job_runner(self) -> str: ...


def _cause(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _texts(items: object, key: str) -> list[str]:
    entries = items if isinstance(items, list) else []
    return [
        str(entry[key])
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get(key), str)
    ]


def _mappings(items: object) -> list[Mapping[str, object]]:
    return [
        item for item in (items if isinstance(items, list) else []) if isinstance(item, Mapping)
    ]


def _field(artifact: object, key: str) -> object:
    return artifact.get(key) if isinstance(artifact, Mapping) else None


class KeepService:
    """Keeps finished deep reviews as KB pages and drafts the repo's "How it works".

    Args:
        kb: the KB service, through which every page is read and written.
        tags: the shared tag catalog (``TagService.ensure``).
        store: ``review.sqlite``.
        clock: the current UTC time.
        folder: the KB folder new pages go under (``folders.review``), read per write so a
            config change applies without a restart.
    """

    def __init__(
        self,
        *,
        kb: KbService,
        tags: TagService,
        store: KeepStore,
        clock: Clock,
        folder: Callable[[], str] = lambda: REVIEWS_FOLDER,
    ) -> None:
        self._kb = kb
        self._folder = folder
        self._tags = tags
        self._store = store
        self._clock = clock
        # One write per page at a time (a review page per PR URL, a system page per repo).
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # --- keeping a review -------------------------------------------------------------------

    async def keep_finished(self, round_id: int) -> KeptState | None:
        """Keep the round's page now that it was posted or marked Done, and draft the repo's
        "How it works" seed or update. Never raises: a failure is logged with its cause and
        stored on the round. Returns ``None`` for a round that isn't kept (quick, a rebase,
        unfinished), else where the page went or why it couldn't be written."""
        try:
            rnd = self._store.round(round_id)
            if rnd is None or not self._keepable(rnd):
                return None
        except Exception as exc:  # secondary: the post or Done already happened
            log.warning("review.keep round=%d couldn't read the round: %s", round_id, _cause(exc))
            return None
        state = await self._keep_and_record(round_id)
        await self._draft_quietly(round_id)
        return state

    async def retry_keep(self, ref: PrRef, round_id: int | None) -> KeptState:
        """Keep the PR's round again (``round_id``, else its latest), and draft its "How it
        works" again unless one was accepted; the outcome is stored as ``keep_finished``'s.

        Raises:
            NoReviewRound: the PR has no such round.
            NotKeepable: the round isn't kept (quick, a rebase, unfinished).
        """
        rnd = self._round_of(ref, round_id)
        self._require_keepable(rnd)
        state = await self._keep_and_record(rnd.id)
        await self._draft_quietly(rnd.id)
        return state

    async def keep_review(self, round_id: int) -> PageWithRev:
        """Write (or update) the round's section of its PR's review page.

        Raises:
            NoReviewRound: no such round.
            NotKeepable: the round isn't kept (quick, a rebase, unfinished).
            KbError: the KB couldn't be written (``KbUnavailable``, ``StaleWrite`` twice).
        """
        rnd = self._round(round_id)
        self._require_keepable(rnd)
        kept = self._kept_round(rnd)
        self._kb.require_available()
        tags = await self._tags_for(kept.repo)
        section = round_section(kept, self._authors(rnd), tags)
        async with self._lock(kept.url):
            page_id = await self._review_page(round_id, kept)
            if page_id is None:
                draft = PageDraft(
                    None,
                    review_title(kept.number, kept.title),
                    repo_folder(kept.repo, self._folder()),
                    merge_round_section("", kept.round_id, section),
                    review_front_matter(kept),
                )
                return await self._kb.write(draft, for_user=True)
            try:
                return await self._rewrite(page_id, kept, section)
            except StaleWrite:  # edited between the read and the write: once more
                return await self._rewrite(page_id, kept, section)

    async def _keep_and_record(self, round_id: int) -> KeptState:
        try:
            page = await self.keep_review(round_id)
            state = KeptState("kept", page.page.id, str(page.page.path), None)
            log.info("review.keep round=%d kept %s", round_id, page.page.path)
        except Exception as exc:  # secondary: surfaced on the round with its cause
            log.warning(
                "review.keep round=%d couldn't keep the review page: %s",
                round_id, _cause(exc), exc_info=exc,
            )  # fmt: skip
            state = KeptState("failed", None, None, _cause(exc))
        self._record(round_id, KEPT, state.as_json())
        return state

    def _record(self, round_id: int, kind: str, data: object) -> None:
        try:
            self._store.put_artifact(round_id, kind, data)
        except Exception as exc:  # the page is written; only its note on the round is lost
            log.warning("review.keep round=%d couldn't record %s: %s", round_id, kind, _cause(exc))

    def _round(self, round_id: int) -> RoundRecord:
        rnd = self._store.round(round_id)
        if rnd is None:
            raise NoReviewRound(f"no review round {round_id}")
        return rnd

    def _round_of(self, ref: PrRef, round_id: int | None) -> RoundRecord:
        rnd = self._store.latest_round(ref) if round_id is None else self._store.round(round_id)
        if rnd is None or rnd.ref != ref:
            raise NoReviewRound(f"{ref} has no review round {round_id or ''}".rstrip())
        return rnd

    def _keepable(self, rnd: RoundRecord) -> bool:
        followup = _field(self._store.artifact(rnd.id, "followup"), "kind")
        return rnd.depth == "deep" and rnd.done and followup not in REBASE_KINDS

    def _require_keepable(self, rnd: RoundRecord) -> None:
        if not self._keepable(rnd):
            raise NotKeepable(
                f"round {rnd.id} isn't kept: only a deep review that was posted or marked "
                "Done is kept as a page (not a quick review, a rebase or one in progress)"
            )

    def _posted_event(self, rnd: RoundRecord) -> str | None:
        posted = [p for p in self._store.postings(rnd.id) if p.state == "posted"]
        return posted[-1].event.lower() if posted else None

    def _kept_round(self, rnd: RoundRecord) -> KeptRound:
        stored = self._store.pr(rnd.ref)
        url = f"https://github.com/{rnd.ref.repo}/pull/{rnd.ref.number}"
        artifact = self._store.artifact
        overview = _field(artifact(rnd.id, "overview"), "purpose")
        findings = [
            *_mappings(_field(artifact(rnd.id, "correctness"), "findings")),
            *_mappings(_field(artifact(rnd.id, "findings"), "findings")),
        ]
        paths = artifact(rnd.id, "paths")
        return KeptRound(
            round_id=rnd.id,
            repo=canonical_repo(rnd.ref.repo),
            number=rnd.ref.number,
            title=stored.pr.title if stored is not None else "",
            url=url,
            head_sha=rnd.head_sha,
            verdict=rnd.verdict,
            reviewed=rnd.finished_at or rnd.started_at,
            outcome=self._posted_event(rnd) or "done",
            review_url=rnd.posted_review_url,
            overview=overview if isinstance(overview, str) else "",
            steps=tuple(_texts(_field(artifact(rnd.id, "walkthrough"), "steps"), "title")),
            findings=tuple(findings),
            summary=self._store.review_summary(rnd.id),
            comments=tuple(KeptComment(d.path, d.line, d.body) for d in self._store.drafts(rnd.id)),
            paths=tuple(
                p for p in (paths if isinstance(paths, list) else []) if isinstance(p, str)
            ),
        )

    def _author(self, rnd: RoundRecord, *jobs: JobKind) -> Provenance:
        """Who wrote a part: the runner, model and session of the first of ``jobs`` that
        recorded them (``<job>_agent``). A round from before that record names the runner
        configured now, an unknown model and the round."""
        at = rnd.finished_at or rnd.started_at
        for job in jobs:
            record = self._store.artifact(rnd.id, agent_artifact(job))
            runner, session = _field(record, "runner"), _field(record, "session")
            if isinstance(runner, str) and isinstance(session, str):
                model = _field(record, "model")
                return Provenance(runner, model if isinstance(model, str) else "", session, at)
        return Provenance(self._store.job_runner(), UNKNOWN_MODEL, f"review-round-{rnd.id}", at)

    def _authors(self, rnd: RoundRecord) -> Authors:
        return Authors(
            overview=self._author(rnd, JobKind.OVERVIEW),
            walkthrough=self._author(rnd, JobKind.WALKTHROUGH),
            findings=self._author(rnd, JobKind.FINDINGS, JobKind.CORRECTNESS),
        )

    async def _tags_for(self, repo: str) -> list[str]:
        """``#code-review`` and the repo's name, by their catalog names (new ones are added;
        a near-duplicate takes the catalog's spelling). A tag the catalog can't take is left
        out, with its cause logged: tags never stop a page from being kept."""
        wanted = [REVIEW_TAG]
        name = normalise_tag(repo.split("/", 1)[1])
        if is_valid_tag(name):
            wanted.append(name)
        names: list[str] = []
        for tag in wanted:
            try:
                found = await self._tags.ensure([tag])
            except NearDuplicate as exc:
                found = exc.near[:1]
            except Exception as exc:  # the catalog is secondary to the page
                log.warning("review.keep left #%s out: %s", tag, _cause(exc))
                continue
            names.extend(n for n in found if n not in names)
        return names

    async def _review_page(self, round_id: int, kept: KeptRound) -> PageId | None:
        """The PR's review page: the one this round was kept in, else a review page whose
        source is the PR's URL. Only those pages are read (a page of another kind with the
        URL is never touched), so a first keep reads nothing."""
        recorded = KeptState.from_json(self._store.artifact(round_id, KEPT))
        candidates = [recorded.page_id] if recorded is not None and recorded.page_id else []
        candidates += self._kb.index.pages_by_source(kept.url)
        for candidate in dict.fromkeys(candidates):
            page_id = PageId(candidate)
            if self._kb.index.get(page_id) is None:
                continue
            meta = (await self._kb.read(page_id)).page.jesinia
            if meta.get("kind") == REVIEW_KIND and meta.get("source") == kept.url:
                return page_id
        return None

    async def _rewrite(self, page_id: PageId, kept: KeptRound, section: str) -> PageWithRev:
        current = await self._kb.read(page_id)
        page = current.page
        draft = PageDraft(
            page_id,
            page.title,  # the author may have renamed or moved it: it stays where it is
            page.path.parent,
            merge_round_section(page.body, kept.round_id, section),
            review_front_matter(kept, page.jesinia),
        )
        return await self._kb.write(draft, expected_rev=current.rev, for_user=True)

    # --- How it works -------------------------------------------------------------------------

    async def system_page(self, repo: str) -> PageWithRev | None:
        """The repo's How it works page, if it exists and the KB can be read: the
        ``review/system`` page whose source is this repo's (``system_source``), wherever it
        now lives, else ``Reviews/<owner>-<repo>/How it works.md``. Two repos can share that
        folder (``a-b/c`` and ``a/b-c``): a page that names another repo in ``jesinia.repo``
        isn't this repo's, and its seed (written under a free name) is found by its source
        (N3)."""
        if self._kb.files is None:
            return None
        canonical = canonical_repo(repo)
        for page_id in self._kb.index.pages_by_source(system_source(canonical)):
            found = await self._kb.read(page_id)
            meta = found.page.jesinia
            owner = meta.get("repo")
            if meta.get("kind") == SYSTEM_KIND and isinstance(owner, str):
                if owner.lower() == canonical:
                    return found
        row = self._kb.index.by_path(system_page_path(repo, self._folder()))
        if row is None:
            return None
        page = await self._kb.read(row.id)
        owner = page.page.jesinia.get("repo")
        if isinstance(owner, str) and owner.lower() != canonical:
            log.warning("review.keep %s is %s's How it works, not %s's", row.path, owner, repo)
            return None
        return page

    async def system_notes(self, repo: str) -> str:
        """The "How it works" body for a deep review's pack (``""`` when there is none). It
        is anyone's to edit, so the jobs fence it as untrusted.

        Raises:
            KbUnavailable: there is no KB to read (the round says so in a warning).
        """
        self._kb.require_available()
        page = await self.system_page(repo)
        return "" if page is None else page.page.body

    async def propose_system_update(self, round_id: int) -> str:
        """Draft the repo's "How it works": a seed when there is no page yet, or (once the
        round was posted as an approval) an update. The draft is stored with the round for
        the author to accept; nothing is written to the KB. Returns the draft's text, or
        ``""`` when there is nothing to draft, in which case a draft still on offer is
        withdrawn (a seed once someone made the page, say), so it stops offering Accept.

        Raises:
            NoReviewRound: no such round.
            KbError: the page couldn't be read.
        """
        rnd = self._round(round_id)
        kept = self._kept_round(rnd)
        self._kb.require_available()
        current = await self.system_page(kept.repo)
        text, draft = "", None
        if current is None:
            background = _mappings(_field(self._store.artifact(rnd.id, "background"), "sections"))
            author = self._author(rnd, JobKind.BACKGROUND, JobKind.OVERVIEW)
            text = system_seed(kept, background, author)
            draft = SystemDraft("seed", text, None, None, "proposed")
        elif kept.outcome == "approve":
            author = self._author(rnd, JobKind.OVERVIEW, JobKind.WALKTHROUGH)
            text = system_update(current.page.body, kept, author)
            draft = SystemDraft("update", text, current.rev, current.page.id, "proposed")
        if text and draft is not None:
            self._store.put_artifact(round_id, SYSTEM_DRAFT, draft.as_json())
            return text
        stored = SystemDraft.from_json(self._store.artifact(round_id, SYSTEM_DRAFT))
        if stored is not None and stored.state == "proposed":
            withdrawn = SystemDraft(stored.kind, "", None, None, "withdrawn")
            self._store.put_artifact(round_id, SYSTEM_DRAFT, withdrawn.as_json())
        return ""

    async def _draft_quietly(self, round_id: int) -> None:
        """``propose_system_update`` unless the round's draft was accepted; a failure is
        logged and stored as a failed draft, never raised."""
        try:
            stored = SystemDraft.from_json(self._store.artifact(round_id, SYSTEM_DRAFT))
            if stored is not None and stored.state == "accepted":
                return
            await self.propose_system_update(round_id)
        except Exception as exc:  # secondary: the review page doesn't depend on it
            log.warning(
                "review.keep round=%d couldn't draft How it works: %s",
                round_id, _cause(exc), exc_info=exc,
            )  # fmt: skip
            failed = SystemDraft("update", "", None, None, "failed", _cause(exc))
            self._record(round_id, SYSTEM_DRAFT, failed.as_json())

    async def accept_system_update(
        self, ref: PrRef, round_id: int, base_rev: str | None
    ) -> PageWithRev:
        """Write the round's drafted "How it works" (the author accepted it). ``base_rev``
        is the page revision of the draft the author was shown (``None`` for a seed). A
        replay of an accepted draft writes nothing. When the page changed since the draft
        was made, the draft is made again against the page as it is (or withdrawn, when
        there is nothing left to offer) before the refusal, so the author's next look shows
        a draft that can be accepted.

        Raises:
            NoReviewRound: the PR has no such round.
            KbUnavailable: there is no KB to write.
            NoSystemDraft: the round has no draft to accept.
            SystemPageChanged: the page changed since the draft was made, or the author was
                shown another draft; nothing was written.
            KbError: the KB couldn't be written.
        """
        rnd = self._round_of(ref, round_id)
        self._kb.require_available()
        repo = canonical_repo(rnd.ref.repo)
        async with self._lock(f"system:{repo}"):
            # Read under the lock: a concurrent accept of the same draft may have written it.
            draft = SystemDraft.from_json(self._store.artifact(rnd.id, SYSTEM_DRAFT))
            if draft is None or draft.state not in ("proposed", "accepted"):
                raise NoSystemDraft(f"round {rnd.id} has no drafted How it works to accept")
            current = await self.system_page(repo)
            if draft.state == "accepted" and current is not None:
                return current
            if base_rev != draft.base_rev:
                raise SystemPageChanged(
                    "this draft of How it works was replaced by a newer one; review it again"
                )
            try:
                page = await self._write_system(repo, draft, current)
            except SystemPageChanged:
                await self._draft_quietly(rnd.id)
                raise
            accepted = SystemDraft(draft.kind, draft.text, page.rev, page.page.id, "accepted")
            self._record(rnd.id, SYSTEM_DRAFT, accepted.as_json())
        return page

    async def _write_system(
        self, repo: str, draft: SystemDraft, current: PageWithRev | None
    ) -> PageWithRev:
        changed = SystemPageChanged(
            "How it works changed since this draft was made, so nothing was written and the "
            "edit is kept; the draft was made again against the page as it is now"
        )
        if draft.base_rev is None:  # a seed: there must still be no page
            if current is not None:
                raise changed
            return await self._kb.write(
                PageDraft(
                    None,
                    SYSTEM_PAGE_TITLE,
                    repo_folder(repo, self._folder()),
                    draft.text,
                    # The source finds the page again wherever it lands (N3).
                    {"kind": SYSTEM_KIND, "repo": repo, "source": system_source(repo)},
                ),
                for_user=True,
            )
        if current is None or current.rev != draft.base_rev:
            raise changed
        try:
            # Body only: the page's front matter stays byte-for-byte the author's.
            return await self._kb.update_body(
                current.page.id, draft.text, expected_rev=draft.base_rev, for_user=True
            )
        except StaleWrite as exc:
            raise changed from exc
