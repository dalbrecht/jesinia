# Identity session explorations

Snapshots of the visual comparisons used in the Task 2 identity session (2026-10-16).
They are working material behind [the brief](../brief.md), not specs. Values in them are
rough, and `docs/design/tokens.md` (Task 3, not yet written) supersedes them.

| File | Question | Outcome |
|---|---|---|
| [`density.html`](density.html) | Q3: density | RSS/Review at "Desk"; Issues compact |
| [`palette.html`](palette.html) | Q5: accent strategy | "Botanical plate" palette; per-app identity hues |
| [`type.html`](type.html) | Q6: typefaces | Bricolage Grotesque, Newsreader, and JetBrains Mono |

Open the files directly in a browser. `type.html` loads its specimens from Google Fonts
so it renders without a build step. That is for previewing only: the apps self-host
every face via `@fontsource` and never load fonts remotely (kit §2).
