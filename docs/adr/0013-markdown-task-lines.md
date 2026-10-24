# ADR 0013: KB tasks use Dopo Tasks' task-line dialect

- **Status:** Proposed (pending the compat check in `docs/design/compat/`, rows i–m, and
  the shared dialect spec, c65llc/amicus-platform#3264)
- **Date:** 2026-10-23
- **Related:** [ADR 0003](0003-format-only-pigeonpile-interop.md);
  [ADR 0007](0007-pigeonpile-compat-results.md); [KB format](../conventions/kb-format.md);
  [DOPO next-actions mirror spec](../specs/2026-10-22-dopo-next-actions-mirror-design.md)

## Context

Tasks need to move between the Pigeonpile Wiki, Dopo Tasks and Jesinia through files only
(ADR 0003). Emacs Org mode has a proven task model (states, priorities, scheduled and
deadline dates, repeaters, a closed date), so it was considered as the exchange format.
Embedding Org syntax in Markdown was rejected:

- In a fenced ```` ```org ```` block, every Markdown viewer shows raw text, nothing can be
  checked off in place, and Jesinia's tag grammar ignores fenced code.
- Unfenced, Org lines change meaning in Markdown (`* TODO` is a bullet, `:PROPERTIES:`
  drawers are stray text), and a Wiki re-save may rewrite them.
- Org brings a second tag syntax and a second id and link syntax, and Org tools read
  `.org` files, not Org inside `.md`.

Dopo Tasks already has a Markdown task-line dialect: the GFM checklist file `tasks.v2.md`
that it reconciles two-way with its database. Dopo owns that format, and Jesinia's mirror
already has to read it. A second task syntax in the same KB would mean two parsers and
two sets of rules for one checkbox.

## Decision

Task lines in KB pages use **Dopo's `tasks.v2.md` dialect**, as written down in the shared
spec `docs/specs/2026-10-23-tasks-md-dialect.md` in `c65llc/amicus-platform` (the
*dialect spec*). Where Org's model and Dopo's differ, Dopo's wins. What Dopo lacked
(priority, repeaters, the completion date, hand-added to Today) is proposed there as new
tokens, for fields Dopo's item model already has, rather than invented here.

```markdown
- [ ] Ship feed management #jesinia @status:next @priority:1 due:2026-10-31 defer:2026-10-24
- [ ] Water the garden @every:1w due:2026-10-25
- [ ] Renew passport @status:waiting @waiting:"photo appointment" due:2026-12-01
  - [x] Book photo appointment completed:2026-10-22
```

### What Jesinia takes from the dialect spec

- **Line grammar** (§1): `-`, `*` or `+`, then `[ ]`, `[x]` or `[X]`. Ordered-list items are
  not tasks. Nesting is 2 spaces per level.
- **Tokens** (§2, §4): one contiguous run at the **end** of the line, after the title and
  any `#tags`. A `#tag` after a token ends the run and turns the whole line into title.
- **Reading rules** (§3): explicit `@status:` wins over the checkbox, and an open box
  without one is `inbox`. Invalid and unknown tokens are kept verbatim. For a duplicated
  key the last valid token wins. Dates are `YYYY-MM-DD` days, with no time.
- **Org mapping:** `TODO` is `[ ]` (`inbox`/`next`), `NEXT` is `@status:next`, `WAITING`
  is `@status:waiting` plus a reason, `CANCELLED` is `@status:trash`, `SCHEDULED` is
  `defer:`, `DEADLINE` is `due:`, `CLOSED` is `completed:`, priority is `@priority:<rank>`
  (1 highest), `++1w` is `@every:1w` and `.+1w` is `@after:1w`. There is no yearly unit
  (use `12m`) and no time of day.
- **Recurrence math** is Dopo's engine: month steps clamp to month end, and the clamp is
  sticky.

### Jesinia-specific rules (KB pages)

The dialect spec governs `tasks.v2.md`, which Dopo rewrites whole. KB pages are the user's
text, so Jesinia's writers add these rules there:

- **Bytes are kept.** A writer changes only the tokens it touches, in place, and keeps the
  rest of the line and file byte-for-byte. A new token is appended to the end of the token
  run, before any trailer. Bullet style, `[X]`, indentation and token order are not
  normalized.
- **Ids.** A KB task line has no trailer. Jesinia never mints Dopo ids or revs. Lines
  inside a `<!-- jesinia:mirror … -->` region are the mirror's rendering of Dopo items
  (mirror spec §5.5), not KB tasks.
- **Completing** a task sets `[x]`. It sets the line's `@status:` to `done` if the line has
  one (since an explicit status wins), and sets `completed:` to today's local date, updating
  the existing token if there is one. Reopening changes only the checkbox, the same as the
  Wiki, and `completed:` on an open line is ignored.
- **Completing a repeating task** follows Dopo's respawn: the line is completed as above,
  and the next occurrence is inserted directly below it as a new open line, with its dates
  moved by the engine and the recurrence tokens moved to it (`@repeat-count:` decremented).
  The finished line keeps no recurrence tokens.
- **Writes** use the page's revision check (`expected_rev`); a stale edit is refused.

### What stays out of `tasks.v2.md`

Until Dopo's codec reads the new tokens (dialect spec §4.1), Jesinia doesn't add them to
`tasks.v2.md`. Dopo would keep them as inert extras that go stale. The mirror's
completion edit stays as its spec describes it.

## Consequences

- One task syntax across the KB and Dopo's file. KB pages read and toggle as checklists in
  any Markdown tool, and `#tags` keep working, provided they come before the tokens.
- Jesinia's parser follows the dialect spec. ADR 0003 still applies: no suite code, and
  test cases are shared through fixtures, not code.
- Token names may still change in review of the dialect spec. This ADR follows whatever it
  settles.
- The DOPO mirror spec was written against the v1 `tasks.md` (with `@project:`). Dopo now
  writes `tasks.v2.md` (nesting and `@kind:project` instead of `@project:`), so its §3–§4
  need revising before that plan runs.
- When this ADR is accepted, the KB rules above move into `docs/conventions/kb-format.md`
  as a Tasks section, which links to the dialect spec.
