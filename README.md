# Jesinia

Jesinia is a personal knowledge base plus a small suite of local apps that feed it. A
Python server on your Mac serves the apps as installable web apps at
`http://127.0.0.1:4870/`, connects them to your own AI agents (Claude Code, Codex), and
keeps your Markdown files on disk as the source of truth.

> **Status:** under construction. The foundation is being built from
> [the spec](docs/specs/2026-10-16-jesinia-foundation-design.md) and
> [the plan](docs/superpowers/plans/2026-10-16-jesinia-foundation.md). A real quickstart
> replaces this section when `./setup` lands.

## Develop

Requirements: macOS, [uv](https://docs.astral.sh/uv/), Node 22+ with Corepack (pnpm),
and git.

```bash
git clone --recurse-submodules https://github.com/dalbrecht/jesinia.git
cd jesinia
make install
make ci
```

`make help` lists every target. The Makefile is the only entry point.

## License

[AGPL-3.0-or-later](LICENSE). Every source file carries an SPDX header.
