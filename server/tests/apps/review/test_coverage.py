# SPDX-License-Identifier: AGPL-3.0-or-later
"""Walkthrough coverage (plan Task 6, spec §3.1 "Walkthrough coverage"): computed by Jesinia
from the steps' hunks against the full diff, never reported by the agent, and never silent
about what was left out."""

from __future__ import annotations

from dataclasses import replace

import pytest

from jesinia.apps.review.domain.coverage import (
    INCOMPLETE_CEILING,
    Coverage,
    DiffParseError,
    HunkRef,
    LineRange,
    ParsedDiff,
    Skip,
    Step,
    checked_coverage,
    coverage,
    parse_unified_diff,
    ranges_from_json,
    skips_from_json,
    steps_from_json,
)

APP = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 a
-b
+B
+c
 d
@@ -10,2 +11,2 @@ def f():
 x
-y
+Y
"""
LOCK = """\
diff --git a/web/pnpm-lock.yaml b/web/pnpm-lock.yaml
index 1111111..2222222 100644
--- a/web/pnpm-lock.yaml
+++ b/web/pnpm-lock.yaml
@@ -1,3 +1,3 @@
-one
-two
-three
+uno
+dos
+tres
"""
BINARY = """\
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..3333333
Binary files /dev/null and b/assets/logo.png differ
"""
RENAME = """\
diff --git a/old name.py b/new name.py
similarity index 100%
rename from old name.py
rename to new name.py
"""
ADDED = """\
diff --git a/docs/notes.md b/docs/notes.md
new file mode 100644
index 0000000..4444444
--- /dev/null
+++ b/docs/notes.md
@@ -0,0 +1,2 @@
+hello
+world
"""
DELETED = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 5555555..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""
HUGE = APP + LOCK + BINARY + RENAME + ADDED + DELETED


def walk(*refs: tuple[str, int]) -> Step:
    return Step(hunks=tuple(HunkRef(path, hunk) for path, hunk in refs), title="step")


# --- parsing -----------------------------------------------------------------------------------


def test_parse_unified_diff_reads_files_hunks_and_changed_lines() -> None:
    diff = parse_unified_diff(HUGE)
    assert [f.path for f in diff.files] == [
        "src/app.py",
        "web/pnpm-lock.yaml",
        "assets/logo.png",
        "new name.py",
        "docs/notes.md",
        "gone.py",
    ]
    app, lock, logo, renamed, added, gone = diff.files
    assert app.status == "modified" and len(app.hunks) == 2 and app.changed_lines == 5
    first = app.hunks[0]
    assert [(line.kind, line.number) for line in first.lines] == [("-", 2), ("+", 2), ("+", 3)]
    assert lock.changed_lines == 6
    assert logo.is_binary and logo.status == "added" and logo.hunks == ()
    assert renamed.status == "renamed" and renamed.old_path == "old name.py"
    assert renamed.hunks == ()
    assert added.status == "added" and added.changed_lines == 2
    assert gone.status == "deleted" and gone.path == "gone.py" and gone.changed_lines == 2
    assert diff.changed_lines == 15 and diff.complete
    assert diff.file("src/app.py") is app and diff.file("nope") is None


def test_parse_handles_quoted_paths_and_missing_newlines() -> None:
    text = (
        'diff --git "a/caf\\303\\251 \\"x\\".py" "b/caf\\303\\251 \\"x\\".py"\n'
        "index 1..2 100644\n"
        '--- "a/caf\\303\\251 \\"x\\".py"\n'
        '+++ "b/caf\\303\\251 \\"x\\".py"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
        'diff --git a/tab "b/t\\tb\\\\c"\n'
        "similarity index 90%\n"
        "rename from tab\n"
        'rename to "t\\tb\\\\c"\n'
        "--- a/tab\n"
        '+++ "b/t\\tb\\\\c"\n'
        "@@ -1,2 +1,2 @@\n"
        " --- not a header\n"
        "--- removed line that looks like a header\n"
        "+++ added line that looks like a header\n"
    )
    diff = parse_unified_diff(text)
    first, second = diff.files
    assert first.path == 'café "x".py' and first.changed_lines == 2
    assert second.path == "t\tb\\c" and second.old_path == "tab"
    assert second.changed_lines == 2


def test_parse_reads_mode_changes_and_an_empty_diff() -> None:
    mode = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755"  # no final newline
    (only,) = parse_unified_diff(mode).files
    assert only.status == "modified" and only.hunks == () and not only.is_binary
    assert parse_unified_diff("").files == ()


@pytest.mark.parametrize(
    "text",
    [
        "not a diff\n",
        "diff --git a/x b/x\n@@ nonsense @@\n",
        "diff --git a/x b/x\n@@ -1,2 +1,2 @@\n a\n",  # the hunk stops short
        "diff --git a/x b/x\n@@ -1 +1 @@\n?odd\n",
        'diff --git "a/x b/x\n',  # an unterminated quote
        "diff --git a/x\n",
        'diff --git "a/x" "b/\\9"\n',  # a bad escape
    ],
)
def test_parse_refuses_malformed_diffs(text: str) -> None:
    with pytest.raises(DiffParseError):
        parse_unified_diff(text)


# --- walkthrough coverage -----------------------------------------------------------------------


def test_coverage_counts_skipped_binaries_and_lockfiles() -> None:
    """Review Focus 2: a huge PR with a lockfile and a binary. Both go straight to "not walked
    through" with their reason, and the coverage line counts their lines honestly."""
    diff = parse_unified_diff(HUGE)
    result = coverage(
        diff,
        [walk(("src/app.py", 0), ("src/app.py", 1)), walk(("docs/notes.md", 0))],
        [Skip("gone.py", None, "judged_mechanical")],
    )
    assert isinstance(result, Coverage)
    # 7 of 15 changed lines: the lockfile's 6 and the deletion's 2 were not walked.
    assert result.lines_covered == 7 and result.lines_total == 15
    assert result.pct == pytest.approx(46.7)
    assert (result.files_covered, result.files_total) == (2, 6)
    assert result.not_walked == [
        Skip("web/pnpm-lock.yaml", None, "lockfile"),
        Skip("assets/logo.png", None, "binary"),
        Skip("new name.py", None, "rename_only"),
        Skip("gone.py", None, "judged_mechanical"),
    ]
    # Only Jesinia's own skips leave the reviewable lines: the agent's don't.
    assert result.lines_auto_skipped == 6
    assert result.reviewable_pct == pytest.approx(77.8)
    assert result.complete and result.incomplete_reason is None


