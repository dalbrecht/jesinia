# Jesinia Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Jesinia foundation: design system and app wireframes first, then a launchd-managed local Python server with shared KB/search/sync/agent/MCP services, the `@jesinia/design` package, launcher/settings PWA, and a Notes tracer app proving the whole loop.

**Architecture:** One FastAPI process (uv) under a per-user LaunchAgent. `core ← services ← apps`; each service exposes one application-layer implementation through a Python interface, HTTP routes, and MCP tools. KB markdown files are the source of truth; each module owns one SQLite database classed derived/cache/history. React + Vite PWAs live in a separate pnpm workspace and are built into the server's static directory.

**Tech Stack:** Python ≥ 3.12, uv, FastAPI, uvicorn, Typer, PyYAML, watchfiles, httpx, nh3, `mcp` (official Python SDK), sqlite3/FTS5, pytest; TypeScript, pnpm (Corepack), React 19, Vite, vitest, @testing-library, Playwright, openapi-typescript. Tooling versions and configs come from `.standards/`.

**Spec:** `docs/specs/2026-10-16-jesinia-foundation-design.md` — read it alongside this plan.

## Global Constraints

- License: AGPL-3.0-or-later; every source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later` (comment syntax per language).
- Tooling, line lengths (ruff 100, prettier 120), strictness (`mypy --strict`, TS strict), and coverage (≥ 95% per module, domain 100% target) are defined by `.standards/`; where this plan names a tool it is because the standards do.
- The Makefile is the only entry point; CI never calls `uv`/`pnpm` directly. Python and JS toolchains never orchestrate each other.
- Layering: `core ← services ← apps`; apps never import apps; inside each module `domain ← application ← infrastructure ← api`. Enforced by import-linter.
- Server binds `127.0.0.1`, default port `4870`.
- Data dir `~/Library/Application Support/jesinia/`; logs `~/Library/Logs/jesinia/server.log`; config `~/Library/Application Support/jesinia/config.toml` mode `0600`.
- LaunchAgent label `com.jesinia.server`; `KeepAlive: {SuccessfulExit: false}`; `RunAtLoad: true`.
- KB sync default interval **4 hours**; quiet period **30 s**; skip entirely when tree clean and not ahead.
- Commit trailer exactly `Jesinia-Auto-Sync: true`.
- Reserved top-level KB names: `Journal`, `Drafts` (case-insensitive). Jesinia never writes inside them.
- Collision suffix: ` [<id-prefix>]` (8 hex chars of the id, no dashes); never ` 2`.
- Front-matter order: `id`, `title`, any foreign keys in original order, `jesinia` last.
- Provenance comments exactly: `<!-- jesinia:ai agent=<agent> model=<model> at=<ISO-8601 UTC, Z> session=<id> -->` … `<!-- /jesinia:ai -->`.
- MCP wire tool names use `_` (`kb_search`), matching `^[a-zA-Z0-9_-]{1,64}$`.
- Jesinia stores no credentials of its own (no API keys, no GitHub tokens on disk).
- Ids are UUIDv7 strings (lowercase, dashed).

## Review Focus

1. **macOS NFD filenames** — a page titled `Café` written by another tool arrives as NFD on disk; Jesinia must still match it by `id`, not create a duplicate, and must not rename it on every scan. (Task 10, Task 12)
2. **External edit racing a Jesinia write** — the user saves a page in the Pigeonpile Wiki while an agent writes the same page; Jesinia must refuse (`StaleWrite`), never clobber. (Task 11)
3. **Path escape** — `kb_write` with a folder like `../../.ssh` or a symlink inside the KB pointing outside must be rejected with `PathOutsideKb`. (Task 11)
4. **Malformed agent stream** — the CLI emits a truncated JSON line or exits non-zero mid-turn; the session becomes `failed` with the error and keeps the transcript so far. (Task 17, Task 18)
5. **Odd git state at sync time** — detached HEAD, no upstream, or a rebase already in progress; the cycle must not commit, must report the problem in status, and must leave the repo untouched. (Task 15)

---

# Phase 0 — Design (no code)

Phase 0 tasks are collaborative sessions with the user. Each produces committed documents under `docs/design/`. No `server/` or `web/` code is written until Task 6 (design sign-off) is complete. Use the Artifact tool's Design / design-system types (or plain HTML files in `docs/design/wireframes/`) for visual work; commit the source or an exported HTML snapshot plus a link.

### Task 1: Pigeonpile Wiki compatibility round-trip (spec §5.6)

**Files:**
- Create: `docs/design/compat/fixture-kb/Notes/Round Trip.md`, `docs/design/compat/fixture-kb/Notes/Plain File.md`
- Create: `docs/adr/0007-pigeonpile-compat-results.md`

- [ ] **Step 1: Write fixture pages.** `Round Trip.md` has front-matter `id` (UUIDv7), `title: Round Trip`, a foreign key `aliases: [rt]`, and a `jesinia:` block (`kind: note`, `captured: 2026-10-16T09:12:00-07:00`); its body contains a `[[id|Title]]` link, a `#tag`, and a `<!-- jesinia:ai … -->` … `<!-- /jesinia:ai -->` span. `Plain File.md` has no front-matter.
- [ ] **Step 2: With the user, open the fixture folder in the Pigeonpile Wiki**, edit one word in each page, save, and copy the resulting files back.
- [ ] **Step 3: Record results** in ADR 0007: (a) is `jesinia:` preserved; (b) is `aliases` preserved; (c) do HTML comments render hidden or as text; (d) was `Plain File.md` adopted and with what front-matter. State the decision: keep the `jesinia:` block in front-matter, or switch to the `.jesinia/meta/<id>.yaml` sidecar fallback; and keep or replace the comment marker.
- [ ] **Step 4: Commit** `git add docs/design/compat docs/adr/0007-pigeonpile-compat-results.md && git commit -m "docs: record Pigeonpile Wiki compatibility results"`

### Task 2: Design-system brief and identity

**Files:**
- Create: `docs/design/brief.md`

- [ ] **Step 1: Run an identity session with the user** covering: product personality (3–5 adjectives + anti-adjectives), reference products, density (reading-heavy RSS/review vs. dashboard-like issues), light/dark priority, motion stance.
- [ ] **Step 2: Choose typefaces** — only fonts licensed OFL/Apache/MIT (AGPL-compatible, self-hostable via `@fontsource`). Record family, weights, and the role of each (UI, reading, mono).
- [ ] **Step 3: Write `brief.md`** with the decisions above and a one-paragraph voice guide for UI copy.
- [ ] **Step 4: User sign-off**, then commit `docs: add design-system brief`.

### Task 3: Token specification

**Files:**
- Create: `docs/design/tokens.md`

- [ ] **Step 1: Define three tiers with exact names and values** (oklch for color):
  - primitives: `--jx-<hue>-<step>` ramps (steps 50–950) for neutral + one accent + success/warn/danger.
  - semantic: `--jx-bg`, `--jx-surface`, `--jx-surface-raised`, `--jx-fg`, `--jx-fg-muted`, `--jx-fg-subtle`, `--jx-border`, `--jx-accent`, `--jx-accent-fg`, `--jx-focus`, `--jx-success`, `--jx-warn`, `--jx-danger`, `--jx-ai` (subtle marker for agent-authored UI, not KB content); each with light and dark values.
  - component tokens for the eight components of Task 4 (e.g. `--jx-button-bg`, `--jx-chat-bubble-agent-bg`).
  - spacing `--jx-space-1…8`, radius `--jx-radius-sm|md|lg`, shadow `--jx-shadow-1|2`, type scale `--jx-text-xs…2xl` with line heights, font stacks `--jx-font-ui|read|mono`, motion `--jx-duration-fast|base`, `--jx-ease`.
- [ ] **Step 2: Contrast table** — every semantic fg/bg pair the components use, with computed WCAG ratio in both themes; all body pairs ≥ 4.5:1, large/UI ≥ 3:1. This table becomes the fixture for Task 22's contrast test.
- [ ] **Step 3: Render a token sheet** (Artifact or `docs/design/tokens.html`) for the user to see both themes; iterate to sign-off; commit `docs: add design tokens spec`.

### Task 4: Component specifications

**Files:**
- Create: `docs/design/components.md`

- [ ] **Step 1: For each of Button, Input, Panel, List/Row, Badge, Tabs, Toast, ChatTranscript** document: anatomy, variants (Button: primary/secondary/ghost/danger; sizes sm/md), states (default/hover/active/focus-visible/disabled/loading), the tokens it consumes, keyboard behavior, ARIA roles, and the React props signature (e.g. `Button({variant, size, loading, ...ButtonHTMLAttributes})`).
- [ ] **Step 2: ChatTranscript specifics:** message kinds (user, agent streaming, agent done, tool-use collapsed row, error, pending-write approval card with Approve/Reject), auto-scroll rule (stick to bottom unless the user scrolled up), live-region announcement policy.
- [ ] **Step 3: User sign-off; commit** `docs: add component specs`.

### Task 5: Wireframes for every app

**Files:**
- Create: `docs/design/wireframes/README.md` (index + links)
- Create: `docs/design/wireframes/{launcher,settings,notes,rss,review,issues}.html` (or Artifact links recorded in the README)

- [ ] **Step 1: Foundation screens** (desktop ≥ 1200 px and narrow 390 px): launcher (app tiles, sync banner slot, runner status); settings (KB path field + validate, git mode + last sync + Sync now/Resume/Initialize git, runners, conflicts list, token rotate); Notes (search → page view → chat pane → Save note, pending-write card).
- [ ] **Step 2: Product screens** (lower fidelity, inform later specs): RSS (feed list, item list, reader, Engage chat → save); Review (PR picker, background pack, narrative walkthrough beside diff, metrics scoring, quiz, recommendation); Issues (board, list, triage pane with agent suggestions).
- [ ] **Step 3: Shared shell decisions** recorded in README: navigation pattern between apps, where the sync/conflict banner lives, how the chat pane docks (right rail vs. bottom sheet on narrow).
- [ ] **Step 4: Walk the user through each; revise; commit** `docs: add app wireframes`.

### Task 6: Design sign-off gate

