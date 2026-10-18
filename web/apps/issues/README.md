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
- **Pending changes:** every GitHub change except an approval waits in the tray until Apply
  (⌘↵). Applies, retries and approvals run in the background on the server; the tray polls each
  batch to its per-issue results, with Retry, and Abandon for a batch that creates issues.

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
