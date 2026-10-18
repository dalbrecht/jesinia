<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Issues PWA

The Issues app (`/issues/`): triage GitHub issues across the repos you watch, against each
repo's label convention. The design is `docs/specs/2026-10-16-issues-design.md` §6; the API it
reads and writes is `server/jesinia/apps/issues/README.md`.

- **Workspace tabs:** Inbox, Backlog, Epics and Queues, then one tab per opened issue (kept in
  local storage).
- **Inbox and Backlog:** a compact list with multi-select and a bulk bar ("Suggest with
  Claude", "Approve all"), or a board by readiness, priority (both take drops, which stage the
  label) or status (derived, read-only).
- **An issue:** the book layout, with the readiness and priority pickers, suggestions with
  Accept, Edit and Dismiss, and Approve and Dismiss all.
- **Themes:** a column of drop targets beside the list and the board, and a filter. A drop (or
  "Add theme…") is local and immediate, with Undo; **Publish…** stages the `theme:<tag>` label in
  the tray, and **To epic…** previews one batch to approve (a closed theme epic offers a new one).
- **Epics:** the epics and initiatives with progress and readiness, each a drop target and itself
  draggable, beside the open issues without an epic and the agent's group proposals. A drop stages
  one parent link per issue; a drop an epic can't take (into itself or its own child, an
  initiative into an epic, another owner's issue) is refused with the reason. An epic opens in the
  book layout: its children (a sortable list: reorders stage the sub-issue order), the brief
  editor (posted through the brief endpoints; a brief edited on GitHub asks to refresh or
  overwrite), Copy brief, the checklist conversion and the epic's proposals.
- **Queues:** section tabs with counts (each tab takes drops), flat or by priority. Order is local
  and immediate, with Undo; a move into another band stages the priority (refused while a
  priority change from the tray is staged). Done (the checkbox, `x`, or the Done zone) moves the
  item to the collapsed Done group and stages its close or agent-ready. Warnings, Copy brief, and
  the agent's proposals as a draft diff to approve.
- **Menus:** every drop target has a menu equivalent reachable by keyboard ("Move to epic…", "Add
  to queue…", "Add theme…", the queue item's More… menu, Move up and Move down).
- **Pending changes:** every GitHub change except an approval waits in the tray until Apply
  (⌘↵). Applies, retries and approvals run in the background on the server; the tray polls each
  batch to its per-issue results, with Retry, and Abandon for a batch that creates issues.
  Batches that didn't finish are read back from the server on load (`GET /api/issues/batches`).

Issue text and agent text are untrusted: Markdown goes through the shell's `SafeMarkdown` (no raw
HTML, no images) and hidden characters show as visible tokens (`VisibleText`).

## Development

```bash
make install          # once
cd web/apps/issues
pnpm dev              # Vite on its own port, proxying /api to a running Jesinia
pnpm test             # vitest with coverage (95/95/95/90)
pnpm build            # into server/jesinia/static/issues
```

The e2e flow is `web/tests/e2e/issues.spec.ts` (`make test-e2e`), against the fake GitHub in
`web/tests/e2e/fixtures/github/server.py` and the scripted `fake` runner.
