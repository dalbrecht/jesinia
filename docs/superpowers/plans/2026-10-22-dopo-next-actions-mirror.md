# DOPO Tasks Next-Actions Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror each KB project page's open DOPO Tasks actions into a marked region under its `## Next Actions` heading, render today's actions into `01 Today/Today.md`, and (Phase 2) turn a tick in the KB into a single-line completion edit of DOPO's `tasks.md`.

**Architecture:** A new service module `server/jesinia/services/tasksmirror/` (`domain ← application ← infrastructure ← api`). The domain layer reads the `tasks.md` dialect, selects open actions, maps them to project pages by name, renders and replaces regions, and edits one line for a completion. The application layer runs one pass on a 15 s mtime poll (plus midnight and `kb.page_changed`) and writes pages through `KbService.update_body(expected_rev=…)`. A SQLite ledger keeps pending completions. Settings (launcher PWA) gets a DOPO Tasks card from `GET /api/tasksmirror/status`.

**Tech Stack:** Python 3 / FastAPI / SQLite, pytest (`server/`); React 19 + TypeScript, vitest (`web/apps/launcher`); typed client from OpenAPI (`make gen-client`).

**Spec:** `docs/specs/2026-10-22-dopo-next-actions-mirror-design.md`

## Global Constraints

- Every source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later` (`make spdx`).
- No import from, and no network call to, `amicus-platform` code (ADR 0003). The dialect reader is Jesinia's own, pinned by hand-written fixtures.
- Python: mypy `--strict`, ruff; `domain/` at 100% coverage, the rest at ≥ 95% (`make coverage-check`).
- import-linter: `tasksmirror` imports `core` and reaches `kb` only through `ctx.services["kb"]` (`make imports`).
- The mirror never writes outside its marked regions, never rewrites a page whose region is unchanged, never rewrites the whole `tasks.md`, and never computes or changes a `rev`.
- Failures are caught at the pass boundary, logged with the original cause (structured: operation, page id, path), and surfaced in status. Nothing propagates into `kb`.
- Copy, verbatim from the spec: "No next actions in DOPO Tasks.", "Nothing from DOPO Tasks today.", "From DOPO Tasks", "Overdue", "Today", "Tomorrow", "pending DOPO", "Not connected", "Sync now", "Open DOPO Tasks to apply".
- Phase 1 is Tasks 1–7 and ships on its own PR(s). Phase 2 (Tasks 8–10) starts only after Phase 1 has run against the real `tasks.md`.

## Review Focus

1. **User text is never lost:** a line typed inside the region is moved below it, not deleted. Test in Task 3 (`test_should_move_user_lines_out_of_the_region`).
2. **No churn:** a second pass over the same `tasks.md` issues zero KB writes. Test in Task 5 (`test_should_not_write_when_regions_are_unchanged`).
3. **The completion edit is minimal:** only the box and `@status:` change, the trailer and every other byte stay the same. Test in Task 8 (`test_should_change_only_box_and_status_of_target_line`).
4. **Racing DOPO's rewrite:** a completion that DOPO overwrote is re-applied, and one DOPO superseded (rev changed, still open) is dropped. Test in Task 9.
5. **Local midnight:** Today regroups across midnight without a `tasks.md` change. Test in Task 4 (`test_should_regroup_today_across_local_midnight`).
6. **No DOPO write is ever discarded:** a file DOPO installs during Jesinia's edit survives, with at most Jesinia's one line added. Test in Task 9 (`test_should_keep_dopo_write_that_lands_before_swap`). A done line still at the base rev is not yet *applied* (`test_should_keep_written_request_pending_until_rev_changes`).

---

### Task 1: ADR, KB format, and config key

**Files:**
- Create: `docs/adr/0011-dopo-tasks-mirror.md`
- Modify: `docs/conventions/kb-format.md` (new "Mirror regions" subsection under Body)
- Modify: `server/jesinia/core/config.py` (`tasks_md_path: Path | None = None`, serialize/parse/`set_value`)
- Test: `server/tests/core/test_config.py`

- [ ] **Step 1:** ADR 0011: Jesinia reads and line-edits DOPO's `tasks.md` (format-only, extends ADR 0003). Phased read-only then completion-only. Name-based mapping with a `tasks_project` override.
- [ ] **Step 2:** Document the `<!-- jesinia:mirror … -->` / `<!-- /jesinia:mirror -->` region in `kb-format.md`: owned by Jesinia, user lines are moved out, and the `jesinia.tasks_project` front-matter key.
- [ ] **Step 3:** Failing tests: `test_should_round_trip_tasks_md_path`, `test_should_default_tasks_md_path_to_none`, `test_should_reject_relative_tasks_md_path`. Implement. `make test`.
- [ ] **Step 4:** Commit `feat(config): add tasks_md_path for the DOPO mirror`.

### Task 2: Domain: `tasks.md` reader

**Files:**
- Create: `server/jesinia/services/tasksmirror/{__init__,domain/__init__,domain/dialect}.py`
- Create: `server/tests/services/tasksmirror/fixtures/canonical.md` + `expected.json`
- Test: `server/tests/services/tasksmirror/domain/test_dialect.py`

- [ ] **Step 1:** Write fixtures by hand from the spec §3 grammar: every state, every token, `@waiting:"a \"quoted\" reason"`, unknown tokens, `*`/`+` bullets, `[X]`, sections, prose, front matter, a line with no trailer, and a `[x]` box with `@status:next` (state stays `next`).
- [ ] **Step 2:** Failing tests: parse each fixture line into `TaskLine(title, state, context, project, energy, waiting_reason, due, defer, item_id, rev, extra_tokens, line_number)`. Invalid dates are kept as extra tokens. Non-task lines are skipped.
- [ ] **Step 3:** Implement `parse_tasks_md(text: str) -> list[TaskLine]` with frozen dataclasses and `datetime.date`. 100% coverage.
- [ ] **Step 4:** Commit `feat(tasksmirror): read the tasks.md dialect`.

### Task 3: Domain: selection, mapping, project region

**Files:**
- Create: `server/jesinia/services/tasksmirror/domain/{select,mapping,region}.py`
- Test: `server/tests/services/tasksmirror/domain/test_{select,mapping,region}.py`

- [ ] **Step 1 (select):** `open_actions(lines, today)` keeps `next/in-progress/waiting/blocked` with no future `defer`. Tests per state and defer boundary (`defer == today` is kept).
- [ ] **Step 2 (mapping):** `map_projects(pages: list[ProjectPage], lines) -> Mapping` with NFC + casefold + trim keys, the `tasks_project` override, `unmatched`, `collisions`. Tests: `test_should_match_case_and_nfc_insensitively`, `test_should_prefer_tasks_project_key`, `test_should_withhold_both_pages_on_collision`, `test_should_skip_lines_without_project`, `test_should_flag_paused_page_with_next_actions` (spec §4.3; `waiting`/`blocked` alone doesn't flag).
- [ ] **Step 3 (render):** `render_project_region(project, actions) -> str` following spec §5.5 line shape and order, and the empty text. Neutralize `<!--`/`-->` in titles (reuse the KB service's neutralizer if it is a pure function. Otherwise move it into `core` in this task, with tests).
- [ ] **Step 4 (replace):** `replace_region(body, heading="## Next Actions", region) -> str | None` returns `None` when the heading is missing. Handles: markers present; first run over the exact seeded placeholder; first run over other text (insert at top, keep text); user lines inside the region moved below the closing marker; the enclosing `jesinia:ai` block left alone. Tests for each, plus `test_should_move_user_lines_out_of_the_region` and `test_should_be_idempotent`.
- [ ] **Step 5:** Commit `feat(tasksmirror): map actions to project pages and render regions`.

### Task 4: Domain: Today

**Files:**
- Create: `server/jesinia/services/tasksmirror/domain/today.py`
- Test: `server/tests/services/tasksmirror/domain/test_today.py`

- [ ] **Step 1:** Failing tests for spec §6.1: inbox with a date included; done/trash/someday excluded; overdue/today/tomorrow grouping by local `date`; defer reached goes to Today; unmatched project as plain text; empty text; `test_should_regroup_today_across_local_midnight`.
- [ ] **Step 2:** Implement `render_today_region(lines, page_links, today)` and `ensure_today_section(body) -> str` (adds `## From DOPO Tasks` after the intro paragraph once).
- [ ] **Step 3:** Commit `feat(tasksmirror): render today's actions`.

