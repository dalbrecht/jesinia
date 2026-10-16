# Design pass: Review, RSS and Settings (2026-10-16)

Mockups reviewed with the author in the browser and accepted. They supersede the
lower-fidelity RSS and Review wireframes where the two disagree. The rendered screens are in
[`design-pass/`](design-pass/): [review queue](design-pass/review-queue.html),
[PR view](design-pass/pr-view.html),
[re-review, stack and posting](design-pass/followups.html), and
[RSS and Settings](design-pass/rss-settings.html).

## Patterns (apply across apps)

1. **Workspace tabs (Review).** A strip of tabs above the app body. The queue is the first tab,
   and every opened review is its own tab, marked with a dot coloured by its verdict, so several
   reviews stay open at once.
2. **Sections are clearly separated.**
   - The queue's groups are tabs with counts (Ready for you, Waiting, Loaded, Still approved),
     not one long list.
   - The open section starts with an intro bar saying what's in it, with its own bulk actions.
   - Rows are separated cards, and a summary strip shows the other sections at a glance.
3. **The book layout (Review PR view, Settings):**
   - **Left:** an outline, like a table of contents.
   - **Right:** a view pane with a **toolbar on top**, the page in the middle and **action
     buttons along the bottom**.
   - The PR outline reads in order: "before you read" (Overview, Background), the walkthrough
     steps as chapters (each with its findings), **Not walked through** with its share of the
     change, and "after you read" (Findings, Metrics, Quiz, your comments, Recommendation).
     Read ticks and a progress footer track where you are.
4. **Coverage is always visible.**
   - The PR toolbar shows "Walkthrough covers N% · x of y files".
   - The outline lists what was left out, with reasons.
   - Queue rows carry the percentage.

## Screen decisions

- **Re-review:**
  - The outline starts with **your requests**, each marked addressed, partly or not
    addressed, then **New since your last look**.
  - The page quotes what you wrote in the earlier round above how the new commits answer it.
  - The previous round can be shown inline or side by side.
- **Stack:**
  - The outline covers the whole stack first, then each PR as a part, then cross-PR findings.
  - A per-PR table gives verdict, findings and coverage, with **Post all** after one
    confirmation.
- **Posting confirmation:**
  - It shows exactly what will be posted.
  - Each warning (a BLOCKING comment on Approve, or commits newer than the reviewed one) must
    be ticked before Approve unlocks, with a one-click alternative.
- **RSS** (three panes, as chosen earlier):
  - **Engage** and **Keep** are always visible at the reader's top right.
  - **Keep is the only way to hold on to a post.** There's no star and no "Saved for later".
  - There's no on-screen key guide; `?` lists the keys.
  - The feeds pane keeps only **+ Add feed**.
  - The reader's bottom bar has Previous, Next and Mark unread.
  - The Keep popover gives the note first, then tag chips from the catalog with a dashed *new*
    chip, and catches near-duplicates before a tag is created.
- **Settings** uses the book layout. Its sections are KB folder, Sync, Agents, Tags, Review,
  **RSS** (new) and Conflicts.
  - **Settings → Tags:** the catalog table with Rename, Merge and Retire. **Retired tags can be
    restored.** The Uncataloged panel suggests merges.
  - **Settings → Review:** the pre-review setting, the gates, watched repos with per-repo
    overrides, the standards layers, and *What Jesinia thinks you know*.
  - **Settings → RSS:** OPML import and export, retention, and the kept-posts folder.
