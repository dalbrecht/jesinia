# SPDX-License-Identifier: AGPL-3.0-or-later
"""Walkthrough coverage (plan Task 6, spec §3.1 "Walkthrough coverage"): computed by Jesinia
from the steps' hunks against the full diff, never reported by the agent, and never silent
about what was left out."""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Callable
from dataclasses import replace

import pytest

from jesinia.apps.review.domain.coverage import (
    INCOMPLETE_CEILING,
    MAX_CHANGED_LINES,
    MAX_FILES,
    NOT_WALKED_LIMIT,
    Coverage,
    DiffParseError,
    HunkRef,
    LineRange,
    ParsedDiff,
    Skip,
    Step,
    checked_coverage,
    coverage,
    hunk_index,
    parse_unified_diff,
    ranges_from_json,
    skips_from_json,
    steps_from_json,
    strip_step_number,
    verified_ranges,
    with_clean_step_titles,
    with_clean_titles,
)
from jesinia.apps.review.domain.workspace import DiffTooLargeError
from tests.apps.review.perf import CEILING, assert_linear

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
    assert [(run.kind, run.start, run.end, run.anchor) for run in first.runs] == [
        ("-", 2, 2, 2),
        ("+", 2, 3, 2),
    ]
    assert first.header == "@@ -1,3 +1,4 @@" and first.changed == 3
    assert lock.changed_lines == 6
    assert logo.is_binary and logo.status == "added" and logo.hunks == ()
    assert renamed.status == "renamed" and renamed.old_path == "old name.py"
    assert renamed.hunks == ()
    assert added.status == "added" and added.changed_lines == 2
    assert gone.status == "deleted" and gone.path == "gone.py" and gone.changed_lines == 2
    assert diff.changed_lines == 15 and diff.complete
    assert diff.has_path("src/app.py") and not diff.has_path("nope")
    assert diff.locate("src/app.py", 1) == (0, 1) and diff.locate("src/app.py", 2) is None


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
        "diff --git a/x b/x\n@@ -" + "9" * 5000 + " +1 @@\n",  # absurd line numbers
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
    # Only Jesinia's own skips and whole-file deletions (there's nothing left to read at
    # the head) leave the reviewable lines; the agent's skips don't.
    assert result.lines_auto_skipped == 6
    assert result.reviewable_pct == 100.0
    assert result.not_walked_total == 4
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
    # Nothing to walk (binaries, renames) reads as fully covered, not as "0% looked at".
    only_binary = coverage(parse_unified_diff(BINARY), [], [])
    assert only_binary.pct == 100.0 and only_binary.reviewable_pct == 100.0
    assert only_binary.not_walked == [Skip("assets/logo.png", None, "binary")]
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


# --- bounds (fix round 1, R41) ------------------------------------------------------------------