def test_unknown_hunks_count_as_uncovered() -> None:
    """Review Focus 3: a step citing a hunk that doesn't exist is dropped, and the lines it
    claimed (even its real ones) count as not covered."""
    diff = parse_unified_diff(APP)
    result = coverage(
        diff,
        [
            walk(("src/app.py", 0), ("src/app.py", 7)),  # hunk 7 doesn't exist
            walk(("src/ghost.py", 0)),
            Step(hunks=(), title="cites nothing"),
            walk(("src/app.py", 1)),
        ],
        [],
    )
    assert result.dropped_steps == 3
    assert result.lines_covered == 2 and result.pct == pytest.approx(40.0)
    assert result.files_covered == 0
    assert result.not_walked == [Skip("src/app.py", 0, "omitted")]


def test_agent_skips_are_validated_and_never_hide_walked_code() -> None:
    diff = parse_unified_diff(APP + LOCK)
    result = coverage(
        diff,
        [walk(("src/app.py", 0)), walk(("web/pnpm-lock.yaml", 0))],
        [
            Skip("src/app.py", 0, "format_only"),  # walked: ignored
            Skip("src/app.py", 1, "format_only"),
            Skip("src/app.py", 1, "generated"),  # a duplicate: the first one wins
            Skip("src/ghost.py", None, "generated"),  # not in the diff
            Skip("src/app.py", 9, "generated"),  # no such hunk
            Skip("src/app.py", 1, "omitted"),  # not a reason an agent may give
        ],
    )
    # The lockfile was walked after all, so it isn't skipped.
    assert result.not_walked == [Skip("src/app.py", 1, "format_only")]
    assert result.files_covered == 1 and result.lines_auto_skipped == 0


def test_a_file_level_skip_from_the_agent_covers_its_unwalked_hunks() -> None:
    diff = parse_unified_diff(APP)
    result = coverage(diff, [], [Skip("src/app.py", None, "generated")])
    assert result.not_walked == [Skip("src/app.py", None, "generated")]
    assert result.pct == 0.0 and result.lines_auto_skipped == 0


def test_a_partly_walked_auto_skip_file_lists_only_its_hunks() -> None:
    two_hunks = LOCK + "@@ -9 +9 @@\n-nine\n+nueve\n"
    lock = parse_unified_diff(two_hunks + LOCK.replace("web/", "app/"))
    result = coverage(lock, [walk(("web/pnpm-lock.yaml", 0))], [])
    assert result.not_walked == [
        Skip("web/pnpm-lock.yaml", 1, "omitted"),
        Skip("app/pnpm-lock.yaml", None, "lockfile"),
    ]
    assert result.lines_auto_skipped == 6
    mode = parse_unified_diff("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n")
    assert coverage(mode, [], []).not_walked == [Skip("run.sh", None, "metadata_only")]


def test_full_and_empty_walks() -> None:
    diff = parse_unified_diff(APP)
    full = coverage(diff, [walk(("src/app.py", 0), ("src/app.py", 1))], [])
    assert full.pct == 100.0 and full.not_walked == [] and full.files_covered == 1
    assert full.reviewable_pct == 100.0
    empty = coverage(parse_unified_diff(""), [], [])
    assert (empty.pct, empty.files_total, empty.reviewable_pct) == (100.0, 0, 100.0)
    only_binary = coverage(parse_unified_diff(BINARY), [], [])
    assert only_binary.pct == 0.0 and only_binary.reviewable_pct == 100.0
    assert only_binary.lines_auto_skipped == 0


