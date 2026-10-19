# Security

A one-page threat model for a single-user local server.

## What we protect

The KB, the user's agent logins and git credentials, and the user's machine.

## Boundaries and defenses

| Threat | Defense |
|---|---|
| Other machines on the network | Bind `127.0.0.1` only |
| A web page attacking the local server (CSRF, DNS rebinding) | `Host` must be the local origin (otherwise 421). Cross-origin writes get 403. The session cookie is `HttpOnly; SameSite=Strict`. CSP `default-src 'self'` (inline styles allowed for the editor), `frame-ancestors 'none'`, `X-Frame-Options: DENY`. Only `/rss/` adds `img-src 'self' https:` so feed images show (ruling R21; never `http:`, and `no-referrer` applies) |
| Other local processes | API and MCP need the install token, a session cookie or a session token. `config.toml` is mode 600. The token never appears in a URL (the login link carries a single-use nonce). Browser sessions persist in `sessions.json` (mode 600) as SHA-256 hashes only, expire after 30 days unused, and are cleared on token rotation (ADR 0010) |
| A prompt-injected agent (via a KB page or remote content) | Context is fenced as `<untrusted-data>`. Sessions are confined (no shell, no file writes, scratch working directory). Writes need approval with a preview of the real target and a diff. Agent saves are capped, and provenance can't be forged |
| A malicious path or symlink | Writes stay inside the KB, refuse any symlinked path component, and never replace other files |
| SSRF through `fetch` | http(s) only; every hop and every TCP connect must resolve to public addresses; size and decompression caps; per-host pacing |
| Remote HTML | Sanitized against an allowlist (`nh3`) before rendering |
| Leaked secrets | Jesinia stores no credentials: agents use their own logins, GitHub uses `gh` or the Keychain, git uses the user's helper. Git errors are redacted. TruffleHog runs in CI |

## Accepted risks

- The install token sits in each agent's user config (`~/.claude.json`,
  `~/.codex/config.toml`), as it does in Jesinia's `config.toml`. It grants loopback
  access only. Rotate it with `jesinia token rotate` or Settings → Security.
- Agents run with the user's own account and can read what the CLI's sandbox allows.

## Reporting

This is a personal project. Open an issue (or contact the author privately) for anything
security-related.
