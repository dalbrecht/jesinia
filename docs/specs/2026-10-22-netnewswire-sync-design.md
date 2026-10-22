# NetNewsWire sync (Google Reader API)

- **Status:** Accepted
- **Date:** 2026-10-22
- **Related:** [RSS reader spec](2026-10-16-rss-reader-design.md) (reverses its non-goal
  "syncing read state with other readers"); [feed management spec](2026-10-21-rss-feed-management-design.md);
  [remote access spec](2026-10-22-remote-access-design.md); [ADR 0012](../adr/0012-reader-api-for-netnewswire.md)

## 1. Intent

NetNewsWire on iPhone and iPad becomes the owner's main RSS reading experience, smart
feeds included. Jesinia stays the server and the source of truth: it polls the feeds,
pulls the full articles, and keeps read state and Keeps, so its agents and the KB see
everything that is read there. Every interaction NetNewsWire can sync is mapped into
Jesinia, so the data Jesinia gets is as rich as the protocol allows.

## 2. Decisions the owner made

- Jesinia acts as a server NetNewsWire already supports, with no app changes.
- **Read = read.** Unread = unread.
- **Star = Keep**, exactly as Keep works in the reader (a KB page, then the archive).
- **A star ledger.** Every keep and unkeep is appended to a ledger, so stars can always be
  traced to KB content. A Keep from the reader is added to the ledger too, when the ledger
  does not already show the post as kept.
- **Unstar = "unkept" in the ledger only.** The KB page, the archive and the reader's Kept
  view are untouched.
- **Full articles are pulled ahead of time** for every new post, so NetNewsWire shows the
  whole article.
- Reached over Tailscale (remote access spec).

## 3. Protocol choice

NetNewsWire's only sync account that takes a custom server URL is **FreshRSS**, which
speaks the Google Reader API ("greader"). Feedbin's address is fixed. Jesinia serves the
subset NetNewsWire calls (as read from its source, `Modules/Account/.../ReaderAPI/`).

NetNewsWire's server field gets `https://<remote_host>/api/greader`; it appends
`/accounts/ClientLogin` and `/reader/api/0/...`.

## 4. What maps to what

| NetNewsWire | Reader API call | Jesinia |
|---|---|---|
| Sign in | `POST /accounts/ClientLogin` | Checks the reader password; returns a reader token |
| — | `GET /reader/api/0/token` | Write token (`T`) bound to the reader token |
| — | `GET /reader/api/0/user-info` | A fixed user |
| Folder list | `GET /reader/api/0/tag/list` | Folders (+ the starred state) |
| Feed list | `GET /reader/api/0/subscription/list` | Subscribed feeds with folder |
| Sync unread / all / starred ids | `GET /reader/api/0/stream/items/ids` | Post ids per stream |
| Download articles | `POST /reader/api/0/stream/items/contents` | Posts, with the full article as HTML |
| Read / unread | `edit-tag a=/r= …/state/com.google/read` | `posts.read` |
| Star | `edit-tag a=…/starred` | Keep (no note, no tags), ledger `keep` |
| Unstar | `edit-tag r=…/starred` | Ledger `unkeep` only |
| Mark all read | per-item `edit-tag` | as read |
| Subscribe | `subscription/quickadd` | Add feed (discovery as in the reader) |
| Unsubscribe | `subscription/edit ac=unsubscribe` | Remove feed |
| Rename feed | `subscription/edit ac=edit t=` | Feed title |
| Move feed / put in folder | `subscription/edit ac=edit a=label r=label` | Feed folder (created if new) |
| Rename folder | `rename-tag` | Rename folder |
| Delete folder | `disable-tag` | Its feeds become unfiled (NetNewsWire first untags or unsubscribes them itself) |
| Import OPML | `subscription/import` | OPML import |

Calls NetNewsWire never makes (`mark-all-as-read`, item labels) are not served. Item
labels sent through `edit-tag` are ignored with a 200.

### 4.1 IDs and streams

- Item ids: the post's integer id, decimal in `itemRefs`, and
  `tag:google.com,2005:reader/item/<16 hex digits>` in contents. Both forms are accepted
  in `i=`.
- Feed stream id `feed/<id>`; folder label `user/-/label/<name>`; states
  `user/-/state/com.google/{reading-list,read,starred}`.
- `ot` (oldest time) filters by first sighting; `xt` excludes a state; `n` caps a page
  (max 10000), `c` continues it.
- Starred stream: posts whose latest ledger entry is `keep`.
- Feeds without a folder are listed with no category. NetNewsWire's FreshRSS account may
  refuse to create a feed outside a folder; putting a feed into a folder that does not
  exist creates it.

### 4.2 Article content

`summary.content` is, in order: the full article (cached Markdown rendered to HTML and
sanitized), else the feed's sanitized content, else the escaped snippet. NetNewsWire
downloads an item's content once, so a post whose full article is still pending is held
back from item-id lists for up to 15 minutes after it was first seen.

## 5. Full-article prefetch

A scheduled job (every minute) extracts the full article of posts that have none and no
recorded attempt, newest first, a small batch per run, through the existing fetch service
(SSRF guard, per-host pacing, cache). A failure is recorded on the post
(`article_error`, `article_attempted_at`) and never retried by the job; the reader's
on-demand load still works. This covers new posts and, the first time, existing ones.
It is best-effort and error-isolated: a failure never touches polling or reading.

## 6. Star ledger

Table `star_ledger(id, post_id, action keep|unkeep, source reader|netnewswire|backfill,
page_id, at)`, append-only, in `rss.sqlite` (backed up). The migration backfills one
`keep` per already-kept post. A Keep (any source) appends `keep` unless the post's latest
entry already is one. Unstar appends `unkeep` unless the latest entry already is one.
Starring a post that is already kept to a page appends `keep` and does not write the page
again. Ledger rows outlive pruning of the post they name (no foreign key), since the page
is what they trace.

## 7. Auth

- A **reader password** separate from the install token: `jesinia rss reader-password`
  (and Settings → RSS) creates one, shows it once, and revokes the previous one together
  with every reader token issued under it. Only its SHA-256 is stored.
- `ClientLogin` accepts any `Email` and the reader password, and returns a new random
  reader token (stored hashed; it does not expire, it is revoked with the password).
- Every other call needs `Authorization: GoogleLogin auth=<reader token>`. Writes also
  need `T`, an HMAC of the reader token.
- The Reader API is mounted outside the owner-auth dependency and is the only thing a
  reader token can reach.

## 8. Non-goals

- Other Reader API clients (they may work; only NetNewsWire is tested).
- Sending notes or tags with a star.
- Fever or Feedbin APIs.
- Engagement signals (opened, time read): NetNewsWire does not send them.

## 9. Left to the builder

Batch sizes, exact error bodies, log wording.

## 10. Calls made while building

- Creating a reader password is not idempotent: a stored response would keep the
  password in clear, and a retry only replaces the first one.
- Posts of unsubscribed feeds (kept ones stay in Jesinia) are left out of every item
  list, since NetNewsWire could not place them.
- Unauthenticated callers and the install token both get 401 from the Reader API.
- The ClientLogin username is ignored; the password alone identifies the owner.
- Post ids are never reused (a high-water mark in `rss.sqlite`), since they are the
  Reader item ids a client keeps.
- Item-id lists are newest first by post time, as the reader lists them; the
  continuation is that list's cursor.