**Files:**
- Create: `docs/adr/0008-design-system-and-shell.md`

- [ ] **Step 1: ADR** summarizing brief, token tiers, component set, shell pattern, and anything from Tasks 1–5 that changes the spec (update the spec in the same commit if so).
- [ ] **Step 2: Explicit user approval recorded in the ADR status ("Accepted").** Commit `docs: accept design system and shell ADR`. Code phases start only after this.

---

# Phase 1 — Repository and core

### Task 7: Repository scaffold, standards, CI

**Files:**
- Create: `LICENSE` (AGPL-3.0 full text), `README.md` (stub: what/quickstart placeholder replaced in Task 27), `.gitignore`, `Makefile`, `server/pyproject.toml`, `server/jesinia/__init__.py`, `server/tests/test_smoke.py`, `web/package.json`, `web/pnpm-workspace.yaml`, `scripts/check_spdx.py`, `server/tests/test_spdx.py`, `.github/workflows/ci.yml`
- Create (via installer): `.standards/` submodule, `.standards.yml`, `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`

**Interfaces:**
- Produces: make targets `install build test test-e2e lint fmt fmt-check coverage-check hooks ci-py ci-js ci lint-standards sync-standards doctor`; `scripts/check_spdx.py` exits non-zero listing files missing the header.

- [ ] **Step 1: Install coding standards** via the c65llc installer into the repo with languages `python, typescript` and agents `claude-code, codex, copilot`; confirm `.standards.yml` lists them. Append a "Jesinia specifics" section to the generated `CLAUDE.md`/`AGENTS.md` pointing at `docs/conventions/` and the spec.
- [ ] **Step 2: Write failing test** `server/tests/test_spdx.py`:

```python
def test_reports_missing_header(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "b.py").write_text("# SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert missing_spdx(tmp_path, suffixes={".py"}) == [tmp_path / "a.py"]
```

- [ ] **Step 3: Implement `missing_spdx(root: Path, suffixes: set[str]) -> list[Path]`** in `scripts/check_spdx.py` (header must appear in the first 3 lines; skips `.standards/`, `node_modules/`, `.venv/`, `dist/`, `static/`). CLI entry checks `.py .ts .tsx .css .sh` plus `setup`.
- [ ] **Step 4: pyproject + Makefile** per standards (ruff/mypy/pytest-cov configs copied from `lang-01`); `ci-py` = fmt-check + lint + mypy + import-linter + pytest with coverage gate + spdx; `ci-js` = prettier check + eslint + tsc + vitest + build; `ci` = both + `lint-standards`.
- [ ] **Step 5: Run** `make ci` — Expected: PASS on the skeleton.
- [ ] **Step 6: CI workflow** runs `make ci` on ubuntu for PRs; checks out submodules. Opt in the standards-review PR workflow via `.standards/scripts/setup.sh --workflow`. Commit `chore: scaffold repo with coding standards, AGPL license, CI`.

### Task 8: Config, paths, and per-module databases

**Files:**
- Create: `server/jesinia/core/config.py`, `server/jesinia/core/paths.py`, `server/jesinia/core/db.py`
- Test: `server/tests/core/test_config.py`, `server/tests/core/test_db.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Config: port: int = 4870; kb_path: Path | None = None; kbsync_interval: timedelta = timedelta(hours=4); kbsync_quiet: timedelta = timedelta(seconds=30); token: str; data_dir: Path; log_dir: Path; folders: Mapping[str, str] = {"note": "Notes"}`
  - `load_config(path: Path) -> Config` (creates file with a fresh `secrets.token_urlsafe(32)` token if absent, chmod 0600), `save_config(cfg: Config, path: Path) -> None`, `set_value(path: Path, key: str, raw: str) -> Config` for keys `server.port`, `kb.path`, `kbsync.interval`, `kbsync.quiet`, `folders.<kind>`; durations parse `"4h"`, `"30s"`, `"15m"`. Raises `ConfigError` on unknown key/bad value.
  - `default_data_dir() -> Path`, `default_log_dir() -> Path`, `config_path() -> Path` (respect `JESINIA_HOME` env override for tests and setup smoke).
  - `class DbClass(StrEnum): DERIVED = "derived"; CACHE = "cache"; HISTORY = "history"`; `@dataclass(frozen=True) class ModuleDb: name: str; db_class: DbClass; migrations: Traversable`; `open_db(data_dir: Path, spec: ModuleDb) -> sqlite3.Connection` (WAL, foreign_keys on, applies `NNNN_*.sql` in order, tracks `schema_version`); `MigrationError`.

- [ ] **Step 1: Failing tests:**

```python
def test_defaults_and_token_created_0600(tmp_path):
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.port == 4870 and cfg.kbsync_interval == timedelta(hours=4)
    assert len(cfg.token) >= 43
    assert stat.S_IMODE((tmp_path / "config.toml").stat().st_mode) == 0o600

def test_set_value_parses_durations(tmp_path):
    p = tmp_path / "config.toml"; load_config(p)
    assert set_value(p, "kbsync.interval", "15m").kbsync_interval == timedelta(minutes=15)
    with pytest.raises(ConfigError): set_value(p, "kbsync.interval", "soon")
    with pytest.raises(ConfigError): set_value(p, "nope", "1")

def test_migrations_apply_once_in_order(tmp_path, fake_migrations):
    conn = open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, fake_migrations))
    assert conn.execute("select max(version) from schema_version").fetchone()[0] == 2
    open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, fake_migrations))  # idempotent

def test_failed_migration_raises_and_rolls_back(tmp_path, broken_migrations):
    with pytest.raises(MigrationError): open_db(tmp_path, ModuleDb("t", DbClass.DERIVED, broken_migrations))
```

- [ ] **Step 2: Run** `make test` — Expected: FAIL (imports).
- [ ] **Step 3: Implement** the three files (tomllib to read; write TOML by hand-rolled serializer for the flat known keys — no extra dependency).
- [ ] **Step 4: Run** `make test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): config, paths, per-module sqlite with migrations`.

### Task 9: Event bus, scheduler, module protocol, app factory, auth

**Files:**
- Create: `server/jesinia/core/events.py`, `server/jesinia/core/scheduler.py`, `server/jesinia/core/module.py`, `server/jesinia/core/app.py`, `server/jesinia/core/auth.py`, `server/jesinia/core/security.py`
- Test: `server/tests/core/test_events.py`, `test_scheduler.py`, `test_app.py`, `test_auth.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Event: topic: str; payload: Mapping[str, object]`; `class EventBus: subscribe(topic: str, handler: Callable[[Event], Awaitable[None]]) -> None; async publish(event: Event) -> None` (handler exceptions logged, not propagated).
  - `class Scheduler: every(name: str, interval: timedelta, job: Callable[[], Awaitable[None]]) -> None; async start() -> None; async stop() -> None; async run_now(name: str) -> None`.
  - `@dataclass class AppContext: cfg: Config; bus: EventBus; scheduler: Scheduler; services: dict[str, object]`.
  - `class Module(Protocol): name: str; def router(self) -> APIRouter | None; def mcp_tools(self) -> Sequence[ToolDef]; async def start(self, ctx: AppContext) -> None; async def stop(self) -> None; def health(self) -> ModuleHealth`; `ModuleHealth(ok: bool, detail: str)`.
  - `ToolDef(name: str, description: str, input_model: type[BaseModel], handler: Callable[[BaseModel, CallContext], Awaitable[object]], writes: bool)`; `CallContext(session_id: str | None)` (defined here so services can declare tools before Task 16 mounts them).
  - `create_app(cfg: Config, modules: Sequence[Module]) -> FastAPI` — starts modules in order on lifespan startup, stops in reverse; a module whose `start` raises is marked unhealthy and skipped, others continue. `GET /healthz` → `{"ok": bool, "modules": {name: {"ok", "detail"}}}` (unauthenticated).
  - `auth.py`: `issue_login_nonce(ttl: timedelta = timedelta(minutes=5)) -> str`; `GET /login?nonce=…` sets cookie `jesinia_session` (`HttpOnly; SameSite=Strict; Path=/`) and redirects to `/`; nonce single-use. `require_user` FastAPI dependency accepts that cookie or `Authorization: Bearer <cfg.token>`; returns `Principal(kind: Literal["user","session"], session_id: str | None)`. Session-scoped bearer tokens are registered via `register_session_token(token: str, session_id: str)` (used in Task 18).
  - `security.py`: middleware rejecting (`421`) requests whose `Host` is not `127.0.0.1:<port>`/`localhost:<port>`, and (`403`) state-changing requests with an `Origin` that is not the same; sets CSP `default-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'`.

- [ ] **Step 1: Failing tests** (pytest + httpx `ASGITransport`):

