# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layered review standards (spec §5.2): base, repo and user layers, rule IDs as heading
paths, the language filter, and a loader that reads the repo's files only from the PR's
base commit in the mirror (never the PR head, never the filesystem)."""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Iterator
from importlib.resources import files
from pathlib import Path

import pytest

from jesinia.apps.review.domain.standards import (
    MAX_CONFIG_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_RULE_DEPTH,
    MAX_SECTIONS,
    REPO_REVIEW_FILE,
    REPO_STANDARDS_CONFIG,
    Document,
    InvalidOverrideError,
    InvalidStandardsConfigError,
    Override,
    Section,
    Standards,
    StandardsDocumentTooLargeError,
    StandardsUnreadableError,
    TooManyStandardsSectionsError,
    applies,
    check_config_nesting,
    expand_languages,
    layer_standards,
    parse_languages,
    split_sections,
)
from jesinia.apps.review.domain.workspace import (
    GitCommandError,
    InvalidTreePathError,
    MirrorFileTooLargeError,
)
from jesinia.apps.review.infrastructure.standards_loader import StandardsLoader
from jesinia.apps.review.infrastructure.store import SqliteReviewStore
from jesinia.apps.review.infrastructure.workspace import GitWorkspace
from jesinia.core.db import DbClass, ModuleDb, open_db
from tests.apps.review.perf import assert_linear

REPO = "acme/widget"
BASE = "a" * 40
PY = "lang-01_python_standards.md"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


# --- layering and rule IDs ------------------------------------------------------------------


def _ids(standards: Standards) -> list[str]:
    return [Standards.rule_id(section) for section in standards.sections]


def test_layering_later_wins_and_rule_ids_are_heading_paths() -> None:
    base = Document(
        PY,
        "base",
        "# Python\nintro\n## Security\nsecurity rules\n### P1\nold p1\n### P2\nold p2\n"
        "## Style\nbase style\n",
    )
    repo = Document(
        REPO_REVIEW_FILE,
        "repo",
        f"# {PY} › Python › Security › P1\nrepo p1\n# Tests\nAlways add a regression test.\n",
    )
    user = Document("mine.md", "user", f"# {PY} › Python › Style\nuser style\n")
    stored = Override(PY, ("Python", "Security", "P1"), "stored p1")

    # Layers apply in layer order whatever order the documents arrive in; stored overrides last.
    standards = layer_standards([user, repo, base], [stored])

    assert _ids(standards) == [
        f"{PY} › Python",
        f"{PY} › Python › Security",
        f"{PY} › Python › Security › P1",
        f"{PY} › Python › Security › P2",
        f"{PY} › Python › Style",
        f"{REPO_REVIEW_FILE} › Tests",
    ]
    by_id = {Standards.rule_id(s): s for s in standards.sections}
    p1 = by_id[f"{PY} › Python › Security › P1"]
    assert (p1.layer, p1.text) == ("user", "stored p1")  # base < repo < user < stored
    assert by_id[f"{PY} › Python › Security › P2"].layer == "base"
    style = by_id[f"{PY} › Python › Style"]
    assert (style.layer, style.text) == ("user", "user style")
    assert by_id[f"{REPO_REVIEW_FILE} › Tests"].layer == "repo"
    assert p1 == Section(PY, ("Python", "Security", "P1"), "user", "stored p1")

    prompt = standards.as_prompt()
    assert f"## {PY} › Python › Security › P1 [user]\n\nstored p1" in prompt
    assert "old p1" not in prompt and "repo p1" not in prompt
    assert prompt.index(f"{PY} › Python ›") < prompt.index(f"{REPO_REVIEW_FILE} › Tests")


def test_overriding_a_heading_replaces_its_whole_subtree() -> None:
    base = Document(PY, "base", "# Security\n## P1\none\n## P2\ntwo\n# Style\nstyle\n")
    repo = Document(REPO_REVIEW_FILE, "repo", f"# {PY} › Security\nOnly OWASP top ten.\n")
    standards = layer_standards([base, repo], [])
    assert _ids(standards) == [f"{PY} › Security", f"{PY} › Style"]
    assert standards.sections[0].text == "Only OWASP top ten."


def test_an_empty_override_removes_a_rule_and_headings_alone_are_not_rules() -> None:
    base = Document(PY, "base", "# Security\n## P1\none\n## P2\ntwo\n")
    standards = layer_standards([base], [Override(PY, ("Security", "P1"), "  ")])
    assert _ids(standards) == [f"{PY} › Security › P2"]  # "Security" has no text of its own


def test_nested_headings_under_a_targeted_heading_extend_its_path() -> None:
    doc = Document(REPO_REVIEW_FILE, "repo", f"# {PY} › Security\n## P3\nnew rule\n")
    (section,) = layer_standards([doc], []).sections
    assert Standards.rule_id(section) == f"{PY} › Security › P3"


def test_split_sections_keeps_a_preamble_and_ignores_headings_in_code_fences() -> None:
    doc = Document(
        "doc.md",
        "base",
        "Read this first.\n# A\n```md\n# not a heading\n```\n~~~\n## nor this\n~~~\n"
        "#### B ####\nb\n#hashtag is text\n",
    )
    sections = split_sections(doc)
    assert [(s.heading_path, s.text) for s in sections] == [
        ((), "Read this first."),
        (("A",), "```md\n# not a heading\n```\n~~~\n## nor this\n~~~"),
        (("A", "B"), "b\n#hashtag is text"),
    ]
    assert Standards.rule_id(sections[0]) == "doc.md"


def test_a_heading_that_only_looks_like_a_rule_id_is_an_ordinary_heading() -> None:
    doc = Document("doc.md", "base", "# › Oops\nx\n# a ›  › b\ny\n")
    assert [s.heading_path for s in split_sections(doc)] == [("› Oops",), ("a ›  › b",)]


def test_overrides_are_validated() -> None:
    with pytest.raises(InvalidOverrideError):
        Override("", ("A",), "x")
    with pytest.raises(InvalidOverrideError):
        Override(PY, ("A", " "), "x")


def _layering_documents(rules: int) -> list[Document]:
    documents = [
        Document(f"doc{d}.md", "base", "".join(f"# h{i}\nrule {i}\n" for i in range(rules)))
        for d in range(10)
    ]
    # Each user document replaces every other rule of one base document.
    users = [
        Document(
            f"mine{d}.md",
            "user",
            "".join(f"# doc{d}.md › h{i}\nnew {i}\n" for i in range(0, rules, 2)),
        )
        for d in range(10)
    ]
    return [*documents, *users]


def test_layering_is_linear_in_the_number_of_rules() -> None:
    def work(rules: int) -> Callable[[], object]:
        documents = _layering_documents(rules)
        return lambda: layer_standards(documents, [])

    assert_linear(work, 500)  # 5,000 rules, then 20,000
    standards = layer_standards(_layering_documents(2_000), [])
    assert len(standards.sections) == 20_000
    assert standards.sections[0].text == "new 0" and standards.sections[1].text == "rule 1"


def test_a_document_with_too_many_rules_is_refused() -> None:
    doc = Document("big.md", "repo", "".join(f"# h{i}\nx\n" for i in range(MAX_SECTIONS + 1)))
    with pytest.raises(TooManyStandardsSectionsError, match="big.md"):
        split_sections(doc)


def test_rule_ids_are_capped_in_depth() -> None:
    deep = " › ".join(f"h{i}" for i in range(MAX_RULE_DEPTH + 1))
    (section,) = split_sections(Document("d.md", "repo", f"# PY.md › {deep}\nx\n"))
    assert section.doc == "d.md"  # too deep to be a rule ID: an ordinary heading
    with pytest.raises(InvalidOverrideError, match="deep"):
        Override("d.md", tuple(f"h{i}" for i in range(MAX_RULE_DEPTH + 1)), "x")


def test_standards_over_the_budget_drop_the_base_layer_first_and_say_so() -> None:
    base = Document(PY, "base", "".join(f"# b{i}\n{'x' * 900}\n" for i in range(20)))
    repo = Document(REPO_REVIEW_FILE, "repo", f"# r\nrepo rule {'r' * 400}\n")
    user = Document("mine.md", "user", "# u\nuser rule\n")
    standards = layer_standards([base, repo, user], [])
    full = standards.as_prompt()
    assert standards.as_prompt(limit=len(full)) == full
    trimmed = standards.as_prompt(limit=8_000)
    assert len(trimmed) <= 8_000
    assert "repo rule" in trimmed and "user rule" in trimmed
    assert "rules from the base layer were left out" in trimmed
    assert f"## {PY} › b0 [base]" in trimmed  # base rules stay in order while they fit
    assert f"## {PY} › b19 [base]" not in trimmed
    tiny = standards.as_prompt(limit=800)
    assert "user rule" in tiny and "repo rule" not in tiny and len(tiny) <= 800
    assert "rules from the repo layer were left out (1 of them)" in tiny


def test_config_nesting_ignores_quoted_text_and_comments() -> None:
    check_config_nesting('a: "x \\" [[[[" # [[[[\nb: \'[[[[\'\nc: [d] # [[[[\n# {{{{{\n' + "[" * 32)
    with pytest.raises(InvalidStandardsConfigError, match="32"):
        check_config_nesting("[" * 33)


def test_no_standards_says_so() -> None:
    assert "No review standards" in layer_standards([], []).as_prompt()


# --- the language filter ----------------------------------------------------------------------


def test_expand_languages_and_applies() -> None:
    assert expand_languages(["Python", "jvm", "rails"]) == frozenset(
        {"python", "java", "kotlin", "ruby", "ruby_on_rails"}
    )
    python = expand_languages(["python"])
    assert applies("lang-01_python_standards.md", python)
    assert not applies("lang-06_typescript_standards.md", python)
    assert applies("sec-01_security_standards.md", python)  # common documents always apply
    assert applies("core-standards.md", frozenset())


def test_parse_languages_validates_the_config() -> None:
    assert parse_languages({"version": 1, "languages": ["python", "typescript"]}) == (
        "python",
        "typescript",
    )
    assert parse_languages({"version": 1}) == ()
    assert parse_languages(None) == ()  # an empty file
    for bad in ("languages: python", {"languages": "python"}, {"languages": [1]}):
        with pytest.raises(InvalidStandardsConfigError):
            parse_languages(bad)


class FakeMirror:
    """``MirrorFiles`` over a dict of ``(sha, path) -> bytes``."""

    def __init__(self, blobs: dict[tuple[str, str], bytes]) -> None:
        self.blobs = blobs
        self.calls: list[tuple[str, str, str, int]] = []
        self.error: Exception | None = None

    async def show_file(self, repo: str, sha: str, path: str, *, limit: int) -> bytes | None:
        self.calls.append((repo, sha, path, limit))
        if self.error is not None:
            raise self.error
        return self.blobs.get((sha, path))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def standards_dir(tmp_path: Path) -> Path:
    root = tmp_path / "coding-standards"
    docs = root / "standards"
    _write(docs / "languages" / PY, "# Python\nUse ruff.\n")
    _write(docs / "languages" / "lang-06_typescript_standards.md", "# TypeScript\nNo any.\n")
    _write(docs / "languages" / "lang-10_ruby_standards.md", "# Ruby\nrubocop.\n")
    _write(docs / "languages" / "lang-11_ruby_on_rails_standards.md", "# Rails\nstrong params\n")
    _write(docs / "security" / "sec-01_security_standards.md", "# Security\nNo eval.\n")
    _write(docs / "shared" / "core-standards.md", "# Core\nSOLID.\n")
    _write(docs / "shared" / "standards-yml-schema.md", "# Schema\nnot a standard\n")
    _write(docs / "README.md", "# Readme\nnot a standard\n")
    _write(docs / "agents" / "claude-code" / "base.md", "# Agent\nnot a standard\n")
    return root


def _docs(standards: Standards) -> set[str]:
    return {section.doc for section in standards.sections}


async def test_languages_filter_documents(standards_dir: Path) -> None:
    mirror = FakeMirror({(BASE, REPO_STANDARDS_CONFIG): b"version: 1\nlanguages: [python]\n"})
    loader = StandardsLoader(mirror, lambda: standards_dir)
    loaded = await loader.load(REPO, BASE, None, [])
    assert _docs(loaded) == {PY, "sec-01_security_standards.md", "core-standards.md"}
    assert {s.layer for s in loaded.sections} == {"base"}

    mirror.blobs[(BASE, REPO_STANDARDS_CONFIG)] = b"version: 1\nlanguages: [rails]\n"
    rails = _docs(await loader.load(REPO, BASE, None, []))
    assert {"lang-10_ruby_standards.md", "lang-11_ruby_on_rails_standards.md"} <= rails
    assert PY not in rails

    # No .standards.yml at the base commit: only the common documents apply.
    del mirror.blobs[(BASE, REPO_STANDARDS_CONFIG)]
    assert _docs(await loader.load(REPO, BASE, None, [])) == {
        "sec-01_security_standards.md",
        "core-standards.md",
    }


async def test_the_loader_reads_repo_files_only_at_the_commit_it_is_given(
    standards_dir: Path, tmp_path: Path
) -> None:
    mirror = FakeMirror(
        {
            (BASE, REPO_STANDARDS_CONFIG): b"languages: [python]\n",
            (BASE, REPO_REVIEW_FILE): f"# {PY} › Python\nUse ruff and mypy.\n".encode(),
        }
    )
    user_dir = tmp_path / "user-standards"
    _write(user_dir / "mine.md", f"# {PY} › Python\nAlso pyright.\n")
    _write(user_dir / "notes.txt", "# Not markdown\nignored\n")
    loader = StandardsLoader(mirror, lambda: standards_dir)
    loaded = await loader.load(
        "Acme/Widget", BASE, user_dir, [Override("core-standards.md", ("Core",), "KISS.")]
    )
    assert {(repo, sha, path) for repo, sha, path, _ in mirror.calls} == {
        (REPO, BASE, REPO_STANDARDS_CONFIG),
        (REPO, BASE, REPO_REVIEW_FILE),
    }
    by_id = {Standards.rule_id(s): s for s in loaded.sections}
    assert (by_id[f"{PY} › Python"].layer, by_id[f"{PY} › Python"].text) == (
        "user",
        "Also pyright.",
    )
    assert by_id["core-standards.md › Core"].text == "KISS."
    assert "ignored" not in loaded.as_prompt()


async def test_missing_layers_are_absent(tmp_path: Path) -> None:
    loader = StandardsLoader(FakeMirror({}), lambda: None)
    assert (await loader.load(REPO, BASE, tmp_path / "nowhere", [])).sections == []
    missing_dir = StandardsLoader(FakeMirror({}), lambda: tmp_path / "not-checked-out")
    assert (await missing_dir.load(REPO, BASE, None, [])).sections == []


async def test_a_standards_dir_without_a_standards_folder_is_read_directly(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "flat" / "security" / "sec-01_security_standards.md", "# S\nNo eval.\n")
    loader = StandardsLoader(FakeMirror({}), lambda: tmp_path / "flat")
    assert _docs(await loader.load(REPO, BASE, None, [])) == {"sec-01_security_standards.md"}


async def test_oversized_documents_are_refused(standards_dir: Path, tmp_path: Path) -> None:
    mirror = FakeMirror({})
    mirror.error = MirrorFileTooLargeError(f"{REPO_REVIEW_FILE} is 300000 bytes")
    with pytest.raises(StandardsDocumentTooLargeError) as raised:
        await StandardsLoader(mirror, lambda: None).load(REPO, BASE, None, [])
    assert isinstance(raised.value.__cause__, MirrorFileTooLargeError)

    big = standards_dir / "standards" / "security" / "sec-02_big.md"
    big.write_text("# Big\n" + "x" * MAX_DOCUMENT_BYTES)
    with pytest.raises(StandardsDocumentTooLargeError, match="sec-02_big.md"):
        await StandardsLoader(FakeMirror({}), lambda: standards_dir).load(REPO, BASE, None, [])

    user_dir = tmp_path / "user"
    _write(user_dir / "huge.md", "y" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(StandardsDocumentTooLargeError, match="huge.md"):
        await StandardsLoader(FakeMirror({}), lambda: None).load(REPO, BASE, user_dir, [])


async def test_mirror_failures_and_bad_configs_are_typed(standards_dir: Path) -> None:
    mirror = FakeMirror({})
    mirror.error = GitCommandError(("git", "ls-tree"), 128, "fatal: not a tree object")
    with pytest.raises(StandardsUnreadableError, match="not a tree object") as raised:
        await StandardsLoader(mirror, lambda: None).load(REPO, BASE, None, [])
    assert raised.value.__cause__ is mirror.error

    for config in (b"languages: [python\n", b"languages: python\n", b"\xff\xfe"):
        bad = FakeMirror({(BASE, REPO_STANDARDS_CONFIG): config})
        with pytest.raises(InvalidStandardsConfigError):
            await StandardsLoader(bad, lambda: None).load(REPO, BASE, None, [])


async def test_a_binary_or_undecodable_repo_layer_is_absent() -> None:
    for content in (b"# T\n\xff\n", b"# T\nnul\0here\n"):
        mirror = FakeMirror({(BASE, REPO_REVIEW_FILE): content})
        loaded = await StandardsLoader(mirror, lambda: None).load(REPO, BASE, None, [])
        assert loaded.sections == []


async def test_hostile_standards_configs_are_typed_never_raw() -> None:
    deep = b"languages: " + b"[" * 50_000 + b"]" * 50_000 + b"\n"
    nested_maps = b"languages: " + b"{a: " * 5_000 + b"1" + b"}" * 5_000 + b"\n"
    for config in (deep, nested_maps):
        mirror = FakeMirror({(BASE, REPO_STANDARDS_CONFIG): config})
        with pytest.raises(InvalidStandardsConfigError, match="nest"):
            await StandardsLoader(mirror, lambda: None).load(REPO, BASE, None, [])
    # Brackets inside quoted strings are text, not nesting.
    quoted = b"version: 1\nlanguages: [python]\nnote: '" + b"[" * 100 + b"'\n"
    ok = FakeMirror({(BASE, REPO_STANDARDS_CONFIG): quoted})
    assert (await StandardsLoader(ok, lambda: None).load(REPO, BASE, None, [])).sections == []
    # The config gets its own, smaller cap.
    capped = FakeMirror({})
    await StandardsLoader(capped, lambda: None).load(REPO, BASE, None, [])
    limits = {path: limit for _, _, path, limit in capped.calls}
    assert limits[REPO_STANDARDS_CONFIG] == MAX_CONFIG_BYTES
    assert limits[REPO_REVIEW_FILE] == MAX_DOCUMENT_BYTES


async def test_symlinks_and_special_files_in_local_layers_are_not_followed(
    standards_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret\nhunter2\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "link.md").symlink_to(secret)
    (user_dir / "folder.md").mkdir()
    os.mkfifo(user_dir / "pipe.md")
    (standards_dir / "standards" / "security" / "link.md").symlink_to(secret)
    loader = StandardsLoader(FakeMirror({}), lambda: standards_dir)
    loaded = await asyncio.wait_for(loader.load(REPO, BASE, user_dir, []), timeout=5)
    assert "hunter2" not in loaded.as_prompt()


# --- against real git: the base commit, never the head ------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(work: Path, message: str) -> str:
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def mirror_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[GitWorkspace, str, str, str]]:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "main")
    _write(work / REPO_STANDARDS_CONFIG, "version: 1\nlanguages: [python]\n")
    _write(work / REPO_REVIEW_FILE, "# Tests\nEvery bug fix needs a regression test.\n")
    base = commit_all(work, "base")
    # The PR rewrites its own review standards and points the file at a secret.
    _write(work / REPO_STANDARDS_CONFIG, "version: 1\nlanguages: [typescript]\n")
    (work / REPO_REVIEW_FILE).unlink()
    (work / REPO_REVIEW_FILE).symlink_to("/etc/passwd")
    head = commit_all(work, "head")
    git(work, "checkout", "-q", "-b", "big", base)
    _write(work / REPO_REVIEW_FILE, "# Big\n" + "z" * MAX_DOCUMENT_BYTES)
    big = commit_all(work, "big review file")
    git(work, "push", "-q", "origin", "main", "big")
    workspace = GitWorkspace(tmp_path / "data" / "review" / "repos", lambda repo: str(remote))

    async def fetch() -> None:
        await workspace.ensure_mirror(REPO)
        await workspace.fetch(REPO, ["refs/heads/main", "refs/heads/big"])

    asyncio.run(fetch())
    yield workspace, base, head, big


async def test_a_pr_cannot_rewrite_its_own_standards(
    mirror_repo: tuple[GitWorkspace, str, str, str], standards_dir: Path
) -> None:
    workspace, base, head, big = mirror_repo
    loader = StandardsLoader(workspace, lambda: standards_dir)
    at_base = await loader.load(REPO, base, None, [])
    assert PY in _docs(at_base)
    assert "Every bug fix needs a regression test." in at_base.as_prompt()

    # At the head the config says typescript and review.md is a symlink: a symlink blob is
    # never followed or read, so that layer is simply absent.
    at_head = await loader.load(REPO, head, None, [])
    assert "lang-06_typescript_standards.md" in _docs(at_head)
    assert REPO_REVIEW_FILE not in _docs(at_head)
    assert "root:" not in at_head.as_prompt()

    with pytest.raises(StandardsDocumentTooLargeError):
        await loader.load(REPO, big, None, [])


async def test_show_file_reads_blobs_by_path_at_a_commit(
    mirror_repo: tuple[GitWorkspace, str, str, str],
) -> None:
    workspace, base, head, _ = mirror_repo
    assert await workspace.show_file(REPO, base, REPO_REVIEW_FILE, limit=1000) == (
        b"# Tests\nEvery bug fix needs a regression test.\n"
    )
    assert await workspace.show_file(REPO, base, "missing.md", limit=1000) is None
    assert await workspace.show_file(REPO, head, REPO_REVIEW_FILE, limit=1000) is None
    assert await workspace.show_file(REPO, base, ".jesinia", limit=1000) is None  # a tree
    with pytest.raises(MirrorFileTooLargeError):
        await workspace.show_file(REPO, base, REPO_REVIEW_FILE, limit=10)
    for unsafe in ("/etc/passwd", "../x", "-p", "", "a/../../b", "a//b"):
        with pytest.raises(InvalidTreePathError):
            await workspace.show_file(REPO, base, unsafe, limit=1000)


async def test_commit_messages_come_from_the_mirror(
    mirror_repo: tuple[GitWorkspace, str, str, str],
) -> None:
    workspace, base, head, _ = mirror_repo
    log = await workspace.commit_messages(REPO, base, head, limit=1 << 20)
    assert log.data.split(b"\0")[0].strip() == b"head"
    assert not log.truncated
    capped = await workspace.commit_messages(REPO, base, head, limit=2)
    assert capped.truncated


# --- the stored settings and overrides ------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteReviewStore]:
    conn: sqlite3.Connection = open_db(
        tmp_path / "data",
        ModuleDb("review", DbClass.HISTORY, files("jesinia.apps.review.migrations")),
    )
    yield SqliteReviewStore(conn)
    conn.close()


def test_store_keeps_standards_settings_and_overrides(
    store: SqliteReviewStore, tmp_path: Path
) -> None:
    assert store.standards_dir() is None and store.standards_user_dir() is None
    assert store.job_runner() == "codex"
    store.set_standards_dir(tmp_path / "standards")
    store.set_standards_user_dir(tmp_path / "mine")
    store.set_job_runner("claude-code")
    assert store.standards_dir() == tmp_path / "standards"
    assert store.standards_user_dir() == tmp_path / "mine"
    assert store.job_runner() == "claude-code"
    store.set_standards_dir(None)
    assert store.standards_dir() is None

    everywhere = Override(PY, ("Python",), "for every repo")
    here = Override(PY, ("Python",), "for this repo")
    store.add_standards_override(None, everywhere)
    store.add_standards_override("Acme/Widget", here)
    store.add_standards_override("acme/other", Override(PY, ("Other",), "elsewhere"))
    # The author's own overrides first, then this repo's, so the per-repo one wins.
    assert store.standards_overrides(REPO) == [everywhere, here]
    assert store.standards_overrides("acme/none") == [everywhere]


async def test_unreadable_local_files_and_bad_repo_names_are_typed(
    standards_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(StandardsUnreadableError, match="owner/name"):
        await StandardsLoader(FakeMirror({}), lambda: None).load("not a repo", BASE, None, [])

    real_open = os.open
    locked = standards_dir / "standards" / "security" / "sec-01_security_standards.md"

    def deny(path: object, flags: int, *args: int) -> int:
        if Path(str(path)) == locked:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", deny)
    loader = StandardsLoader(FakeMirror({}), lambda: standards_dir)
    with pytest.raises(StandardsUnreadableError, match="Permission denied"):
        await loader.load(REPO, BASE, None, [])


async def test_loading_large_standards_never_blocks_the_event_loop(
    standards_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "".join(f"# h{i}\n{'w' * 80}\n" for i in range(MAX_SECTIONS))
    mirror = FakeMirror({(BASE, REPO_REVIEW_FILE): body.encode()})
    loader = StandardsLoader(mirror, lambda: standards_dir)
    loop = asyncio.get_running_loop()
    real_open = os.open
    answers: list[bool] = []

    # A handshake, not a stopwatch: the load takes milliseconds, so gap timings can't tell
    # blocking from a slow CI runner. Each file read asks the loop to answer; a loader running
    # on the loop can't let it answer until the read returns, so the wait times out.
    def open_once_the_loop_answers(path: object, flags: int, *args: int) -> int:
        answered = threading.Event()
        loop.call_soon_threadsafe(answered.set)
        answers.append(answered.wait(timeout=2))
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_once_the_loop_answers)
    loaded = await loader.load(REPO, BASE, None, [])
    assert len(loaded.sections) > MAX_SECTIONS
    assert answers and all(answers)


@pytest.fixture
def stacked_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[GitWorkspace, Path, str]]:
    """main carries the repo's real standards; stack-a (another PR's branch, the base of a
    stacked PR) rewrites them."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "main")
    _write(work / REPO_STANDARDS_CONFIG, "version: 1\nlanguages: [python]\n")
    _write(work / REPO_REVIEW_FILE, "# Tests\nEvery bug fix needs a regression test.\n")
    main = commit_all(work, "main")
    git(work, "checkout", "-q", "-b", "stack-a")
    _write(work / REPO_STANDARDS_CONFIG, "version: 1\nlanguages: [typescript]\n")
    _write(work / REPO_REVIEW_FILE, "# sec-01_security_standards.md › Security\n\n")
    commit_all(work, "PR A weakens the standards")
    git(work, "push", "-q", "origin", "main", "stack-a")
    workspace = GitWorkspace(tmp_path / "data" / "review" / "repos", lambda repo: str(remote))

    async def fetch() -> None:
        await workspace.ensure_mirror(REPO)
        await workspace.fetch(REPO, ["refs/heads/stack-a"])

    asyncio.run(fetch())
    yield workspace, work, main


