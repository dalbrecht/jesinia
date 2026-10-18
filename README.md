# Jesinia

Jesinia is a personal knowledge base plus a small suite of local apps that feed it. A
Python server on your Mac serves the apps as installable web apps at
`http://127.0.0.1:5374/`. It connects them to your own AI agents (Claude Code and Codex,
through their existing logins), and keeps your Markdown files on disk as the source of
truth, committed and pushed with git.

The apps:

- **Notes:** browse and search your KB, edit pages in place, talk a page through with an
  agent, and save what's worth keeping as a note with clear AI provenance.
- **RSS:** a three-pane, keyboard-driven feed reader. Keep a post to write it into the KB
  with your note, tags and the archived article; Engage to talk it through with an agent
  first. OPML import and export are in Settings → RSS.
- **Review:** a queue of the pull requests that need you, a guided walkthrough of each
  with AI findings ready before you look, and one confirmed review posted to GitHub.
  It needs the GitHub CLI signed in (`gh auth login`); Jesinia stores no credentials.
- **Issues:** triage the issues of the repos you watch with agent suggestions you approve,
  group them into epics, and build ordered work queues by drag and drop. Every other change
  to GitHub waits in a Pending changes tray until you apply it. It also uses `gh`.
- **Settings:** the KB folder, git sync, agents, the tag catalog, RSS and Issues (watched
  repos, label schema and setup). (Review's settings page comes in a later release.)

## Quickstart

You need macOS, git, [uv](https://docs.astral.sh/uv/) and Node 22+
(`brew install uv node`). Claude Code and/or Codex are optional, for chat; Review and Issues
also need `gh` (`brew install gh && gh auth login`).

```bash
git clone --recurse-submodules https://github.com/dalbrecht/jesinia.git ~/apps/jesinia
cd ~/apps/jesinia
./setup --kb-path ~/kb --auto-update
```

Setup:

1. checks your tools and builds the apps;
2. starts the server under launchd, which keeps it running and starts it at login;
3. asks before connecting Claude Code and Codex;
4. prints a one-time login link.

Open the link, then use **Install** in Chrome or Edge to give Jesinia, Notes, RSS, Review and
Issues their own windows.

- `--kb-path` is your Markdown folder (default `~/kb`). Existing files are indexed in
  place, never moved. If it is a git repository with a remote, Jesinia commits and pushes
  your changes every 4 hours.
- `--auto-update` keeps this checkout on the latest `main` (checked every 15 minutes,
  rolled back if a build fails). Leave it off in a checkout you develop in.
- `--dry-run` shows what setup would do and changes nothing.

Need a new login link later? Run `jesinia status`. Something wrong? Run
`jesinia doctor`. To remove Jesinia, run `./setup --uninstall`; your KB is never touched.

The [operations guide](docs/operations.md) covers everything else:

- what setup changes on your Mac;
- every command;
- where files live;
- updating;
- agent registration;
- uninstalling;
- troubleshooting.

## Using it with agents

After setup, Claude Code and Codex can use your KB from the terminal through Jesinia's
MCP tools (`kb_search`, `kb_read`, `kb_write`, …). The Claude Code plugin adds skills
and a `/jesinia-note` command. Writes that an agent proposes from a Notes chat wait for
your approval, with a preview of the change. See
[agents](docs/conventions/agents.md).

## Documentation

- [Operations guide](docs/operations.md): install, update, uninstall, troubleshooting.
- [Conventions](docs/README.md): the KB format, architecture, agents, sync, security
  and design system.
- [Decisions (ADRs)](docs/README.md) and the
  [foundation spec](docs/specs/2026-10-16-jesinia-foundation-design.md).

## Develop

```bash
git clone --recurse-submodules https://github.com/dalbrecht/jesinia.git ~/projects/jesinia
cd ~/projects/jesinia
make install
make ci          # lint, types, tests, coverage gates, audits, standards
make test-e2e    # Playwright against a throwaway server
```

`make help` lists every target; the Makefile is the only entry point. Adding an app?
Start with [adding an app](docs/conventions/adding-an-app.md).

## License

[AGPL-3.0-or-later](LICENSE). Every source file carries an SPDX header. If you run a
modified Jesinia for other people over a network, you must offer them the source.
