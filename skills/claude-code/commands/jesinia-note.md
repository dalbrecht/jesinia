---
description: Save a note about a KB page in Jesinia (search for the page, think it through, save via MCP)
argument-hint: <page title or topic> [what to note]
---

Use the `jesinia-kb-conventions` skill.

1. Find the page for "$ARGUMENTS" with `kb_search`, then `kb_read` it. If several pages
   match, list the top three and ask which one.
2. Write a concise note about it: the gist, open questions, and links to related pages
   as `[[id|Title]]` (found with `kb_search`).
3. Show the note and the target title, `Notes/<title>.md`, and ask before saving.
4. Save with `kb_write` (`folder: "Notes"`, `jesinia: {"kind": "note"}`, and
   `provenance: {"agent": "claude-code", "model": "<your model id>"}` so Jesinia records
   the text as AI-written), ending the body with `About: [[<page id>|<page title>]]`.
   Don't write provenance markers yourself. Report the saved path.