```python
async def test_failing_module_does_not_block_others(cfg):
    app = create_app(cfg, [Exploding("a"), Healthy("b")])
    async with lifespan_client(app) as c:
        body = (await c.get("/healthz")).json()
    assert body["modules"]["a"]["ok"] is False and body["modules"]["b"]["ok"] is True

async def test_rejects_foreign_host(client):
    assert (await client.get("/healthz", headers={"Host": "evil.example:4870"})).status_code == 421

async def test_nonce_login_sets_cookie_once(client):
    n = issue_login_nonce()
    r = await client.get(f"/login?nonce={n}", follow_redirects=False)
    assert "jesinia_session" in r.cookies and "httponly" in r.headers["set-cookie"].lower()
    assert (await client.get(f"/login?nonce={n}")).status_code == 401

async def test_api_requires_auth(client):
    assert (await client.get("/api/ping")).status_code == 401
    assert (await client.get("/api/ping", headers=bearer(cfg.token))).status_code == 200

async def test_scheduler_runs_and_stops(): ...  # every(…, 10ms) runs ≥ 2 times in 50ms; no runs after stop()
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Add import-linter contracts** to `server/pyproject.toml`: layers `jesinia.apps | jesinia.services | jesinia.core`; independence contract across `jesinia.apps.*`; per-module layers `api | infrastructure | application | domain`. Run `make lint` — PASS.
- [ ] **Step 6: Commit** `feat(core): event bus, scheduler, module host, auth, local-origin guard`.

---

# Phase 2 — KB service

### Task 10: KB domain — front-matter, classification, paths

**Files:**
- Create: `server/jesinia/services/kb/domain/{page.py,frontmatter.py,paths.py,provenance.py}`
- Test: `server/tests/services/kb/domain/test_frontmatter.py`, `test_paths.py`, `test_provenance.py`

**Interfaces:**
- Produces:
  - `PageId = NewType("PageId", str)`; `new_page_id() -> PageId` (UUIDv7).
  - `@dataclass(frozen=True) class Page: id: PageId; title: str; path: PurePosixPath; body: str; jesinia: Mapping[str, object]; foreign: Mapping[str, object]` (`path` relative to KB root, NFC).
  - `ParsedDoc(kind: Literal["page", "plain", "foreign"], frontmatter: dict[str, object] | None, body: str)`; `parse_document(text: str) -> ParsedDoc` (`page` = fence with `id`; `plain` = no fence; `foreign` = fence without `id` or unparsable YAML).
  - `serialize_page(page: Page) -> str` (order per Global Constraints); `title_scalar(title: str) -> str` (bare iff `yaml.safe_load(f"t: {title}")["t"] == title` and no newline, else `json.dumps(title)`).
  - `sanitize_title(title: str) -> str` (NFC; replace `/ \ :` and control chars with space; collapse spaces; strip trailing dots/spaces; empty → `Untitled`).
  - `filename_for(title: str, id: PageId, siblings: Iterable[tuple[str, PageId]]) -> str` — case-insensitive collision; the id-ordered first keeps the bare name, others get ` [<first 8 hex of id without dashes>]`.
  - `is_hidden(rel: PurePosixPath) -> bool` (any segment starting `.`), `is_reserved(rel: PurePosixPath) -> bool` (first segment casefold in `{"journal","drafts"}`), `is_page_candidate(rel) -> bool` (`.md`, not hidden, not under `_assets/`).
  - `wrap_ai(text: str, *, agent: str, model: str, at: datetime, session: str) -> str`.
  - `split_document(text: str) -> tuple[str | None, str]` — the raw front-matter block (from the opening `---` through the closing `---` line, newline included) exactly as on disk, and the body. `replace_body(text: str, body: str) -> str` returns the raw block unchanged plus the new body. Ordinary editor saves use this path, so user YAML (flow style, comments, quoting, key order) is never re-serialized (ADR 0008 §5).
  - `retitle_links(body: str, id: PageId, title: str) -> str` — updates the `Title` part of every `[[<id>|…]]` for that id; other links untouched.

- [ ] **Step 1: Failing tests:**

```python
def test_round_trip_preserves_foreign_keys_and_order():
    text = "---\nid: 0192f7a8-0000-7000-8000-000000000001\ntitle: Round Trip\naliases: [rt]\njesinia:\n  kind: note\n---\nBody\n"
    doc = parse_document(text); page = page_from(doc, PurePosixPath("Notes/Round Trip.md"))
    out = serialize_page(page)
    assert out.splitlines()[1:4] == ["id: 0192f7a8-0000-7000-8000-000000000001", "title: Round Trip", "aliases:"]
    assert parse_document(out).frontmatter["aliases"] == ["rt"]

@pytest.mark.parametrize("title,expected", [("Plain", "Plain"), ("yes", '"yes"'), ("a: b", '"a: b"'), ("123", '"123"'), ("#x", '"#x"')])
def test_title_scalar(title, expected): assert title_scalar(title) == expected

def test_classify(): 
    assert parse_document("no fence\n").kind == "plain"
    assert parse_document("---\nlayout: post\n---\nx").kind == "foreign"
    assert parse_document("---\n: : bad\n---\nx").kind == "foreign"

def test_sanitize_and_nfc():
    assert sanitize_title("a/b: c.. ") == "a b c"
    assert sanitize_title("Café") == "Café"
    assert sanitize_title("  ") == "Untitled"

def test_collision_suffix_is_bracketed_id_prefix():
    a, b = PageId("0192f7a8-0000-7000-8000-00000000000a"), PageId("0192f7a8-0000-7000-8000-00000000000b")
    assert filename_for("Note", a, [("note", b)]) == "Note.md"
    assert filename_for("Note", b, [("Note", a)]) == "Note [0192f7a8].md"

def test_reserved_and_hidden():
    assert is_reserved(PurePosixPath("journal/x.md")) and not is_reserved(PurePosixPath("Notes/Journal/x.md"))
    assert is_hidden(PurePosixPath("a/.obsidian/x.md"))

def test_replace_body_keeps_raw_frontmatter_bytes():
    text = "---\nid: 0192f7a8-0000-7000-8000-000000000001\ntitle: 'Quoted'   # note\naliases: [rt, \"x\"]\njesinia: {kind: note}\n---\nOld body\n"
    raw, body = split_document(text)
    out = replace_body(text, "New body\n")
    assert out == raw + "New body\n" and out.startswith(text[: len(raw)])

def test_retitle_links_only_touches_that_id():
    a, b = "0192f7a8-0000-7000-8000-00000000000a", "0192f7a8-0000-7000-8000-00000000000b"
    body = f"See [[{a}|Old]] and [[{b}|Other]]."
    assert retitle_links(body, PageId(a), "New") == f"See [[{a}|New]] and [[{b}|Other]]."

def test_wrap_ai_exact():
    out = wrap_ai("Hi", agent="claude-code", model="claude-opus-5-5", at=datetime(2026,10,16,9,20,tzinfo=UTC), session="s1")
    assert out == "<!-- jesinia:ai agent=claude-code model=claude-opus-5-5 at=2026-10-16T09:20:00Z session=s1 -->\nHi\n<!-- /jesinia:ai -->"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (PyYAML `safe_load`; serialize foreign/jesinia with `yaml.safe_dump(sort_keys=False, allow_unicode=True)`). **Step 4: Run** — PASS, domain coverage 100%.
- [ ] **Step 5: If Task 1 chose the sidecar fallback**, `serialize_page` omits `jesinia` and Task 11 writes `.jesinia/meta/<id>.yaml`; add that test now. Commit `feat(kb): front-matter codec, path rules, provenance`.

### Task 11: KbService — read, write, move, atomic writes, index

**Files:**
- Create: `server/jesinia/services/kb/application/service.py`, `server/jesinia/services/kb/application/errors.py`, `server/jesinia/services/kb/infrastructure/{fs.py,index.py}`, `server/jesinia/services/kb/migrations/0001_init.sql`
- Test: `server/tests/services/kb/test_service.py`

**Interfaces:**
- Consumes: Task 8 `open_db`, `DbClass.DERIVED`; Task 9 `EventBus`; Task 10 domain.
- Produces:
  - `PageDraft(id: PageId | None, title: str, folder: PurePosixPath, body: str, jesinia: Mapping[str, object] = {})`.
  - `class KbService: async read(id: PageId) -> PageWithRev; async write(draft: PageDraft, *, expected_rev: str | None = None) -> PageWithRev; async update_body(id: PageId, body: str, *, expected_rev: str) -> PageWithRev; async move(id: PageId, folder: PurePosixPath, title: str | None = None) -> MoveResult; async tree() -> list[TreeNode]; resolve(id: PageId) -> PurePosixPath | None; root: Path | None`.
  - `update_body` writes via `replace_body` (raw front matter byte-identical) with the same atomic, rev-checked path as `write`; `write` (full re-serialize) is used only for new pages and explicit front-matter edits.
  - `MoveResult(page: PageWithRev, relinked: list[PageId])` — when `title` changes, every page linking to `id` gets `retitle_links` applied through `update_body` (each rev-checked; a stale linker is reported in `relinked` as skipped via `kb.page_changed` and left for the next scan, never overwritten). Links resolve by id, so a folder-only move needs no relinking.
  - `TreeNode(path: PurePosixPath, kind: Literal["folder", "page"], title: str | None, id: PageId | None, pages: int, children: list[TreeNode])` — from the index, hidden and reserved paths excluded, folders sorted before pages, case-insensitive.
  - `PageWithRev(page: Page, rev: str)` — `rev` = `sha256` hex of file bytes.
  - Errors: `KbUnavailable`, `PageNotFound`, `StaleWrite`, `PathOutsideKb`, `ReservedPath`.
  - Events: `Event("kb.page_changed", {"id", "path", "change": "created"|"updated"|"moved"|"missing", "origin": "jesinia"|"external"})`.
  - `kb.sqlite` tables: `pages(id PK, path UNIQUE, rev, title, mtime)`, `conflicts(id, kind, paths_json, detected_at)`.

- [ ] **Step 1: Failing tests:**

