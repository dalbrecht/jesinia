# ADR 0005: Drive agents through their CLIs; store no API keys

- **Status:** Accepted
- **Date:** 2026-10-16
- **Related:** foundation spec §4.3, §7.5; [agents conventions](../conventions/agents.md)

## Context

Jesinia's apps need an AI agent that can read and write the KB. The user already has
Claude Code and Codex installed and logged in. Calling model APIs directly would mean
storing API keys and re-implementing tool use.

## Decision

- Jesinia runs **`claude -p` (stream-json)** and **`codex exec --json`** as subprocesses,
  using each CLI's existing login. Jesinia stores no API keys. GitHub access uses `gh`
  or a Keychain PAT, and git uses the user's credential helper.
- Sessions launched by Jesinia are confined:
  - the working directory is a per-session scratch folder;
  - KB access goes only through Jesinia's MCP tools, with a per-session token;
  - built-in shell and file-write tools are disabled (Claude Code's allowlist, Codex's
    read-only sandbox with the shell off);
  - the environment is minimal.
- Write tools need the user's approval in the PWA unless the app pre-authorized them
  for that session (Notes' `notes_save`).
- KB and remote content given to an agent is wrapped in `<untrusted-data>` blocks.
- Terminal agents use the same MCP endpoint with the install token, registered by
  `jesinia install`.

## Consequences

- No secrets in Jesinia's config except its own local install token.
- CLI output formats can change: the adapters are tested against recorded transcripts,
  and runner availability is reported in Settings and `jesinia doctor`.
- An agent's behavior depends on the CLI version the user has installed.