def _added(path: str, lines: int, hunks: int = 1) -> str:
    """A diff adding ``lines`` lines to ``path``, split over ``hunks`` hunks."""
    per = lines // hunks
    parts = [f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"]
    for h in range(hunks):
        start = h * (per + 10) + 1
        parts.append(f"@@ -{start},0 +{start},{per} @@\n" + "+x\n" * per)
    return "".join(parts)


def test_a_diff_over_the_changed_line_cap_is_refused_quickly() -> None:
    text = _added("big.py", MAX_CHANGED_LINES + 1)
    started = time.perf_counter()
    with pytest.raises(DiffTooLargeError, match=f"over {MAX_CHANGED_LINES} changed lines") as e:
        parse_unified_diff(text)
    assert e.value.limit == MAX_CHANGED_LINES
    assert time.perf_counter() - started < CEILING  # refused, never parsed for minutes
    many = "".join(f"diff --git a/f{i} b/f{i}\nold mode 100644\nnew mode 100755\n"
                   for i in range(MAX_FILES + 1))  # fmt: skip
    with pytest.raises(DiffTooLargeError, match=f"over {MAX_FILES} files"):
        parse_unified_diff(many)


def test_coverage_of_a_huge_diff_is_fast_and_bounded() -> None:
    """150k changed lines in 3,000 hunks, read with the schema's 2,000 ranges: coverage is
    interval maths, not per-line sets (linear in the diff, within a generous ceiling)."""

    def work(hunks: int) -> Callable[[], object]:
        diff = parse_unified_diff(_added("big.py", hunks * 50, hunks=hunks))
        ranges = [LineRange("big.py", i * 80 + 1, i * 80 + 40) for i in range(2_000)]
        walked = [walk(*(("big.py", h) for h in range(0, hunks, 2)))]
        return lambda: (checked_coverage(diff, ranges), coverage(diff, walked, []))

    assert_linear(work, 750)  # 37,500 changed lines, then 150,000
    text = _added("big.py", 150_000, hunks=3_000)
    tracemalloc.start()
    diff = parse_unified_diff(text)
    ranges = [LineRange("big.py", i * 80 + 1, i * 80 + 40) for i in range(2_000)]
    read = checked_coverage(diff, ranges)
    walked = coverage(diff, [walk(*(("big.py", h) for h in range(0, 3_000, 2)))], [])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 64 * 1024 * 1024  # the 1 MB diff text plus compact hunks
    assert read.lines_total == 150_000 and 0 < read.lines_covered < 150_000
    assert walked.lines_covered == 75_000 and walked.pct == 50.0
    # Half the hunks weren't walked: the list is capped, with the full count beside it.
    assert len(walked.not_walked) == NOT_WALKED_LIMIT and walked.not_walked_total == 1_500
    assert walked.as_json()["not_walked_total"] == 1_500


def test_checked_ranges_merge_and_count_each_line_once() -> None:
    diff = parse_unified_diff(_added("a.py", 10))
    overlapping = [LineRange("a.py", 1, 4), LineRange("a.py", 3, 6), LineRange("a.py", 9, 30)]
    assert checked_coverage(diff, overlapping).lines_covered == 8
    # A removed run counts only when the line it sat in front of was read.
    app = checked_coverage(parse_unified_diff(APP), [LineRange("src/app.py", 12, 12)])
    assert app.lines_covered == 2 and app.not_walked == [Skip("src/app.py", 0, "omitted")]


# --- the hunk index agents cite (fix round 1, R42) ------------------------------------------------


def test_hunk_index_lists_every_hunk_with_jesinias_numbering() -> None:
    index = hunk_index(parse_unified_diff(APP + BINARY + RENAME))
    assert not index.truncated and index.files_listed == index.files_total == 3
    assert '"src/app.py" (modified)' in index.text
    assert "hunk 0: @@ -1,3 +1,4 @@ (+2 -1)" in index.text
    assert "hunk 1: @@ -10,2 +11,2 @@ (+1 -1)" in index.text
    assert '"assets/logo.png" (added, binary: no hunks)' in index.text
    assert '"new name.py" (renamed from "old name.py": no hunks)' in index.text


def test_hunk_index_is_capped() -> None:
    diff = parse_unified_diff("".join(_added(f"f{i}.py", 1) for i in range(400)))
    index = hunk_index(diff, limit=2_000)
    assert index.truncated and index.files_total == 400
    assert 0 < index.files_listed < 400 and len(index.text) <= 2_000


# --- step 0 of Task 7: one huge file never hides the rest; a type change counts twice --------

TYPE_CHANGE = """\
diff --git a/link b/link
deleted file mode 100644
index 1111111..0000000
--- a/link
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
diff --git a/link b/link
new file mode 120000
index 0000000..2222222
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+target
\\ No newline at end of file
"""


def test_a_huge_file_in_the_index_is_summarised_and_later_files_still_listed() -> None:
    lockfile = _added("pnpm-lock.yaml", 4_000, hunks=2_000)
    diff = parse_unified_diff(lockfile + APP + ADDED)
    index = hunk_index(diff)
    assert index.files_listed == index.files_total == 3 and not index.truncated
    assert index.files_summarised == 1
    assert '"pnpm-lock.yaml" (modified: 2000 hunks, 0 to 1999, too many to list here)' in (
        index.text
    )
    assert '"src/app.py" (modified)' in index.text
    assert "hunk 1: @@ -10,2 +11,2 @@ (+1 -1)" in index.text
    assert '"docs/notes.md" (added)' in index.text


def test_a_file_whose_summary_does_not_fit_is_skipped_and_counted() -> None:
    diff = parse_unified_diff(_added("a" * 300 + ".py", 2) + APP)
    index = hunk_index(diff, limit=250)
    assert index.files_total == 2 and index.files_listed == 1 and index.truncated
    assert '"src/app.py" (modified' in index.text and "aaaa" not in index.text


def test_a_type_change_counts_both_entries_of_the_path() -> None:
    diff = parse_unified_diff(TYPE_CHANGE)
    assert [f.status for f in diff.files] == ["deleted", "added"]
    both = coverage(diff, [walk(("link", 0), ("link", 1))], [])
    assert (both.pct, both.files_covered, both.files_total) == (100.0, 2, 2)
    assert both.lines_covered == both.lines_total == 3 and both.not_walked == []
    second = coverage(diff, [walk(("link", 1))], [])
    assert second.lines_covered == 1 and second.dropped_steps == 0
    assert second.not_walked == [Skip("link", 0, "omitted")]
    first = coverage(diff, [walk(("link", 0))], [Skip("link", 1, "judged_mechanical")])
    assert first.lines_covered == 2
    assert first.not_walked == [Skip("link", 1, "judged_mechanical")]
    assert coverage(diff, [walk(("link", 2))], []).dropped_steps == 1


def test_the_index_numbers_a_type_changes_hunks_across_its_entries() -> None:
    text = hunk_index(parse_unified_diff(TYPE_CHANGE)).text
    assert '"link" (deleted)\n  hunk 0: @@ -1,2 +0,0 @@ (+0 -2)' in text
    assert '"link" (added)\n  hunk 1: @@ -0,0 +1,1 @@ (+1 -0)' in text


def test_checked_coverage_reads_both_entries_of_a_type_change() -> None:
    diff = parse_unified_diff(TYPE_CHANGE)
    read = checked_coverage(diff, [LineRange("link", 1, 2)])
    assert read.lines_covered == 3 and read.files_covered == 2


# --- M7: claimed ranges count only where the agent's tools actually read -----------------------


def test_verified_ranges_keep_only_what_was_read() -> None:
    claimed = [LineRange("a.py", 1, 100), LineRange("b.py", 5, 9), LineRange("c.py", 1, 3)]
    read = [LineRange("a.py", 1, 10), LineRange("a.py", 50, 200), LineRange("b.py", 1, 5)]
    read.append(LineRange("d.py", 1, 50))  # read but never claimed: not counted
    assert verified_ranges(claimed, read) == [
        LineRange("a.py", 1, 10),
        LineRange("a.py", 50, 100),
        LineRange("b.py", 5, 5),
    ]
    assert verified_ranges(claimed, []) == []


def test_read_ranges_come_from_jesinias_own_record() -> None:
    record = {"read": [{"path": "a.py", "start_line": 3, "end_line": 4}, {"path": 1}]}
    assert ranges_from_json(record, key="read") == [LineRange("a.py", 3, 4)]
    assert ranges_from_json(None, key="read") == []


def test_nothing_to_walk_is_flagged_when_the_diff_changes_no_lines() -> None:
    renamed = coverage(parse_unified_diff(RENAME + BINARY), [], [])
    assert renamed.nothing_to_walk and renamed.as_json()["nothing_to_walk"] is True
    assert renamed.pct == 100.0
    assert not coverage(parse_unified_diff(APP), [], []).nothing_to_walk
    cut = ParsedDiff((), complete=False, incomplete_reason="too big", expected_lines=0)
    assert not coverage(cut, [], []).nothing_to_walk


@pytest.mark.parametrize(
    ("title", "position", "expected"),
    [
        ("1. Give packout claims an index", 1, "Give packout claims an index"),
        ("12) Add the route", 12, "Add the route"),
        ("Step 1: Add the route", 1, "Add the route"),
        ("step 2 - Add the route", 2, "Add the route"),
        ("Step 3. Add the route", 3, "Add the route"),
        ("Step 4 \u2014 Add the route", 4, "Add the route"),
        ("#1 Add the route", 1, "Add the route"),
        ("  7.   Add the route  ", 7, "Add the route"),
        ("#123 Fix token refresh", 1, "#123 Fix token refresh"),
        ("#2 Fix token refresh", 1, "#2 Fix token refresh"),
        ("7. Something", 3, "7. Something"),
        ("Step 9: Something", 2, "Step 9: Something"),
        ("3D preview fixes", 3, "3D preview fixes"),
        ("2026 migration", 2026, "2026 migration"),
        ("1.5 release notes", 1, "1.5 release notes"),
        ("Step by step guide", 1, "Step by step guide"),
        ("1. ", 1, "1. "),
        ("Step 1:", 1, "Step 1:"),
        ("", 1, ""),
    ],
)
def test_should_strip_only_a_delimited_leading_number_equal_to_the_position(
    title: str, position: int, expected: str
) -> None:
    assert strip_step_number(title, position) == expected


def test_should_clean_every_step_title_and_keep_the_rest_of_the_walkthrough() -> None:
    walkthrough = {
        "steps": [
            {"title": "1. First", "body": "b", "hunks": []},
            {"title": "#123 Fix token refresh", "body": "b", "hunks": []},
            {"body": "no title"},
            {"title": 5},
            "not a step",
        ],
        "skipped": [],
    }
    cleaned = with_clean_step_titles(walkthrough)
    assert cleaned["steps"] == [
        {"title": "First", "body": "b", "hunks": []},
        {"title": "#123 Fix token refresh", "body": "b", "hunks": []},
        {"body": "no title"},
        {"title": 5},
        "not a step",
    ]
    assert cleaned["skipped"] == []
    assert walkthrough["steps"][0]["title"] == "1. First"  # type: ignore[index]


def test_should_leave_a_walkthrough_without_a_steps_list_alone() -> None:
    assert with_clean_step_titles({"steps": "oops"}) == {"steps": "oops"}
    assert with_clean_step_titles({}) == {}


def test_should_clean_titles_of_walkthrough_kinds_only() -> None:
    walk = {"steps": [{"title": "1. A"}, {"title": "7. B"}]}
    clean = {"steps": [{"title": "A"}, {"title": "7. B"}]}
    assert with_clean_titles("walkthrough", walk) == clean
    assert with_clean_titles("stack_walkthrough", walk) == clean
    rereview = {"new_changes_walkthrough": walk, "requests": []}
    assert with_clean_titles("rereview", rereview) == {
        "new_changes_walkthrough": clean,
        "requests": [],
    }
    assert with_clean_titles("rereview", {"requests": []}) == {"requests": []}
    assert with_clean_titles("findings", walk) is walk
    assert with_clean_titles("walkthrough", None) is None