```python
async def test_write_creates_file_in_corpus_format(kb):
    p = await kb.write(PageDraft(None, "Hello", PurePosixPath("Notes"), "Body"))
    text = (kb.root / "Notes/Hello.md").read_text()
    assert text.startswith(f"---\nid: {p.page.id}\ntitle: Hello\n")

async def test_stale_write_refused_after_external_edit(kb):
    p = await kb.write(PageDraft(None, "A", PurePosixPath("Notes"), "v1"))
    (kb.root / "Notes/A.md").write_text((kb.root / "Notes/A.md").read_text().replace("v1", "user"))
    with pytest.raises(StaleWrite):
        await kb.write(PageDraft(p.page.id, "A", PurePosixPath("Notes"), "agent"), expected_rev=p.rev)
    assert "user" in (kb.root / "Notes/A.md").read_text()

@pytest.mark.parametrize("folder", ["../x", "Notes/../../x", "/etc"])
async def test_path_escape_rejected(kb, folder):
    with pytest.raises(PathOutsideKb): await kb.write(PageDraft(None, "x", PurePosixPath(folder), ""))

async def test_symlink_escape_rejected(kb, tmp_path):
    (kb.root / "link").symlink_to(tmp_path)
    with pytest.raises(PathOutsideKb): await kb.write(PageDraft(None, "x", PurePosixPath("link"), ""))

async def test_reserved_rejected(kb):
    with pytest.raises(ReservedPath): await kb.write(PageDraft(None, "x", PurePosixPath("Journal"), ""))

async def test_title_change_moves_file_and_keeps_id(kb, events):
    p = await kb.write(PageDraft(None, "Old", PurePosixPath("Notes"), "b"))
    q = await kb.write(PageDraft(p.page.id, "New", PurePosixPath("Notes"), "b"), expected_rev=p.rev)
    assert not (kb.root / "Notes/Old.md").exists() and (kb.root / "Notes/New.md").exists()
    assert q.page.id == p.page.id and events[-1].payload["change"] == "moved"

async def test_update_body_preserves_noncanonical_frontmatter(kb):
    made = await kb.write(PageDraft(None, "Odd", PurePosixPath("Notes"), "v1\n"))
    raw = f"---\nid: {made.page.id}\ntitle: 'Odd'  # keep me\naliases: [rt]\n---\n"
    (kb.root / "Notes/Odd.md").write_text(raw + "v1\n")  # user reformats the YAML in another editor
    p = await kb.read(made.page.id)                         # rev = hash of the current bytes
    await kb.update_body(p.page.id, "v2\n", expected_rev=p.rev)
    assert (kb.root / "Notes/Odd.md").read_text() == raw + "v2\n"

async def test_update_body_stale_refused(kb):
    p = await kb.write(PageDraft(None, "S", PurePosixPath("Notes"), "v1"))
    (kb.root / "Notes/S.md").write_text((kb.root / "Notes/S.md").read_text().replace("v1", "user"))
    with pytest.raises(StaleWrite): await kb.update_body(p.page.id, "mine", expected_rev=p.rev)

async def test_rename_retitles_inbound_links(kb):
    a = await kb.write(PageDraft(None, "Target", PurePosixPath("Notes"), "t"))
    b = await kb.write(PageDraft(None, "Linker", PurePosixPath("Notes"), f"see [[{a.page.id}|Target]]"))
    r = await kb.move(a.page.id, PurePosixPath("Notes"), title="Renamed")
    assert f"[[{a.page.id}|Renamed]]" in (await kb.read(b.page.id)).page.body and r.relinked == [b.page.id]

async def test_tree_counts_and_order(kb):
    await kb.write(PageDraft(None, "B", PurePosixPath("Notes"), "")); await kb.write(PageDraft(None, "A", PurePosixPath("Notes/Sub"), ""))
    notes = next(n for n in await kb.tree() if n.path == PurePosixPath("Notes"))
    assert notes.pages == 2 and [c.kind for c in notes.children] == ["folder", "page"]

async def test_unavailable_when_no_kb_path(kb_unset):
    with pytest.raises(KbUnavailable): await kb_unset.read(PageId("x"))
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**: `fs.atomic_write(path: Path, data: bytes)` (temp file in same dir, `fsync`, `os.replace`, `fsync` dir); containment check = `resolved.is_relative_to(root.resolve())` after resolving symlinks; `expected_rev` compared to current file hash immediately before replace (check-then-replace under a per-page `asyncio.Lock`); new pages with `expected_rev=None` refuse to overwrite an existing different-id file (collision → suffix). Index updates in the same call; publish event after replace.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(kb): KbService with atomic, rev-checked, contained writes`.

### Task 12: KB scan, adoption, watcher, conflicts; kb routes and MCP tools

**Files:**
- Create: `server/jesinia/services/kb/application/scan.py`, `server/jesinia/services/kb/infrastructure/watcher.py`, `server/jesinia/services/kb/api.py`, `server/jesinia/services/kb/module.py`
- Test: `server/tests/services/kb/test_scan.py`, `test_watcher.py`, `test_api.py`

**Interfaces:**
- Consumes: Task 11.
- Produces:
  - `async scan(svc: KbService) -> ScanReport`; `ScanReport(indexed: int, adopted: list[PurePosixPath], missing: list[PageId], duplicates: list[DuplicateId], foreign_skipped: int)`; `DuplicateId(id: PageId, paths: list[PurePosixPath])`.
  - `KbWatcher(root: Path, on_paths: Callable[[set[Path]], Awaitable[None]], debounce_ms: int = 500)`; `last_activity: datetime` (consumed by kbsync quiet period).
  - `KbModule(Module)` name `"kb"`; routes `GET /api/kb/pages/{id}` (page + `rev`), `PUT /api/kb/pages` (body PageDraft + expected_rev; new pages and front-matter edits), `PATCH /api/kb/pages/{id}/body {body, expected_rev}` (editor autosave via `update_body`; `409 {rev, body}` on `StaleWrite`), `POST /api/kb/pages/{id}/move {folder, title?}` (returns `relinked`), `GET /api/kb/tree`, `GET /api/kb/conflicts`, `POST /api/kb/scan`, `PUT /api/kb/path` (validate + persist `kb.path`, restart watcher, rescan, publish `Event("kb.root_changed", {"path"})`).
  - MCP tools: `kb_read {id}`, `kb_write {id?, title, folder, body, jesinia?, expected_rev?}` (`writes=True`), `kb_move` (`writes=True`), `kb_tree {}` → folders with page counts, `kb_link {id}` → `{id, title, path}`.

- [ ] **Step 1: Failing tests:**

```python
async def test_plain_file_adopted_with_filename_title(kb):
    (kb.root / "Inbox").mkdir(); (kb.root / "Inbox/My Idea.md").write_text("text\n")
    report = await scan(kb)
    doc = parse_document((kb.root / "Inbox/My Idea.md").read_text())
    assert report.adopted == [PurePosixPath("Inbox/My Idea.md")] and doc.frontmatter["title"] == "My Idea"

async def test_foreign_frontmatter_untouched(kb):
    (kb.root / "post.md").write_text("---\nlayout: post\n---\nx"); before = (kb.root / "post.md").read_bytes()
    await scan(kb); assert (kb.root / "post.md").read_bytes() == before

async def test_external_rename_is_move_not_new_page(kb):
    p = await kb.write(PageDraft(None, "A", PurePosixPath("Notes"), "b"))
    (kb.root / "Notes/A.md").rename(kb.root / "Notes/B.md")
    await scan(kb); assert kb.resolve(p.page.id) == PurePosixPath("Notes/B.md")

async def test_nfd_filename_matches_by_id(kb):
    p = await kb.write(PageDraft(None, "Café", PurePosixPath("Notes"), "b"))
    nfd = unicodedata.normalize("NFD", "Notes/Café.md")
    os.rename(kb.root / "Notes/Café.md", kb.root / nfd)
    r1 = await scan(kb); r2 = await scan(kb)
    assert r1.adopted == [] and kb.resolve(p.page.id) is not None and r2.indexed == r1.indexed

async def test_vanished_file_reported_never_deleted(kb):
    p = await kb.write(PageDraft(None, "A", PurePosixPath("Notes"), "b")); (kb.root / "Notes/A.md").unlink()
    assert (await scan(kb)).missing == [p.page.id]

async def test_duplicate_ids_reported_not_deleted(kb):
    p = await kb.write(PageDraft(None, "A", PurePosixPath("Notes"), "b"))
    shutil.copy(kb.root / "Notes/A.md", kb.root / "Notes/A copy.md")
    assert len((await scan(kb)).duplicates[0].paths) == 2 and (kb.root / "Notes/A copy.md").exists()

async def test_watcher_emits_external_change(kb_running, events): ...  # edit file → kb.page_changed origin=external within 2s

async def test_patch_body_returns_409_with_current_on_stale(client, kb): ...  # external edit → PATCH with old rev → 409 {rev, body}; file unchanged
async def test_tree_route_and_mcp_tool_match(client, mcp): ...  # GET /api/kb/tree == kb_tree result
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (NFC-normalize every observed path before comparison; adoption uses `KbService.write` with `expected_rev` of the plain file's hash). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(kb): scan, adoption, watcher, conflicts, routes and MCP tools`.

---

# Phase 3 — Search and sync

### Task 13: Search service

**Files:**
- Create: `server/jesinia/services/search/{application/service.py,infrastructure/fts.py,api.py,module.py,migrations/0001_init.sql}`
- Test: `server/tests/services/search/test_search.py`

**Interfaces:**
- Consumes: `kb.page_changed`, `kb.root_changed` events; `KbService.read`.
- Produces:
  - `ExternalRecord(source: str, key: str, title: str, body: str, url: str | None)`.
  - `SearchHit(source: str, key: str, title: str, snippet: str, path: str | None, authoritative: bool, score: float)`.
  - `class SearchService: async query(q: str, *, limit: int = 20, sources: set[str] | None = None) -> list[SearchHit]; async register(rec: ExternalRecord) -> None; async unregister(source: str, key: str) -> None; async reindex() -> int`.
  - KB pages indexed with `source="kb"`, `key=id`, `authoritative=True`. FTS5 `porter unicode61 remove_diacritics 2`; snippet via `snippet()`.
  - Route `GET /api/search?q=&limit=&source=`; MCP `search_query`, also aliased as `kb_search` (KB-only filter).
  - Reindex on startup runs as a background task; `/healthz` reports `"indexing"` detail while running.

- [ ] **Step 1: Failing tests:**

```python
async def test_finds_kb_page_and_marks_authoritative(env):
    await env.kb.write(PageDraft(None, "Quantum Notes", PurePosixPath("Notes"), "entanglement basics"))
    hits = await env.search.query("entanglement")
    assert hits[0].title == "Quantum Notes" and hits[0].authoritative and hits[0].source == "kb"

async def test_external_records_separate_and_filterable(env):
    await env.search.register(ExternalRecord("rss", "i1", "Feed item", "entanglement news", "https://x"))
    assert {h.source for h in await env.search.query("entanglement")} >= {"rss"}
    assert all(h.source == "kb" for h in await env.search.query("entanglement", sources={"kb"}))

async def test_diacritics_insensitive(env): ...  # "cafe" finds "Café"
async def test_startup_reindex_does_not_block_healthz(env_2000_pages): ...  # /healthz responds < 200ms while reindexing
async def test_fts_syntax_in_query_is_escaped(env): assert await env.search.query('"unbalanced') == []
```

- [ ] **Step 2–4:** FAIL → implement (quote user input as FTS phrase tokens) → PASS.
- [ ] **Step 5: Commit** `feat(search): FTS5 search over KB and registered records`.

### Task 14: kbsync — git runner and detection