def test_an_incomplete_diff_never_shows_full_coverage() -> None:
    """When the diff couldn't be read in full (too large), the coverage says so and its
    denominators are GitHub's own counts, so it can never claim every line."""
    diff = parse_unified_diff(APP)
    cut = replace(
        diff, complete=False, incomplete_reason="the diff is too large", expected_lines=500,
        expected_files=40,
    )  # fmt: skip
    result = coverage(cut, [walk(("src/app.py", 0), ("src/app.py", 1))], [])
    assert not result.complete and result.incomplete_reason == "the diff is too large"
    assert result.lines_total == 500 and result.files_total == 40
    assert result.pct == 1.0 and result.reviewable_pct == 1.0
    small = replace(diff, complete=False, incomplete_reason="cut", expected_lines=1)
    capped = coverage(small, [walk(("src/app.py", 0), ("src/app.py", 1))], [])
    assert capped.pct == INCOMPLETE_CEILING and capped.reviewable_pct == INCOMPLETE_CEILING
    nothing = coverage(ParsedDiff((), complete=False, incomplete_reason="cut"), [], [])
    assert nothing.pct == 0.0 and nothing.reviewable_pct == 0.0


def test_coverage_as_json_is_what_the_queue_and_ui_read() -> None:
    result = coverage(parse_unified_diff(APP + BINARY), [walk(("src/app.py", 0))], [])
    data = result.as_json()
    assert data["pct"] == result.pct and data["files_total"] == 2
    assert data["not_walked"] == [
        {"path": "src/app.py", "hunk": 1, "reason": "omitted"},
        {"path": "assets/logo.png", "hunk": None, "reason": "binary"},
    ]
    assert data["complete"] is True and data["incomplete_reason"] is None
    assert {"lines_covered", "lines_total", "reviewable_pct", "dropped_steps"} <= set(data)


# --- the correctness pass's own coverage --------------------------------------------------------


def test_checked_coverage_counts_the_changed_lines_the_pass_read() -> None:
    diff = parse_unified_diff(APP + DELETED + BINARY)
    read = checked_coverage(
        diff,
        [
            LineRange("src/app.py", 1, 3),  # hunk 0: -b (anchor 2), +B (2), +c (3)
            LineRange("src/app.py", 12, 12),  # hunk 1: +Y at 12; -y anchors at 12 too
            LineRange("gone.py", 1, 1),  # deleted files are read by their old lines
            LineRange("elsewhere.py", 1, 99),
        ],
    )
    assert read.lines_covered == 6 and read.lines_total == 7
    assert read.files_covered == 1
    assert read.not_walked == [
        Skip("gone.py", 0, "omitted"),
        Skip("assets/logo.png", None, "binary"),
    ]


# --- agent output --------------------------------------------------------------------------------


def test_walkthrough_and_correctness_json_become_steps_skips_and_ranges() -> None:
    walkthrough = {
        "steps": [
            {"title": "t", "body": "b", "hunks": [{"path": "a.py", "hunk": 0}]},
            {"title": "bad", "body": "b", "hunks": [{"path": "a.py", "hunk": 1.0}]},
            {"title": "bool", "body": "b", "hunks": [{"path": "a.py", "hunk": True}]},
            "junk",
        ],
        "skipped": [
            {"path": "b.py", "hunk": None, "reason": "generated"},
            {"path": "c.py", "hunk": 2, "reason": "format_only"},
            {"path": "d.py", "hunk": "x", "reason": "generated"},
            {"path": 5, "hunk": None, "reason": "generated"},
        ],
    }
    steps = steps_from_json(walkthrough)
    assert steps[0] == Step((HunkRef("a.py", 0),), "t")
    # A step with a malformed hunk keeps no hunks, so coverage drops it.
    assert steps[1].hunks == () and steps[2].hunks == () and len(steps) == 3
    assert skips_from_json(walkthrough) == [
        Skip("b.py", None, "generated"),
        Skip("c.py", 2, "format_only"),
    ]
    assert steps_from_json(None) == [] and skips_from_json({"skipped": "x"}) == []
    assert steps_from_json({"steps": [{"hunks": "x"}]}) == [Step((), "")]
    checked = {
        "checked": [
            {"path": "a.py", "start_line": 1, "end_line": 4},
            {"path": "a.py", "start_line": 1e308, "end_line": 4},
            {"path": "a.py", "start_line": 5, "end_line": 2},
            "junk",
        ]
    }
    assert ranges_from_json(checked) == [LineRange("a.py", 1, 4)]
    assert ranges_from_json(None) == [] and ranges_from_json({"checked": None}) == []
