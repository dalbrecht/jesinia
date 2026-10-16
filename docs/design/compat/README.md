# Pigeonpile Wiki compatibility fixture (plan Task 1, spec §5.6)

A tiny KB that exercises every convention Jesinia relies on. Run it once against
the Pigeonpile Wiki and record the results in `docs/adr/0007-pigeonpile-compat-results.md`.

## Procedure

1. Copy `fixture-kb/` somewhere outside this repo (e.g. `/tmp/jesinia-compat`), so the
   committed fixture stays pristine for diffing.
2. Open the copy as a workspace in the Pigeonpile Wiki (web and, if available, native).
3. In each of the three pages, change `CHANGEME` (or any word) and save.
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

## Decisions the results drive

- (a) fails → `jesinia:` metadata moves to the `.jesinia/meta/<id>.yaml` sidecar (plan Task 10 Step 5).
- (c) shows comments as text → pick a different provenance marker before the RSS spec.