**Files:**
- Create: `server/jesinia/services/kbsync/infrastructure/git.py`, `server/jesinia/services/kbsync/domain/state.py`, `server/jesinia/services/kbsync/domain/message.py`
- Test: `server/tests/services/kbsync/test_detect.py`, `test_message.py`; fixture `server/tests/services/kbsync/conftest.py` (temp work repo + local bare remote)

**Interfaces:**
- Produces:
  - `class GitRunner: __init__(repo: Path); async run(*args: str, check: bool = True) -> GitResult` (`GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`); `GitResult(code: int, out: str, err: str)`; `GitError`.
  - `GitState(mode: Literal["off","commit","push"], branch: str | None, upstream: str | None, ahead: int, behind: int, dirty: bool, problem: str | None)`; `async detect(git: GitRunner | None, root: Path) -> GitState`. `problem` is set for: `"detached-head"`, `"rebase-in-progress"`, `"merge-in-progress"`, `"no-upstream"` (only when a remote exists → mode `"commit"` with problem text "add a remote/upstream to enable push"). `ahead/behind` from `git rev-list --left-right --count @{u}...HEAD` without fetching.
  - `StatusEntry(code: Literal["A","M","D","R"], path: str)`; `summarize_changes(entries: Sequence[StatusEntry]) -> str` → `kb: <n> added, <n> updated, <n> deleted, <n> renamed (<scopes>)` omitting zero counts in that order; scopes = distinct top-level dirs as `<dir>/…` (root files by name), at most 3, then `, …`. `commit_message(entries) -> str` = summary + `\n\nJesinia-Auto-Sync: true\n`.

- [ ] **Step 1: Failing tests:**

```python
async def test_modes(tmp_path, repo_with_remote, repo_no_remote):
    assert (await detect(None, tmp_path)).mode == "off"
    assert (await detect(GitRunner(repo_no_remote), repo_no_remote)).mode == "commit"
    assert (await detect(GitRunner(repo_with_remote), repo_with_remote)).mode == "push"

async def test_detached_head_reported(repo_with_remote):
    await GitRunner(repo_with_remote).run("checkout", "--detach")
    assert (await detect(GitRunner(repo_with_remote), repo_with_remote)).problem == "detached-head"

def test_summary():
    e = [StatusEntry("M", "Sources/a.md"), StatusEntry("M", "Sources/b.md"), StatusEntry("M", "Notes/c.md"), StatusEntry("A", "Sources/d.md")]
    assert summarize_changes(e) == "kb: 1 added, 3 updated (Sources/…, Notes/…)"
```

- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(kbsync): git runner, state detection, commit messages`.

### Task 15: kbsync — sync cycle, pause/resume, status

**Files:**
- Create: `server/jesinia/services/kbsync/application/service.py`, `server/jesinia/services/kbsync/{api.py,module.py}`
- Test: `server/tests/services/kbsync/test_cycle.py`, `test_api.py`

**Interfaces:**
- Consumes: Task 14; Task 12 `KbWatcher.last_activity`; Task 9 `Scheduler`, `EventBus`.
- Produces:
  - `SyncResult(outcome: Literal["skipped","committed","pushed","conflict","blocked","error"], message: str)`.
  - `SyncStatus(state: GitState, paused: bool, last_run: datetime | None, last_result: SyncResult | None, next_run: datetime | None, consecutive_failures: int)`.
  - `class SyncService: async run_cycle(trigger: Literal["timer","manual","shutdown"]) -> SyncResult; status() -> SyncStatus; async resume() -> None; async init_repo() -> GitState`.
  - Registers scheduler job `"kbsync"` every `cfg.kbsync_interval`; runs `run_cycle("shutdown")` in `stop()` (skipping the quiet wait). Re-detects on `kb.root_changed`.
  - Backoff: after a failure the next timer run is `min(interval, 5min * 2**(failures-1))` from now.
  - Publishes `Event("kbsync.status", asdict(status))` after each cycle.
  - Routes `GET /api/kbsync/status`, `POST /api/kbsync/run`, `POST /api/kbsync/resume`, `POST /api/kbsync/init`; MCP `kb_sync_status`, `kb_sync_now`.

- [ ] **Step 1: Failing tests** (real git, local bare remote, injected clock and `quiet` fake):

```python
async def test_clean_tree_skips_without_network(sync, git_spy):
    assert (await sync.run_cycle("timer")).outcome == "skipped"
    assert not any(c[0] in {"fetch", "pull", "push"} for c in git_spy.calls)

async def test_commit_and_push(sync, work, remote):
    (work / "Notes/a.md").write_text("x")
    r = await sync.run_cycle("manual")
    assert r.outcome == "pushed" and "Jesinia-Auto-Sync: true" in git_log(remote, "-1", "--format=%B")

async def test_waits_for_quiet(sync, watcher_fake):
    watcher_fake.last_activity = sync.clock.now()  # just edited
    ... # cycle sleeps until quiet (fake clock advanced 30s) before `git add`

async def test_rebase_conflict_aborts_and_pauses(sync, work, other_clone):
    make_conflicting_edits(work, other_clone, "Notes/a.md")
    r = await sync.run_cycle("manual")
    assert r.outcome == "conflict" and sync.status().paused
    assert not (work / ".git/rebase-merge").exists() and read(work / "Notes/a.md") == "mine"
    assert (await sync.run_cycle("timer")).outcome == "skipped"  # paused

@pytest.mark.parametrize("setup", ["detach", "start_rebase", "unset_upstream"])
async def test_odd_states_block_without_touching_repo(sync, work, setup):
    apply_state(work, setup); before = repo_fingerprint(work)
    (work / "Notes/a.md").write_text("x")
    r = await sync.run_cycle("manual")
    assert r.outcome in {"blocked", "committed"} and sync.status().state.problem
    if setup != "unset_upstream": assert repo_fingerprint(work, include_worktree=False) == before

async def test_commit_only_mode_never_pushes(sync_no_remote): assert (await ...).outcome == "committed"
async def test_backoff_after_push_failure(sync_bad_remote): ...  # next_run == now + 5min, then 10min
async def test_never_force_pushes(sync, git_spy): assert all("--force" not in " ".join(c) for c in git_spy.calls)
```

- [ ] **Step 2–4:** FAIL → implement per spec §4.4 cycle steps 1–6 (`blocked` when `problem` is detached/rebase/merge; `no-upstream` commits but doesn't push) → PASS.
- [ ] **Step 5: Commit** `feat(kbsync): periodic commit/pull/push cycle with conflict pause`.

---

# Phase 4 — MCP and agents

### Task 16: MCP endpoint

**Files:**
- Create: `server/jesinia/mcp/server.py`
- Modify: `server/jesinia/core/app.py` (mount `/mcp`)
- Test: `server/tests/mcp/test_mcp.py`

**Interfaces:**
- Consumes: every module's `mcp_tools()`; Task 9 `require_user`, `Principal`.
- Produces: `build_mcp(ctx: AppContext, tools: Sequence[ToolDef], on_write: WriteGate) -> ASGIApp` using the `mcp` SDK streamable-HTTP transport, stateless mode. `WriteGate` protocol: `async check(tool: ToolDef, args: BaseModel, principal: Principal) -> GateDecision` (`allow` | `pending(pending_id)`); default gate allows everything for `Principal.kind == "user"` (Task 18 replaces it for sessions). Duplicate tool names raise at startup. Tool errors map to MCP error results with the exception class name and message.

- [ ] **Step 1: Failing tests** using the SDK's client over the ASGI app:

```python
async def test_lists_namespaced_tools(mcp_client):
    names = {t.name for t in (await mcp_client.list_tools()).tools}
    assert {"kb_read", "kb_write", "search_query", "kb_sync_status"} <= names
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in names)

async def test_rejects_missing_bearer(raw_client): assert (await raw_client.post("/mcp", json={})).status_code == 401

async def test_kb_write_then_read_round_trip(mcp_client): ...
async def test_stale_write_surfaces_as_tool_error(mcp_client): ...  # result.isError and "StaleWrite" in text
```

- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(mcp): authenticated streamable-HTTP MCP endpoint aggregating module tools`.

### Task 17: Agent runner protocol, fake runner, Claude Code and Codex adapters

**Files:**
- Create: `server/jesinia/services/agents/domain/events.py`, `server/jesinia/services/agents/application/runner.py`, `server/jesinia/services/agents/infrastructure/{claude.py,codex.py,fake.py,subprocess_stream.py}`
- Create fixtures: `server/tests/services/agents/fixtures/{claude_turn.jsonl,claude_resume.jsonl,codex_turn.jsonl,truncated.jsonl}`
- Test: `server/tests/services/agents/test_claude.py`, `test_codex.py`, `test_fake.py`

**Interfaces:**
- Produces:
  - `AgentEvent = TextDelta(text) | ToolUse(name, input: dict) | ToolResult(name, ok: bool) | TurnDone(agent_session_id: str, model: str | None) | RunError(message: str)`.
  - `TurnRequest(prompt: str, workdir: Path, system_prompt: str, resume_id: str | None, mcp_config: Path, allowed_tools: Sequence[str])`.
  - `RunnerStatus(available: bool, version: str | None, detail: str)`.
  - `class AgentRunner(Protocol): name: Literal["claude-code","codex","fake"]; async status() -> RunnerStatus; def run(req: TurnRequest) -> AsyncIterator[AgentEvent]`.
  - `ClaudeCodeRunner.build_argv(req) -> list[str]`; `CodexRunner.build_argv(req) -> list[str]`; `FakeRunner(script: Sequence[AgentEvent], delay_ms: int = 0)`.
  - `parse_claude_line(line: str) -> list[AgentEvent]`, `parse_codex_line(line: str) -> list[AgentEvent]`.

- [ ] **Step 1: Capture fixtures.** Run `claude --help` and `codex exec --help` and record the exact flags for: non-interactive prompt, stream-json output (Claude currently requires `--verbose` with `--output-format stream-json`), resume by id, MCP config file + strict mode, allowed/disallowed tools, system-prompt append, working directory, and Codex sandbox mode. Record one real short turn from each CLI into the fixture files (strip any account identifiers). Note the CLI versions in a comment at the top of `claude.py`/`codex.py`.
- [ ] **Step 2: Failing tests:**

