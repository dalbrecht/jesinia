# Pigeonpile Wiki compatibility fixture (plan Task 1, spec §5.6)

A tiny KB that exercises every convention Jesinia relies on. Run it once against
the Pigeonpile Wiki and record the results in `docs/adr/0007-pigeonpile-compat-results.md`.

## Procedure

1. Copy `fixture-kb/` somewhere outside this repo (e.g. `/tmp/jesinia-compat`), so the
   committed fixture stays pristine for diffing.
2. Open the copy as a workspace in the Pigeonpile Wiki (web and, if available, native).
3. In each page, change `CHANGEME` (or any word) and save.
4. Diff the copy against `fixture-kb/` and answer:

| # | Question | Where to look |
|---|---|---|
| a | Is the `jesinia:` front-matter block preserved on re-save? | `Round Trip.md` front-matter |
| b | Is the foreign key `aliases` preserved? | `Round Trip.md` front-matter |
| c | Are `<!-- jesinia:ai … -->` comments hidden in the rendered page, or shown as text? Are they preserved in the file? | Wiki view + file |
| d | Does `[[id|Title]]` render as a working link to Link Target? | Wiki view |
| e | Is `#compat` recognized as a tag? | Wiki tag facets |
| f | Was `Plain File.md` adopted? What front-matter was written? | file |
| g | Did any file get renamed or moved? | `ls -R` |
| h | In `Tag grammar.md`, are `#SQLite` and `#sqlite` one tag? Are `#wal-mode`, `#2026` and `#ünïcode` recognized whole? | Wiki tag facets |
| i | In `Task lines.md`, do the task lines render as checkboxes (`-`, `*` and `+` markers, `[x]` and `[X]`, nested ones included), and is the code-block line plain text? | Wiki view |
| j | When you toggle a checkbox in the Wiki, does only `[ ]` ↔ `[x]` change, with the `@key:`/`key:` tokens and the rest of the file byte-for-byte? Is `[X]` kept as written on re-save? | file diff |
| k | Are `*` and `+` markers, indentation and spacing kept on re-save (not rewritten to `-`)? | file diff |
| l | Is `#jesinia` on a task line recognized as a tag? Is no token (`@status:next`, `@context:computer`) shown as a link, mention or tag? | Wiki view + tag facets |
| m | Copy the task lines from `Task lines.md` into a scratch Dopo sync folder's `tasks.v2.md` and open it in Dopo Tasks. Which lines does it list as tasks, with which state and dates? After you edit one item in Dopo, are the proposed tokens (`@priority:`, `@every:`, `completed:`…) still on the other lines? | Dopo Tasks + file diff |

## Decisions the results drive

- (a) fails → `jesinia:` metadata moves to the `.jesinia/meta/<id>.yaml` sidecar (plan Task 10 Step 5).
- (c) shows comments as text → pick a different provenance marker before the RSS spec.
- (h) differs → narrow Jesinia's tag grammar (`docs/conventions/kb-format.md#tags`) to what
  the Wiki accepts, with a follow-up issue if the change is not trivial.
- (i)–(l) pass → ADR 0013 can be accepted as far as the Wiki goes; a failure changes the
  KB rules in ADR 0013 before any parser ships. (m) checks the dialect spec
  (c65llc/amicus-platform#3264) against the shipped Dopo build: core tokens read as §2–§3
  say, and proposed tokens survive as extras until the codec learns them.
