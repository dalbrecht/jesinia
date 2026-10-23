# Jesinia documentation

- **Using Jesinia:** [operations guide](operations.md) covers install, update,
  uninstall, logs, `jesinia doctor` and troubleshooting.
- **How it works:** the [conventions](conventions/):
  - [KB format](conventions/kb-format.md): the on-disk rules every tool and agent follows.
  - [Architecture](conventions/architecture.md): layering, services, databases.
  - [Adding an app](conventions/adding-an-app.md): a checklist.
  - [Agents](conventions/agents.md): runners, sessions, MCP, untrusted content.
  - [KB sync](conventions/kb-sync.md): git detection, the cycle, conflict recovery.
  - [Security](conventions/security.md): the threat model on one page.
  - [Design system](conventions/design-system.md): tokens, components, usage rules.
- **Decisions:** [ADRs](adr/):
  - [0001 AGPL license](adr/0001-agpl-license.md)
  - [0002 one server, shared services](adr/0002-single-server-shared-services.md)
  - [0003 format-only Pigeonpile interop](adr/0003-format-only-pigeonpile-interop.md)
  - [0004 files are the source of truth](adr/0004-files-are-source-of-truth.md)
  - [0005 CLI-driven agents](adr/0005-cli-driven-agents.md)
  - [0006 KB git sync](adr/0006-kb-git-sync.md)
  - [0007 Pigeonpile compatibility results](adr/0007-pigeonpile-compat-results.md)
  - [0008 design system and shell](adr/0008-design-system-and-shell.md)
  - [0009 install and operations](adr/0009-install-and-operations.md)
  - [0010 browser sessions survive restarts](adr/0010-persistent-browser-sessions.md)
  - [0011 Markdown task lines (proposed)](adr/0011-markdown-task-lines.md)
- **Design sources:** [docs/design](design/README.md), covering the brief, tokens,
  components, wireframes and icons.
- **Specs and plans:** [foundation spec](specs/2026-10-16-jesinia-foundation-design.md)
  and [foundation plan](superpowers/plans/2026-10-16-jesinia-foundation.md).