async def test_a_stacked_base_branch_cannot_rewrite_the_standards(
    stacked_repo: tuple[GitWorkspace, Path, str], standards_dir: Path
) -> None:
    workspace, work, main = stacked_repo
    # The default branch's tip is fetched on first use and kept under its own ref.
    assert await workspace.default_branch_head(REPO) == main
    loader = StandardsLoader(workspace, lambda: standards_dir)
    standards = await loader.load(REPO, main, None, [])
    assert PY in _docs(standards) and "lang-06_typescript_standards.md" not in _docs(standards)
    assert "sec-01_security_standards.md" in _docs(standards)
    assert "Every bug fix needs a regression test." in standards.as_prompt()

    # A newer default-branch tip is picked up by the next refresh, not before.
    git(work, "checkout", "-q", "main")
    _write(work / "README.md", "moved on\n")
    newer = commit_all(work, "main moves on")
    git(work, "push", "-q", "origin", "main")
    assert await workspace.default_branch_head(REPO) == main
    assert await workspace.fetch_default_branch(REPO) == newer
    assert await workspace.default_branch_head(REPO) == newer


async def test_mirror_reads_of_missing_repos_and_commits_fail_typed(
    stacked_repo: tuple[GitWorkspace, Path, str], tmp_path: Path
) -> None:
    workspace, _, main = stacked_repo
    with pytest.raises(GitCommandError):
        await workspace.default_branch_head("acme/never-mirrored")
    with pytest.raises(GitCommandError):
        await workspace.commit_messages(REPO, "e" * 40, main, limit=1 << 20)
