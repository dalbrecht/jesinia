# Adding an app

A checklist for a new app (RSS, Review, Issues, …). Read [architecture](architecture.md)
first.

1. **Spec.** Write `docs/specs/<date>-<app>-design.md`: subject, flows, data, tools,
   states (empty, loading, error, streaming, sync paused, runner unavailable, pending
   write).
2. **Module.** Create `server/jesinia/apps/<app>/` with `domain/`, `application/`,
   `infrastructure/`, `api.py` (or `api/`) and `module.py`.
   - Depend only on services, through `ctx.services`. If you need another app's code,
     promote it into a service first.
   - Add the module to `jesinia/registry.py`, after the services it needs.
3. **Database.** Declare a `ModuleDb` with its class (derived, cache or history) and
   `migrations/0001_init.sql` (with an SPDX header comment). Add it to
   `registry.DATABASES`.
4. **HTTP routes** under `/api/<app>/…`. Then run `make gen-client` so the web client is
   typed.
5. **MCP tools.** Name them `<app>_<verb>` and give them descriptions an agent can act
   on. Mark writes with `writes=True` and add a `preview`. Pre-authorize a write in a
   session only when the user's own action is that write (like Notes' "Save note").
6. **Agent context.** Build a `ContextPack`: a short role prompt, plus subject and
   remote content as `UntrustedDoc`s (fenced as `<untrusted-data>`).
7. **PWA.** Create `web/apps/<app>/` from the Notes app:
   - `AppShell` with `data-app="<app>"`;
   - a manifest with `scope: /<app>/` and `display_override: ["window-controls-overlay"]`;
   - icons from `node scripts/export-icons.mjs <app> apps/<app>/public/icons`;
   - the service worker.

   Use `@jesinia/design` components and tokens only, and register the app in
   `packages/shell/src/apps.ts`.
8. **Tests.** Python at 95% per file (domain 100%), vitest for the app, and a Playwright
   spec in `web/tests/e2e/` for the main flow.
9. **Skill.** Add a skill under `skills/claude-code/skills/` and update the Codex snippet
   if agents should know the app's tools. `server/tests/skills` checks that every tool
   name exists.
10. **Docs.** Update `docs/conventions/` if a convention changed, and add an ADR for
    decisions worth remembering.