```python
def test_claude_argv_confined(req):
    argv = ClaudeCodeRunner().build_argv(req)
    assert argv[:2] == ["claude", "-p"] and "--strict-mcp-config" in argv
    assert value_after(argv, "--disallowedTools").split(",") >= ["Bash", "Edit", "Write"]
    assert value_after(argv, "--mcp-config") == str(req.mcp_config)

def test_claude_resume_flag(req): assert "--resume" in ClaudeCodeRunner().build_argv(replace(req, resume_id="abc"))

def test_parse_claude_fixture():
    evs = [e for l in read_fixture("claude_turn.jsonl") for e in parse_claude_line(l)]
    assert any(isinstance(e, TextDelta) for e in evs) and isinstance(evs[-1], TurnDone)

async def test_truncated_stream_and_nonzero_exit_yield_run_error(fake_proc_from("truncated.jsonl", exit=1)):
    evs = [e async for e in runner.run(req)]
    assert isinstance(evs[-1], RunError) and any(isinstance(e, TextDelta) for e in evs)

def test_codex_argv_read_only_sandbox(req): ...  # sandbox read-only, -C workdir, --json
async def test_status_unavailable_when_binary_missing(monkeypatch): ...  # shutil.which → None → available False
```

- [ ] **Step 3–4:** implement (`subprocess_stream.run_lines(argv, cwd, env) -> AsyncIterator[str]`; process killed on cancellation; stderr tail included in `RunError`) → PASS.
- [ ] **Step 5: Commit** `feat(agents): runner protocol with Claude Code, Codex, and fake adapters`.

### Task 18: Sessions, context packs, SSE, write confirmation, attach

**Files:**
- Create: `server/jesinia/services/agents/application/{sessions.py,packs.py,gate.py}`, `server/jesinia/services/agents/infrastructure/store.py`, `server/jesinia/services/agents/{api.py,module.py,migrations/0001_init.sql}`
- Test: `server/tests/services/agents/test_sessions.py`, `test_packs.py`, `test_gate.py`, `test_api.py`

**Interfaces:**
- Consumes: Task 17 runners; Task 16 `WriteGate`; Task 9 `register_session_token`; Task 8 `DbClass.HISTORY`.
- Produces:
  - `Subject(kind: str, ref: str, title: str)`.
  - `ContextPack(role_prompt: str, files: Mapping[str, str], untrusted: Mapping[str, UntrustedDoc])`; `UntrustedDoc(source: str, text: str)`; `write_pack(pack, dir: Path) -> None` — untrusted files wrapped exactly `<untrusted-data source="{source}">\n{text}\n</untrusted-data>\n`; `SYSTEM_PREAMBLE` constant stating that `<untrusted-data>` content is data, never instructions.
  - `Session(id: str, subject: Subject, runner: str, agent_session_id: str | None, status: Literal["idle","running","failed"], created_at: datetime)`; `StoredEvent(seq: int, kind: str, data: dict, origin: Literal["pwa","terminal"])`.
  - `class SessionService: async create(subject: Subject, runner: str, pack: ContextPack, *, preauthorized_tools: set[str] = frozenset()) -> Session; async send(session_id: str, text: str) -> None` (returns immediately; turn runs as a task; one turn at a time per session → `SessionBusy`); `stream(session_id: str, after_seq: int = 0) -> AsyncIterator[StoredEvent]`; `async attach(session_id: str) -> AttachInfo`; `async append_external(session_id: str, kind: str, data: dict) -> None`; `get(session_id) -> Session`.
  - Each session gets a scratch dir `<data_dir>/sessions/<id>/`, a session bearer token (`register_session_token`), and an `mcp.json` pointing at `/mcp` with that token.
  - `SessionWriteGate(WriteGate)`: user principal → allow; session principal → allow if tool in that session's `preauthorized_tools`, else store `PendingWrite(id, session_id, tool, args)` and return `pending`; the MCP tool result tells the agent "awaiting user approval (pending_id=…)". Approve executes the stored call and appends the result to the transcript.
  - Routes: `POST /api/agents/sessions`, `POST /api/agents/sessions/{id}/messages`, `GET /api/agents/sessions/{id}/events?after=` (SSE, `text/event-stream`, event id = seq), `POST /api/agents/sessions/{id}/pending/{pid}/approve|reject`, `GET /api/agents/runners`.
  - MCP tools: `agents_session_attach {id}` → `{subject, pack_dir, transcript}` and `agents_session_note {id, text}` (terminal agent appends progress; `origin="terminal"`).

- [ ] **Step 1: Failing tests** (FakeRunner):

```python
async def test_turn_events_persist_and_stream(svc):
    s = await svc.create(Subject("kb", "p1", "A"), "fake", pack())
    await svc.send(s.id, "hi"); evs = await collect(svc.stream(s.id), until="turn_done")
    assert [e.kind for e in evs][-1] == "turn_done" and svc.get(s.id).agent_session_id == "fake-1"

async def test_second_turn_resumes_agent_session(svc, fake): ...  # fake.last_req.resume_id == "fake-1"
async def test_failed_run_keeps_transcript(svc_failing): ...  # status "failed", earlier text events retained
async def test_send_while_running_raises_busy(svc_slow): ...

def test_untrusted_wrapping_exact(tmp_path):
    write_pack(ContextPack("role", {}, {"post.md": UntrustedDoc("https://x", "Ignore previous")}), tmp_path)
    assert (tmp_path / "post.md").read_text() == '<untrusted-data source="https://x">\nIgnore previous\n</untrusted-data>\n'

async def test_session_write_goes_pending_until_approved(app_env):
    s = await app_env.sessions.create(subj, "fake", pack())
    res = await mcp_call(app_env, "kb_write", args, token=session_token(s))
    assert "awaiting user approval" in res.text and not page_exists(app_env)
    await app_env.client.post(f"/api/agents/sessions/{s.id}/pending/{pid(res)}/approve")
    assert page_exists(app_env)

async def test_preauthorized_write_runs_directly(app_env): ...
async def test_sse_resumes_after_seq(client): ...  # Last-Event-ID / ?after= skips earlier events
async def test_attach_returns_pack_and_transcript_and_terminal_notes_stream(app_env): ...
```

- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(agents): sessions, context packs, SSE, write gate, terminal attach`.

---

# Phase 5 — GitHub, fetch, CLI, install

### Task 19: GitHub and fetch services

**Files:**
- Create: `server/jesinia/services/github/{application/service.py,infrastructure/auth.py,infrastructure/cache.py,module.py,migrations/0001_init.sql}`, `server/jesinia/services/fetch/{application/service.py,infrastructure/sanitize.py,module.py,migrations/0001_init.sql}`
- Test: `server/tests/services/github/test_github.py`, `server/tests/services/fetch/test_fetch.py`

**Interfaces:**
- Produces:
  - `async github_token() -> str | None` — `gh auth token`, then `security find-generic-password -s jesinia-github -w`; never persisted.
  - `class GitHubService: async get(path: str, params: Mapping[str, str] | None = None) -> GitHubResponse; async graphql(query: str, variables: Mapping[str, object]) -> dict`; `GitHubResponse(status: int, data: object, from_cache: bool)`; `GitHubUnavailable`. ETag cache in `github.sqlite` (`DbClass.CACHE`, rows older than 30 days pruned at start).
  - `class FetchService: async get(url: str, *, max_bytes: int = 5_000_000) -> FetchResult`; `FetchResult(url: str, status: int, content_type: str, body: bytes, from_cache: bool)`; `FetchTooLarge`, `FetchBlocked`. Conditional requests via ETag/Last-Modified; per-host minimum interval 1 s; only `http`/`https`; refuses hosts resolving to loopback/private ranges.
  - `sanitize_html(html: str, base_url: str) -> str` via `nh3` with allowlist (headings, p, a[href], img[src,alt], lists, blockquote, pre, code, em, strong, table family); rewrites relative URLs absolute; adds `rel="noopener noreferrer"` to links.

- [x] **Step 1: Failing tests** (respx for HTTP; monkeypatched subprocess for auth):

```python
async def test_etag_reuse(gh, respx_mock):
    respx_mock.get("https://api.github.com/user").mock(side_effect=[Response(200, json={"a":1}, headers={"ETag":"x"}), Response(304)])
    await gh.get("/user"); r = await gh.get("/user")
    assert r.from_cache and r.data == {"a": 1}

async def test_unavailable_without_token(gh_no_token): 
    with pytest.raises(GitHubUnavailable): await gh_no_token.get("/user")

async def test_fetch_blocks_private_addresses(fetch):
    with pytest.raises(FetchBlocked): await fetch.get("http://127.0.0.1:4870/api")

async def test_fetch_size_cap(fetch, respx_mock): ...  # streaming abort past max_bytes → FetchTooLarge

def test_sanitize_strips_script_and_handlers():
    out = sanitize_html('<p onclick="x()">a</p><script>x()</script><a href="/b">l</a>', "https://s.example/p")
    assert "script" not in out and "onclick" not in out and 'href="https://s.example/b"' in out
```

- [x] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat: github and fetch services with caching and sanitization`.

### Task 20: `jesinia` CLI

**Files:**
- Create: `server/jesinia/cli/main.py`, `server/jesinia/cli/backup.py`, `server/jesinia/cli/doctor.py`, `server/jesinia/registry.py` (the ordered module list: kb, search, kbsync, agents, github, fetch, notes)
- Modify: `server/pyproject.toml` (`[project.scripts] jesinia = "jesinia.cli.main:app"`)
- Test: `server/tests/cli/test_cli.py`