### Task 5: Application: the mirror pass

**Files:**
- Create: `server/jesinia/services/tasksmirror/application/{__init__,ports,service}.py`
- Modify: `server/jesinia/services/kb/application/service.py` (`update_body(..., origin: str = "jesinia")`, passed through `_emit` to `emit`)
- Test: `server/tests/services/tasksmirror/application/test_service.py`, `server/tests/services/kb/application/test_service.py`

- [ ] **Step 0:** Failing KB tests `test_should_emit_given_origin_on_update_body` and `test_should_default_update_body_origin_to_jesinia`. Then add the `origin` keyword to `KbService.update_body`. Every existing caller keeps the default.
- [ ] **Step 1:** Ports: `TasksFile` (`stat() -> FileStamp | None`, `read() -> str`), `Pages` (list project pages and Today with id/title/front matter/body/rev; `update_body`), `Clock` (local now).
- [ ] **Step 2:** Failing tests over fakes: writes only changed pages; `test_should_not_write_when_regions_are_unchanged`; `StaleWrite` re-read and one retry; an unreadable file touches no page and records the cause; a page with no section is reported; `status()` shape per spec §7.
- [ ] **Step 3:** Implement `MirrorService.run_pass()` and `status()`. Every write passes `origin="tasksmirror"` (the `Pages` adapter forwards it to `update_body`). Test `test_should_write_with_tasksmirror_origin`.
- [ ] **Step 4:** Commit `feat(tasksmirror): run a mirror pass over KB pages`.

