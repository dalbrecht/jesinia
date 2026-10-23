# ADR 0013: Tasks are Markdown task lines with Org's model

- **Status:** Proposed (pending the compat check in `docs/design/compat/`, row i–m)
- **Date:** 2026-10-23
- **Related:** [ADR 0003](0003-format-only-pigeonpile-interop.md);
  [ADR 0007](0007-pigeonpile-compat-results.md); [KB format](../conventions/kb-format.md)

## Context

Tasks need to move between the Pigeonpile Wiki, Dopo Tasks and Jesinia through the shared
KB folder, which these tools share by file format only (ADR 0003). Emacs Org mode has a
proven task model (states, priorities, scheduled and deadline dates, repeaters, a closed
date), so it was considered as the exchange format.

Embedding Org syntax in Markdown was rejected:

- In a fenced ```` ```org ```` block, every Markdown viewer shows raw text, nothing can be
  checked off in place, and Jesinia's tag grammar ignores fenced code, so the tasks fall
  out of tag search.
- Unfenced between markers, Org lines change meaning: `* TODO` is a bullet, a leading `**`
  can turn bold, `:PROPERTIES:` drawers show as stray text, and a Wiki re-save may rewrite
  them.
- Org brings a second tag syntax (`:a:b:`) and a second link and id syntax (`:ID:`,
  `[[id:…]]`).
- Org tools (Emacs, Orgzly, Bloom) read `.org` files, not Org inside `.md`, so embedding
  adds a grammar without gaining those tools.

## Decision

A task is a **GFM task-list item** whose trailing `key:value` fields carry Org's model.
Prose, headings and links around it stay ordinary Markdown.

```markdown
- [ ] Ship feed management #jesinia priority:a scheduled:2026-10-24 due:2026-10-31
- [ ] Water the garden every:1w scheduled:2026-10-25
- [ ] Renew passport state:waiting due:2026-12-01
  - [x] Book photo appointment closed:2026-10-22
- [x] Old idea state:cancelled closed:2026-10-20
```

### Grammar

- **Task line:** a list item (`-`, `*` or `+`, or an ordered `1.`) followed by `[ ]`,
  `[x]` or `[X]` and a space. Indentation nests a task under the item above it (a
  subtask). Task lines inside fenced or indented code blocks are not tasks.
- **Fields:** a field is a whitespace-separated token `key:value` with a key from the
  table below and a value that matches its form. A token with an unknown key, or a known
  key with an invalid value (`due:tomorrow`), is title text and is never rewritten. Tokens
  inside inline code are text. Fields may appear anywhere after the checkbox; the title is
  the rest of the line with the fields removed and spaces collapsed.
- **One of each:** a key appears at most once per line, and `every` and `after` count as
  one key. If a line has more than one, the first valid token, reading left to right, is
  the field. Every later one is title text: shown in the title, never read, never
  rewritten. Writers update that first token, and never write a second token for a key
  the line already has.
- **Tags:** ordinary `#tags` on the line, per the KB tag grammar. There is no second tag
  syntax.

| Key | Value | Org equivalent |
|---|---|---|
| `state` | `next`, `waiting` (open) or `cancelled` (done) | extra TODO keywords |
| `priority` | `a`, `b` or `c` | `[#A]`–`[#C]` |
| `scheduled` | date, optionally with time | `SCHEDULED:` |
| `due` | date, optionally with time | `DEADLINE:` |
| `every` | repeat interval, keeping the cadence | `++1w` |
| `after` | repeat interval, counted from completion | `.+1w` |
| `closed` | date, optionally with time | `CLOSED:` |

- **States:** `[ ]` is open (Org `TODO`), `[x]` is done (`DONE`). `state:` refines them:
  `next` and `waiting` need `[ ]`; `cancelled` needs `[x]`. A contradictory line
  (`[x] … state:waiting`) is read as its checkbox says, and the field is left as written.
- **Dates** are `YYYY-MM-DD` with an optional `THH:MM`. They are floating local time, as
  in Org: no offset, no zone.
- **Intervals** are a positive integer and a unit: `d`, `w`, `m` (month) or `y`. A repeater
  needs a `scheduled` or `due` date.
- Priority is a field, not Org's `[#A]` cookie, because `#A` after `[` is a tag in the KB
  tag grammar.

### Writing

- Writers change only the tokens they own, in place, and keep the rest of the line and
  the file byte-for-byte. A new field is appended at the end of the line.
- New task lines are written as `- [ ] <title> <tags> <fields>`, fields in table order.
- **Completing** a task sets `[x]` and appends `closed:` with today's date. Completing a
  repeating task instead keeps `[ ]` and moves its dates forward. With `every`, each date
  steps by the interval until it is after today, keeping the original cadence. With
  `after`, the earlier date becomes today plus the interval, and the other date moves by
  the same amount. Time of day is kept.
- Edits use the page's revision check (`expected_rev`, KB format "Writing safely"); a
  stale edit is refused, never merged by line.

## Open questions

- **Task identity across apps.** Lines have no ids in this proposal, so a task is
  addressed by page `id` plus line content. If Dopo Tasks needs a stable id for its own
  store, add an optional `id:` field (short, base32) rather than a block reference.
- **Agenda ownership.** Which app answers "what's due today" for agents (an MCP tool in
  Jesinia, or Dopo Tasks), so there is one computed view.

## Consequences

- Task pages read and toggle as normal checklists in any Markdown tool, including the Wiki.
- Jesinia's tag index and search keep working on task lines with no changes.
- Each app needs a small parser for the field table; it should share test cases (the
  fixture page) rather than code, per ADR 0003.
- Org tools can't read these tasks directly. If that's wanted later, add an `.org` export,
  not a second source of truth.
- When this ADR is accepted, the grammar moves into `docs/conventions/kb-format.md` as a
  Tasks section, and this ADR points to it.