**Interfaces:**
- Produces commands: `serve` (uvicorn on `127.0.0.1:<port>`, JSON logs to `log_dir/server.log` with 10 MB × 5 rotation), `status` (calls `/healthz`, prints a fresh login URL from `POST /api/auth/nonce` using the bearer token), `doctor`, `config get <key>` / `config set <key> <value>`, `token show|rotate` (rotate also invalidates cookie sessions and re-runs agent MCP registration), `sync now`, `reindex`, `backup [--dest DIR]`, `openapi` (prints the OpenAPI JSON — consumed by Task 23), `install`/`uninstall` (Task 21).
- `doctor` checks: config readable + 0600; KB path exists/writable; git mode + `git ls-remote` reachability (timeout 10 s); `uv`, `node`, `pnpm`, `gh` (+ `gh auth status`), `claude`, `codex` presence/versions; LaunchAgent loaded (`launchctl print gui/$UID/com.jesinia.server`); server `/healthz`. Each line `ok|warn|fail <check>: <detail> [→ remedy]`; exit 1 if any `fail`.
- `backup` copies every `DbClass.HISTORY` database (sqlite online backup API) plus `config.toml` into `<dest or data_dir/backups>/<UTC timestamp>/`.

- [x] **Step 1: Failing tests** (Typer `CliRunner`, `JESINIA_HOME=tmp`):

```python
def test_config_set_and_get(cli): 
    assert cli(["config", "set", "kb.path", str(kb)]).exit_code == 0
    assert cli(["config", "get", "kb.path"]).stdout.strip() == str(kb)

def test_token_rotate_changes_token(cli): ...
def test_backup_copies_history_dbs_only(cli, seeded_dbs): ...  # sessions.sqlite present; search.sqlite absent
def test_doctor_reports_missing_kb_as_fail_with_remedy(cli): ...  # "fail kb.path" and "→ jesinia config set kb.path"
def test_openapi_includes_kb_routes(cli): assert "/api/kb/pages/{id}" in json.loads(cli(["openapi"]).stdout)["paths"]
```

- [x] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(cli): jesinia CLI with doctor, backup, and config`.

### Task 21: launchd, `./setup`, install/uninstall, agent registration

**Files:**
- Create: `setup` (bash), `server/jesinia/cli/install.py` (the plist is generated by `render_plist` with `plistlib` rather than from a template file), `docs/operations.md` (registration commands)
- Create: `.github/workflows/macos-setup.yml`
- Test: `server/tests/cli/test_install.py`

**Interfaces:**
- Consumes: Task 20 CLI, Task 8 config.
- Produces:
  - `render_plist(uv: Path, repo: Path, log_dir: Path, path_env: str, ssh_auth_sock: str | None) -> str` (label `com.jesinia.server`, `ProgramArguments = [uv, "run", "--project", <repo>/server, "jesinia", "serve"]`, `RunAtLoad`, `KeepAlive {SuccessfulExit false}`, stdout/stderr to `log_dir`).
  - `jesinia install --kb-path PATH [--dry-run] [--yes]`: writes config, data/log dirs, plist; `launchctl bootout` (ignore absent) then `launchctl bootstrap gui/$UID <plist>`; polls `/healthz` up to 20 s; for each of `claude`/`codex` on PATH asks (unless `--yes`) then registers the MCP server and the skills (Task 26 provides the plugin dir) — exact registration commands are verified from `claude mcp --help`, `claude plugin --help`, and `codex mcp --help` at implementation time and recorded in `docs/operations.md`; prints the login URL. `--dry-run` prints every action and changes nothing.
  - `jesinia uninstall [--dry-run] [--purge-data]`: bootout + remove plist + remove agent registrations; data dirs removed only with `--purge-data` after confirmation; never touches the KB.
  - `setup` bash: `set -euo pipefail`; checks macOS, git, uv, node, corepack/pnpm, gh (prints Homebrew install commands for missing ones); `make install build`; prompts for KB path (default `~/kb`) unless `--kb-path`; forwards `--dry-run`/`--uninstall`/`--yes` to `uv run --project server jesinia install|uninstall`.

- [x] **Step 1: Failing tests:**

```python
def test_plist_is_valid_and_keepalive_on_crash_only(tmp_path):
    p = plistlib.loads(render_plist(Path("/opt/uv"), Path("/r"), tmp_path, "/usr/bin", "/tmp/ssh").encode())
    assert p["Label"] == "com.jesinia.server" and p["KeepAlive"] == {"SuccessfulExit": False}
    assert p["EnvironmentVariables"]["SSH_AUTH_SOCK"] == "/tmp/ssh"

def test_dry_run_changes_nothing(cli, tmp_home, launchctl_spy):
    r = cli(["install", "--kb-path", str(kb), "--dry-run"])
    assert "launchctl bootstrap" in r.stdout and not launchctl_spy.calls and not (tmp_home / "Library/LaunchAgents").exists()

def test_uninstall_never_touches_kb(cli, kb_with_files): ...
def test_install_is_idempotent(cli, launchctl_spy): ...  # second run: bootout then bootstrap, config token unchanged
```

- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: macOS CI smoke** (`macos-setup.yml`, runs on PRs touching `setup`, `launchd/`, `cli/`): `HOME=$(mktemp -d) ./setup --kb-path "$HOME/kb" --yes`, `curl -fsS http://127.0.0.1:4870/healthz`, `./setup --uninstall --yes`, assert plist removed and `$HOME/kb` intact.
- [x] **Step 6: Commit** `feat: launchd install, setup script, and macOS smoke test`.

---

# Phase 6 — Web

### Task 22: `@jesinia/design` package

**Files:**
- Create: `web/packages/design/{package.json,src/tokens.css,src/reset.css,src/fonts.css,src/index.ts,src/components/*.tsx,src/components/*.module.css}`, `web/apps/design-demo/` (Vite app built to `server/jesinia/static/design/`)
- Test: `web/packages/design/src/__tests__/contrast.test.ts`, `components.test.tsx`

**Interfaces:**
- Consumes: Task 3 `docs/design/tokens.md` (exact names/values), Task 4 `docs/design/components.md` (props signatures).
- Produces: `import "@jesinia/design/tokens.css"`, `reset.css`, `fonts.css`; React exports `Button, Input, Panel, List, Row, Badge, Tabs, Toast, ToastProvider, useToast, ChatTranscript, CommandPalette, Banner, StatusIndicator` and the `ChatMessage` type, with the exact props and message shapes from Task 4 (`docs/design/components.md`; e.g. `ChatTranscript({runner, messages, onApprove, onReject, onRetry})`). Task 4 is authoritative where this line and that spec differ.

- [x] **Step 1: Failing tests:** `contrast.test.ts` parses `tokens.css`, resolves each pair from the Task 3 contrast table in light and dark, and asserts ratios ≥ the table's required minimum (oklch → sRGB → WCAG luminance). `components.test.tsx`: Button `loading` sets `aria-busy` and disables; Tabs arrow-key roving focus; ChatTranscript renders a pending-write card whose Approve calls `onApprove(id)`, announces new agent text through a polite live region, and does not auto-scroll when the user has scrolled up.
- [x] **Step 2–4:** FAIL → implement from the Phase 0 docs → PASS.
- [x] **Step 5: Demo app** renders every token and component in both themes with a theme toggle (`data-theme`). Commit `feat(design): @jesinia/design tokens, components, and demo`.

### Task 23: `@jesinia/client`, PWA shell, launcher and settings