### Task 6: Infrastructure, module, API

**Files:**
- Create: `server/jesinia/services/tasksmirror/infrastructure/tasks_file.py`, `server/jesinia/services/tasksmirror/infrastructure/kb_pages.py`
- Create: `server/jesinia/services/tasksmirror/{module.py,api/__init__.py,api/routes.py}`
- Create: `server/jesinia/services/tasksmirror/migrations/0001_init.sql` (the `idempotency` table from `core/idempotency.py`)
- Modify: `server/jesinia/registry.py` (after `KbSyncModule`; add `TASKSMIRROR_DB`, class history, to `DATABASES`), import-linter contracts in `pyproject.toml`
- Test: `server/tests/services/tasksmirror/{infrastructure,api}/…`, `server/tests/services/tasksmirror/test_module.py`

- [ ] **Step 1:** `FsTasksFile` (stat by mtime_ns + size, read UTF-8). `KbPages` adapter over `KbService`: folder notes `<X>/<X>.md` under `10 Projects/`, `10 Projects/_Paused/` and `10 Projects/_Someday/`, each tagged with its KB status (`active`/`paused`/`someday`); nothing from `40 Archive/` or `20 Areas/`; Today at `01 Today/Today.md`. Tests include a paused page being mirrored and a folder that moved to `20 Areas/` no longer being written.
- [ ] **Step 2:** Module: idle and healthy when `tasks_md_path` is unset, degraded when the path is missing. Scheduler `every("tasksmirror", 15s, poll, at_start=True)`, where poll runs a pass when the stamp changes or the local date rolled over. Open `tasksmirror.db`, wire `SqliteIdempotencyStore` over it, call `clear_pending` at start, and schedule a daily `tasksmirror.idempotency-prune` job, all following `apps/rss/module.py`. Test `test_should_replay_sync_for_repeated_idempotency_key`.
- [ ] **Step 3:** Routes `GET /api/tasksmirror/status`, `POST /api/tasksmirror/sync` (owner only, idempotency key like other POSTs), and `GET`/`PUT /api/tasksmirror/path`, modeled on `PUT /api/kb/path`. The `PUT` validates the path (absolute, or empty to disconnect), persists it with `set_value`, sets `ctx.cfg = replace(ctx.cfg, tasks_md_path=…)`, and calls `module.rebind(path)`, which resets the poll stamp and runs a pass at once. Agent tokens get 403. Tests: `test_should_connect_without_restart` (PUT, then status shows connected and pages written), `test_should_disconnect_on_empty_path`, `test_should_reject_relative_path`.
- [ ] **Step 4:** `make gen-client`. Commit with the generated client: `feat(tasksmirror): add the module, poller and status API`.

### Task 7: Settings card, and Phase 1 acceptance

**Files:**
- Modify: `web/apps/launcher/src/…` (Settings page; add a `DopoTasksCard` component and test)

- [ ] **Step 1:** Failing vitest tests: Not connected state with a path field (saves via `PUT /api/tasksmirror/path` and shows the new status without a reload); connected state showing last synced, **Sync now**, and lists of unmatched, collisions, no-section pages and paused-with-next-actions pages; error shows cause then what to do.
- [ ] **Step 2:** Implement with `@jesinia/design` components only.
- [ ] **Step 3:** `make ci` green. Manual: set `tasks_md_path` to the real DOPO file; check all nine project pages and Today, then change an action in DOPO and see it within ~20 s. Note results in the PR.
- [ ] **Step 4:** Commit `feat(launcher): show DOPO Tasks mirror status in Settings`. Open the Phase 1 PR.

---

## Phase 2: completion write-back

### Task 8: Domain: completion detection and line edit

**Files:**
- Create: `server/jesinia/services/tasksmirror/domain/complete.py`
- Test: `server/tests/services/tasksmirror/domain/test_complete.py`

- [ ] **Step 1:** `ticked_ids(region_text) -> set[str]`: `- [x]`/`- [X]` rendered lines with a `<!-- dopo:… -->` trailer. Unticks ignored.
- [ ] **Step 2:** `complete_line(text, item_id) -> str | None`: on the line whose trailer id matches, set `[x]` and `@status:done` (replace or insert before `@context:`/date tokens, matching the canonical token order). Returns `None` when the id is absent or already done. Test `test_should_change_only_box_and_status_of_target_line` (byte diff of the whole file is exactly that line), plus CRLF and a trailing newline kept.
- [ ] **Step 3:** Render a pending request as `- [x] … · pending DOPO` (extend `render_project_region` and `render_today_region`).
- [ ] **Step 4:** Commit `feat(tasksmirror): detect ticks and edit one tasks.md line`.

### Task 9: Ledger and write-back loop

**Files:**
- Create: `server/jesinia/services/tasksmirror/migrations/0002_ledger.sql` (`completions`: `item_id` primary key, `base_rev` not null, `page_id`, `requested_at`, `attempts`, `state`), `server/jesinia/services/tasksmirror/infrastructure/ledger.py`
- Modify: `application/ports.py`, `application/service.py`, `infrastructure/tasks_file.py` (`swap_in(edit: Callable[[str], str | None]) -> SwapOutcome`: write the edited text to a temp file and fsync it, `renamex_np(..., RENAME_SWAP)` via ctypes, compare the displaced copy, then re-edit and re-swap on mismatch, up to 3 rounds, per spec §8.2; `SwapUnsupported` disables Phase 2 writes), `module.py`
- Test: `server/tests/services/tasksmirror/{application,infrastructure}/…`

- [ ] **Step 1:** Failing tests for the ledger transitions in spec §8.3, each comparing the line's trailer rev with the stored `base_rev`: pending → written (done, rev = base: still pending); written → re-applied when the line reverts to open with rev = base; pending → applied (done with rev ≠ base, or gone); pending → superseded (rev ≠ base, still open); pending → stale after 7 days (kept, flagged); re-apply after an overwrite (rev = base); `test_should_decide_from_stored_base_rev_after_restart` (a fresh service over the same DB); a second tick on a pending item records nothing.
- [ ] **Step 1b (swap):** Infrastructure tests on a real APFS temp dir: `test_should_keep_dopo_write_that_lands_before_swap` (a fake "DOPO" replaces the file between the edit and the swap through an injected hook; the result is DOPO's text plus Jesinia's one line), `test_should_install_dopo_text_unedited_after_three_lost_rounds`, `test_should_disable_writes_when_swap_unsupported` (inject an `ENOTSUP`). Flag as manual: the real FSA-writer interleaving with DOPO open.
- [ ] **Step 2:** Subscribe to `kb.page_changed`, ignoring origin `tasksmirror`. Test `test_should_ignore_its_own_pending_render`: a mirror write containing `- [x] … · pending DOPO` records nothing. Record requests (with the line's current trailer rev as `base_rev`) for mapped pages and Today, then run the write-back and a pass.
- [ ] **Step 3:** `status().pending_completions`; Settings lists them with "Open DOPO Tasks to apply" when stale.
- [ ] **Step 4:** Commit `feat(tasksmirror): write ticks back to DOPO Tasks`.

### Task 10: Phase 2 acceptance

- [ ] **Step 1:** `make ci` green.
- [ ] **Step 2:** Manual, with DOPO open: tick an action in Notes and confirm it is done in DOPO within ~20 s and drops off the page. With DOPO closed: tick, see "pending DOPO", open DOPO, and confirm it applies. Conflict: edit the same action in DOPO first, tick in the KB, and confirm DOPO's resolver shows it and Jesinia marks it superseded.
- [ ] **Step 3:** Re-run the Pigeonpile compat fixture with a mirror region (ADR 0007 rows a and c) and record the result.
- [ ] **Step 4:** Open the Phase 2 PR. File the two `amicus-platform` follow-ups (spec §10) if they aren't already filed.