**Files:**
- Create: `web/packages/client/{package.json,src/openapi.d.ts (generated),src/client.ts,src/sse.ts}`, `web/packages/shell/` (shared app shell, nav, sync/conflict banner, PWA manifest + service-worker helper), `web/apps/launcher/` (built to `server/jesinia/static/launcher/`, served at `/` and `/settings/`)
- Modify: `Makefile` (`gen-client`: `uv run --project server jesinia openapi > web/packages/client/openapi.json` then `pnpm --filter @jesinia/client gen`), `server/jesinia/core/app.py` (static mounts: `/` → launcher, `/<app>/` → `static/<app>/`, SPA fallback to the app's `index.html`)
- Test: `web/packages/client/src/__tests__/sse.test.ts`, `web/apps/launcher/src/__tests__/settings.test.tsx`, `web/tests/e2e/launcher.spec.ts`

**Interfaces:**
- Produces:
  - `createClient(): JesiniaClient` (typed `openapi-fetch` over generated paths; cookie auth; `401` → shows "open the login link from `jesinia status`").
  - `subscribe<T>(url: string, onEvent: (e: {id: number, data: T}) => void, opts?: {after?: number}): () => void` — EventSource wrapper that reconnects with `after=<last id>`.
  - `AppShell({app, children})` — nav per Task 5 README, sync banner driven by `GET /api/kbsync/status` + `kbsync.status` SSE (`GET /api/events?topics=kbsync.status,kb.page_changed` — add this core SSE route in this task), conflict banner from `/api/kb/conflicts`.
  - Settings view: KB path field (`PUT /api/kb/path`, shows validation error), git mode + last sync + next run + Sync now / Resume / Initialize git, runner status (`/api/agents/runners`), conflicts list, token rotate.

- [x] **Step 1: Failing tests:** `sse.test.ts` — reconnect resumes from last id; `settings.test.tsx` — invalid KB path shows the server's error, "Resume" appears only when `paused`; e2e `launcher.spec.ts` against a server started with a temp `JESINIA_HOME` and a fixture KB repo: login via nonce URL, set KB path, see mode "push", click Sync now, see last result.
- [x] **Step 2–4:** FAIL → implement → PASS (`make test-e2e` starts the server via Playwright `webServer`).
- [x] **Step 5: Commit** `feat(web): typed client, app shell, launcher and settings PWA`.

---

# Phase 7 — Notes tracer, skills, docs

### Task 24: Notes app backend

**Files:**
- Create: `server/jesinia/apps/notes/{application/service.py,api.py,module.py,migrations/0001_init.sql}`
- Test: `server/tests/apps/notes/test_notes.py`

**Interfaces:**
- Consumes: `KbService`, `SearchService`, `SessionService` (via `ctx.services`), `wrap_ai`.
- Produces:
  - `class NotesService: async start_session(page_id: PageId, runner: str) -> Session; async save_note(session_id: str, *, title: str, user_text: str, agent_text: str, model: str) -> PageWithRev`.
  - `start_session` builds `ContextPack(role_prompt=NOTES_ROLE, files={}, untrusted={"subject.md": UntrustedDoc(f"kb:{id}", <page markdown>), "related.md": UntrustedDoc("kb:search", <top 5 search hits as a list of `[[id|Title]]` + snippet>)})` — KB content used as context is wrapped as untrusted per spec §4.3; `preauthorized_tools={"notes_save"}`.
  - `save_note` writes to folder `cfg.folders["note"]` (`Notes`), body = `user_text` + blank line + `wrap_ai(agent_text, agent=<runner>, model=model, at=now, session=session_id)` + `\n\nAbout: [[<subject id>|<subject title>]]\n`; `jesinia: {kind: "note", captured: now-local-ISO}`; records `(session_id, note_id, subject_id)` in `notes.sqlite` (`DbClass.DERIVED`; rebuildable from note bodies' links).
  - Routes `POST /api/notes/sessions {page_id, runner}`, `POST /api/notes/sessions/{id}/save`; MCP `notes_save` (`writes=True`).
  - Editing and browsing (ADR 0008 §5) use the shared KB routes from Task 12 (`GET /api/kb/tree`, `PATCH /api/kb/pages/{id}/body`, `PUT /api/kb/pages`, `POST /api/kb/pages/{id}/move`); the Notes module adds no KB routes and never touches the filesystem directly.

- [x] **Step 1: Failing tests:**

```python
async def test_save_note_format(env):
    subj = await env.kb.write(PageDraft(None, "Topic", PurePosixPath("Ideas"), "x"))
    s = await env.notes.start_session(subj.page.id, "fake")
    n = await env.notes.save_note(s.id, title="On Topic", user_text="Mine.", agent_text="Theirs.", model="m")
    body = n.page.body
    assert body.startswith("Mine.\n\n<!-- jesinia:ai agent=fake model=m ")
    assert f"About: [[{subj.page.id}|Topic]]" in body and n.page.path.parent == PurePosixPath("Notes")

async def test_pack_wraps_subject_as_untrusted(env, tmp_session_dir): ...
async def test_notes_save_is_preauthorized_but_kb_write_is_not(app_env): ...
```

- [x] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(notes): tracer app backend`.

### Task 25: Notes PWA and end-to-end flow

**Files:**
- Create: `web/apps/notes/` (built to `server/jesinia/static/notes/`)
- Create: `server/jesinia/testing/fake_runner_app.py` (starts the server with `FakeRunner` registered as `"fake"` when `JESINIA_FAKE_RUNNER=1`; refuses to load unless `JESINIA_HOME` is set, so it can never activate in a real install)
- Test: `web/apps/notes/src/__tests__/*.test.tsx`, `web/tests/e2e/notes.spec.ts`

**Interfaces:**
- Consumes: Task 23 client/shell, Task 22 `ChatTranscript`, Task 24 routes, Task 18 SSE + pending routes.
- Editor: CodeMirror 6 (MIT) Markdown with live styling (markers only on the caret line), `[[` autocomplete, provenance comments folded into read-only chips, autosave (~1 s idle, blur, page switch) via `PATCH /api/kb/pages/{id}/body` with `expected_rev`; properties edits go through `PUT /api/kb/pages`; file tree from `GET /api/kb/tree`; grouped search results per the Task 5 wireframes (ADR 0008 §5).

- [ ] **Step 1: Failing tests:** unit — search box debounces 200 ms and lists hits; chat streams deltas into one agent message; Save note disabled until an agent turn completes. e2e (`notes.spec.ts`, fake runner scripted to call `kb_write` once and then reply):
  1. search fixture page → open → start chat with runner "fake" → see streamed reply;
  2. the agent's `kb_write` shows a pending-write card → Reject → file not created;
  3. Save note → file appears in fixture KB `Notes/` with provenance comments and back-link (assert by reading the file);
  4. edit the body of a fixture page whose front matter is noncanonical (flow-style `aliases: [rt]`, a quoted title, a YAML comment) and which contains a provenance block → autosave writes it, and the front-matter block and the untouched provenance block are byte-identical (assert by reading the file);
  5. change the file on disk while a local edit is unsaved → the changed-on-disk bar appears and "Keep mine" writes the local version.
- [ ] **Step 2–4:** FAIL → implement per Task 5 wireframes → PASS.
- [ ] **Step 5: Manual check** with real `claude` and `codex` runners on the author's Mac; record result in the PR description. Commit `feat(notes): tracer PWA with end-to-end test`.

### Task 26: Agent skills and plugins

**Files:**
- Create: `skills/claude-code/.claude-plugin/plugin.json`, `skills/claude-code/.claude-plugin/marketplace.json`, `skills/claude-code/skills/jesinia-kb-conventions/SKILL.md`, `skills/claude-code/skills/jesinia-notes/SKILL.md`, `skills/claude-code/commands/jesinia-note.md`, `skills/codex/AGENTS.snippet.md`, `skills/smoke.sh`
- Test: `server/tests/skills/test_skill_files.py`

**Interfaces:**
- Consumes: MCP tool names from Tasks 12, 13, 15, 18, 24; `docs/conventions/kb-format.md` (Task 27 — write skills to reference it by path; smoke runs after Task 27).
- Produces: plugin `jesinia` (MCP server config uses `${JESINIA_TOKEN}` header from env — `install` exports it via the agent's own config mechanism, never a committed file).

- [ ] **Step 1: Failing test** `test_skill_files.py`: every `SKILL.md` has front-matter `name` + `description`; every MCP tool name mentioned in skills exists in the live tool list (start app in-process, list tools); every `docs/...` path referenced exists.
- [ ] **Step 2: Write skills.** `jesinia-kb-conventions`: always use `kb_*` tools, never edit KB files directly; folder rules; `[[id|Title]]` links via `kb_link`; `#tags`; provenance comments for agent-written spans; pass `expected_rev` from `kb_read`; on `StaleWrite` re-read and merge. `jesinia-notes`: attach via `agents_session_attach`, read the pack dir, post progress with `agents_session_note`, save with `notes_save`.
- [ ] **Step 3: Run test** — PASS. **Step 4: `smoke.sh`** runs `claude -p` and `codex exec` against a temp server + fixture KB asking each to create a note; asserts the file exists with provenance comments; skips a CLI that is absent. Commit `feat(skills): Claude Code plugin and Codex snippet for the KB`.

### Task 27: Conventions docs, ADRs, operations, README

**Files:**
- Create: `docs/conventions/{kb-format.md,architecture.md,adding-an-app.md,agents.md,kb-sync.md,security.md,design-system.md}`, `docs/adr/{0001-agpl-license.md,0002-single-server-shared-services.md,0003-format-only-pigeonpile-interop.md,0004-files-are-source-of-truth.md,0005-cli-driven-agents.md,0006-kb-git-sync.md}`, `docs/operations.md`, `docs/README.md`
- Modify: `README.md`
- Test: `scripts/check_docs.py` + `server/tests/test_docs.py`

- [ ] **Step 1: Failing test** `test_docs.py`: every relative link in `docs/**/*.md` and `README.md` resolves; every ADR has `Status`, `Date`, `Context`, `Decision`, `Consequences`; `kb-format.md` contains the exact provenance comment format and collision-suffix rule from Global Constraints.
- [ ] **Step 2: Write docs** — content from spec §§3–7, §9–10 restated as conventions (not as a design narrative); `kb-format.md` includes the Pigeonpile-compatibility section with ADR 0007 results; `operations.md` includes setup, uninstall, logs, `doctor`, upgrade (`git pull && ./setup`), the verified agent-registration commands from Task 21, and troubleshooting for: KB path missing, sync paused on conflict (exact resolve steps: `cd <kb> && git pull --rebase`, resolve, `git rebase --continue`, then Resume), runner logged out.
- [ ] **Step 3: README** — what Jesinia is (one paragraph), 5-minute quickstart (`git clone … && ./setup`), links to docs, AGPL notice.
- [ ] **Step 4: Run** `make ci` — PASS. Commit `docs: conventions, ADRs, operations guide, README`.

### Task 28: Foundation acceptance

- [ ] **Step 1:** On the author's Mac, from a fresh clone: `./setup` → server running under launchd (`launchctl print gui/$UID/com.jesinia.server` shows running).
- [ ] **Step 2:** KB path set to the real KB repo; Settings shows mode "push"; `jesinia sync now` → `skipped` on a clean tree; edit a page → `pushed`.
- [ ] **Step 3:** From Claude Code and from Codex, `kb_search` returns results via MCP.
- [ ] **Step 4:** Notes flow with a real runner writes a correct page; open it in the Pigeonpile Wiki and confirm it renders.
- [ ] **Step 5:** `make ci` green on `main`; `./setup --uninstall` leaves the KB intact. Record results in `docs/specs/2026-10-16-jesinia-foundation-design.md` status → "Implemented".

---

## PR map

| PR | Tasks | Notes |
|---|---|---|
| 1 | 1 | compat ADR (docs only) |
| 2 | 2–6 | design system + wireframes (docs only), gate |
| 3 | 7 | scaffold, standards, CI |
| 4 | 8–9 | core |
| 5 | 10–12 | kb |
| 6 | 13 | search |
| 7 | 14–15 | kbsync |
| 8 | 16 | MCP |
| 9 | 17–18 | agents |
| 10 | 19 | github + fetch |
| 11 | 20–21 | CLI + install |
| 12 | 22 | design package |
| 13 | 23 | client, shell, launcher/settings |
| 14 | 24–25 | Notes |
| 15 | 26–27 | skills + docs |

Each PR follows the repo workflow: local code review → push → open PR → **request a Codex review** (comment `@codex review` on the PR unless Codex auto-review is enabled on the repo) → **wait for the Codex review to land** → address every finding → **green CI** → merge. Never merge with an unaddressed Codex finding or a red/pending CI run.

**Phase 0 exception:** PRs 1–2 land before CI exists (CI is created in Task 7 / PR 3). These pre-scaffold PRs are docs only and are gated on the Codex review alone (all findings addressed). Every PR from PR 3 on must also have green CI.

## Execution

- **Method:** subagent-driven (superpowers:subagent-driven-development) — a fresh implementer and a fresh reviewer per task.
- **Phase 0 (Tasks 1–6)** is run interactively with the user; the design-pass kit is `docs/design/README.md`.
- **Phases 1–7 (Tasks 7–28)** run as a Workflow in a fresh session, started only after the Task 6 design ADR is Accepted and merged.
