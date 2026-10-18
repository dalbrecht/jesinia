// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes and calls the Review PWA uses (`/api/review`, spec §2), and the pure rules the queue
// renders with. Every PR-derived string here is the PR author's text: it is only ever rendered
// as text.
import { errorText, withIdempotency, type components, type JesiniaClient } from "@jesinia/client";

export { safeHttpUrl } from "@jesinia/shell";

export type QueueRow = components["schemas"]["QueueRowOut"];
export type Queue = components["schemas"]["QueueOut"];
export type PrView = components["schemas"]["PrOut"];
export type OpenResult = components["schemas"]["OpenOut"];
/** What the author asks of opening; anything left out is the server's default. */
export type OpenRequest = Partial<components["schemas"]["OpenIn"]>;
export type PrFile = components["schemas"]["PrFileOut"];
export type DraftSet = components["schemas"]["DraftsOut"];
export type Draft = components["schemas"]["DraftBody"];
export type DraftsUpdate = components["schemas"]["DraftsIn"];
export type PostPreview = components["schemas"]["PreviewOut"];
export type ReviewEvent = PostPreview["event"];
export type PostWarning = PostPreview["warnings"][number];
export type Round = components["schemas"]["RoundOut"];
export type StackView = components["schemas"]["StackOut"];
export type StackOpenResult = components["schemas"]["StackOpenOut"];
export type CrossFinding = components["schemas"]["CrossFindingOut"];
export type PostOutcome = components["schemas"]["PostOutcomeOut"];
export type PostAllItem = components["schemas"]["PostAllItemIn"];

/** The queue's sections (design pass pattern 2), in tab order. */
export type SectionId = "ready" | "waiting" | "loaded" | "approved";
const SECTION_INFO: Record<SectionId, { label: string; intro: string }> = {
  ready: {
    label: "Ready for you",
    intro: "Pre-reviewed with CI passing. Quick ones first; each opens in its own tab.",
  },
  waiting: {
    label: "Waiting",
    intro: "CI or an automated reviewer hasn't finished, or the pre-review hasn't run yet.",
  },
  loaded: { label: "Loaded", intro: "PRs you loaded: they jump the queue until closed or dismissed." },
  approved: {
    label: "Still approved",
    intro: "Rebased since you approved, unchanged, CI green or still running (or no CI).",
  },
};
const SECTION_ORDER: SectionId[] = ["ready", "waiting", "loaded", "approved"];
export const SECTIONS: { id: SectionId; label: string; intro: string }[] = SECTION_ORDER.map((id) => ({
  id,
  ...SECTION_INFO[id],
}));
export const sectionInfo = (id: SectionId) => SECTION_INFO[id];

/**
 * Which section a row belongs to. A row whose fetch failed is never ready (its cached state may
 * be out of date), and a suggested repo's PR is never filed as if it were queued for pre-review.
 */
export function sectionOf(row: QueueRow): SectionId {
  if (row.loaded) return "loaded";
  if (row.fetch_error !== null || row.prereview_state === "fetch_failed" || row.suggested) return "waiting";
  if (row.kind === "rebase") return "approved";
  if (row.prereview_state === "ready") return "ready";
  // "On open only": nothing runs until the PR is opened, so once CI passes it is ready to open.
  if (row.prereview_state === "not_started" && row.ci_state === "passing") return "ready";
  return "waiting";
}

export function groupRows(rows: QueueRow[]): Record<SectionId, QueueRow[]> {
  const out: Record<SectionId, QueueRow[]> = { ready: [], waiting: [], loaded: [], approved: [] };
  for (const r of rows) out[sectionOf(r)].push(r);
  return out;
}

/** A PR's route in the app. */
export function prPath(repo: string, number: number): string {
  return `/review/${repo}/${number}`;
}

const NUMBER = "([1-9][0-9]{0,8})";
const NAME = "([A-Za-z0-9_.-]+)";
const ROUTE = new RegExp(`^/review/${NAME}/${NAME}/${NUMBER}/?$`);

/** The PR a `/review/<owner>/<repo>/<n>` path names, or null for any other path. */
export function routeOf(pathname: string): { repo: string; number: number } | null {
  const m = ROUTE.exec(pathname);
  if (!m) return null;
  return { repo: `${m[1]}/${m[2]}`.toLowerCase(), number: Number(m[3]) };
}

const PASTED = [
  new RegExp(`^https://github\\.com/${NAME}/${NAME}/pull/${NUMBER}(?:[/?#].*)?$`),
  new RegExp(`^${NAME}/${NAME}#${NUMBER}$`),
  new RegExp(`^/review/${NAME}/${NAME}/${NUMBER}/?$`),
];

/** `owner/repo#n` for text that names a PR (a URL, shorthand or route), else null (for ⌘K). */
export function prRefFromText(text: string): string | null {
  const t = text.trim();
  for (const pattern of PASTED) {
    const m = pattern.exec(t);
    if (m) return `${m[1]}/${m[2]}#${m[3]}`;
  }
  return null;
}

/** The walkthrough's coverage: "Nothing to walk" when the diff changes no line (M3), never 100%. */
export function coverageText(row: QueueRow): string | null {
  if (row.nothing_to_walk) return "Nothing to walk";
  if (row.coverage_pct === null) return null;
  return `walk ${Math.round(row.coverage_pct)}%`;
}

/** A cause with its backticked commands split out, to show them as code (never as markup). */
export function splitCode(text: string): { code: boolean; text: string }[] {
  return text
    .split("`")
    .map((part, i) => ({ code: i % 2 === 1, text: part }))
    .filter((p) => p.text !== "");
}

/** The sync failed because GitHub has no usable token (spec §10: show the exact command). */
export function isAuthMissing(syncError: string | null): boolean {
  return syncError !== null && /gh auth login/.test(syncError);
}

const DAY = 86_400;

export function ageOf(createdAt: string, now: Date = new Date()): string {
  const secs = Math.round((now.getTime() - new Date(createdAt).getTime()) / 1000);
  if (Number.isNaN(secs)) return "";
  if (secs < 60) return "just opened";
  if (secs < 3600) return `${Math.floor(secs / 60)} min old`;
  if (secs < DAY) return `${Math.floor(secs / 3600)} h old`;
  const days = Math.floor(secs / DAY);
  return `${days} ${days === 1 ? "day" : "days"} old`;
}

const WEEK = 7 * DAY;

/**
 * How long ago a sync happened, for every range: "just now", "N min ago", "N h ago",
 * "N days ago", then "on <date>" past a week. Never a bare clock time, which reads as today
 * however old it is. Null for a time that can't be read.
 */
export function syncedAgo(at: string, now: Date = new Date()): string | null {
  const then = new Date(at);
  const secs = Math.round((now.getTime() - then.getTime()) / 1000);
  if (Number.isNaN(secs)) return null;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < DAY) return `${Math.floor(secs / 3600)} h ago`;
  if (secs < WEEK) {
    const days = Math.floor(secs / DAY);
    return `${days} ${days === 1 ? "day" : "days"} ago`;
  }
  return `on ${then.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" })}`;
}

/** What a structured review error carries besides its cause (`ReviewErrorOut`). */
export type ErrorFacts = {
  /** The typed error's name, such as `EarlierReviewPosted`; null for an untyped answer. */
  code: string | null;
  /** Seconds to wait before retrying the same action (PostingStillUncertain, PostingInProgress). */
  retryAfter: number | null;
  /** EarlierReviewPosted: the review GitHub already has. */
  reviewUrl: string | null;
  /** NeedsConfirmation: every warning to confirm. */
  warnings: PostWarning[] | null;
  /** InvalidDraftContent: the refused draft (0-based), or null for the summary. */
  draftIndex: number | null;
  /** A Post all that stopped: what was and wasn't posted, per PR. */
  stackOutcomes: PostOutcome[] | null;
};

const NO_FACTS: ErrorFacts = {
  code: null,
  retryAfter: null,
  reviewUrl: null,
  warnings: null,
  draftIndex: null,
  stackOutcomes: null,
};

/** A failed review request, carrying the server's cause (`detail`) as its message. */
export class ReviewRequestError extends Error {
  readonly facts: ErrorFacts;

  constructor(
    message: string,
    readonly status: number,
    facts: Partial<ErrorFacts> = {},
  ) {
    super(message);
    this.name = "ReviewRequestError";
    this.facts = { ...NO_FACTS, ...facts };
  }

  get code(): string | null {
    return this.facts.code;
  }

  /**
   * A newer round is current: the round shown changed (`RoundChanged`), or it was frozen when a
   * round on the PR's new head took over its drafts (`RoundFrozen`). Nothing was saved or posted.
   */
  get roundChanged(): boolean {
    return this.facts.code === "RoundChanged" || this.facts.code === "RoundFrozen";
  }

  /**
   * The server couldn't say whether the action happened, or asks to retry it later
   * (`Retry-After`): the retry is the same action, with the same key.
   */
  get isTransient(): boolean {
    return this.status >= 500 || this.facts.retryAfter !== null;
  }
}

function factsOf(error: unknown, response: Response): Partial<ErrorFacts> {
  const body = error !== null && typeof error === "object" ? (error as Record<string, unknown>) : {};
  const header = Number(response.headers.get("Retry-After"));
  const retry = typeof body.retry_after === "number" ? body.retry_after : header > 0 ? header : null;
  return {
    code: typeof body.error === "string" ? body.error : null,
    retryAfter: retry,
    reviewUrl: typeof body.review_url === "string" ? body.review_url : null,
    warnings: Array.isArray(body.warnings) ? (body.warnings as PostWarning[]) : null,
    draftIndex: typeof body.draft_index === "number" ? body.draft_index : null,
    stackOutcomes: Array.isArray(body.stack_outcomes) ? (body.stack_outcomes as PostOutcome[]) : null,
  };
}

/** The next step when Jesinia never answered (§9: the cause, then the fix). */
export const UNREACHABLE = "Check that Jesinia is running, then try again.";

function asSentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

export function errorCause(e: unknown): string {
  if (e instanceof ReviewRequestError) return asSentence(e.message);
  const raw = e instanceof Error ? e.message : String(e);
  return `Jesinia didn't answer (${raw}). ${UNREACHABLE}`;
}

/**
 * One idempotency key per user action. While an action's outcome is unknown (no answer, a 5xx)
 * a retry of it reuses its key, so the server acts at most once; once answered, the next try
 * is a new action with a new key.
 */
export class ActionKeys {
  private readonly keys = new Map<string, string>();

  keyFor(action: string): string {
    let key = this.keys.get(action);
    if (key === undefined) {
      key = crypto.randomUUID();
      this.keys.set(action, key);
    }
    return key;
  }

  /** Record how the action ended: `error` undefined for success. */
  settle(action: string, error?: unknown): void {
    const unknownOutcome = error !== undefined && (!(error instanceof ReviewRequestError) || error.isTransient);
    if (!unknownOutcome) this.keys.delete(action);
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

function unwrap<T>(r: Result<T>): T {
  if (!r.response.ok) {
    throw new ReviewRequestError(
      errorText(r.error, `HTTP ${r.response.status}`),
      r.response.status,
      factsOf(r.error, r.response),
    );
  }
  return r.data as T;
}

function split(repo: string): { owner: string; repo: string } {
  const [owner = "", name = ""] = repo.split("/");
  return { owner, repo: name };
}

export async function getQueue(client: JesiniaClient): Promise<Queue> {
  return unwrap(await client.GET("/api/review/queue"));
}

export async function getPr(client: JesiniaClient, repo: string, number: number): Promise<PrView> {
  return unwrap(
    await client.GET("/api/review/prs/{owner}/{repo}/{number}", { params: { path: { ...split(repo), number } } }),
  );
}

/** Load a PR for review now, from a URL, `owner/repo#n` or a review route. */
export async function loadPr(client: JesiniaClient, ref: string, key: string): Promise<QueueRow> {
  return unwrap(await client.POST("/api/review/load", { params: { header: withIdempotency(key) }, body: { ref } }));
}

export async function dismissPr(client: JesiniaClient, repo: string, number: number, key: string): Promise<void> {
  unwrap(
    await client.POST("/api/review/queue/{owner}/{repo}/{number}/dismiss", {
      params: { path: { ...split(repo), number }, header: withIdempotency(key) },
    }),
  );
}

/** Open without the CI and automated-review gates (failing CI still never approves). */
export async function reviewAnyway(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
): Promise<OpenResult> {
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/review-anyway", {
      params: { path: { ...split(repo), number }, header: withIdempotency(key) },
    }),
  );
}

/**
 * Fetch the PR from GitHub now (its new head, CI and reviews), checking the new head against
 * the author's previous review (Task 11 M7); opening it afterwards applies its own gates.
 */
export async function refreshPr(client: JesiniaClient, repo: string, number: number, key: string): Promise<QueueRow> {
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/refresh", {
      params: { path: { ...split(repo), number }, header: withIdempotency(key) },
    }),
  );
}

/** Run again what failed. */
export async function retryPr(client: JesiniaClient, repo: string, number: number, key: string): Promise<OpenResult> {
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/retry", {
      params: { path: { ...split(repo), number }, header: withIdempotency(key) },
    }),
  );
}

/** Confirm a suggested repo (R29a): watched and pre-reviewed from now on. */
export async function confirmRepo(client: JesiniaClient, repo: string, key: string): Promise<void> {
  unwrap(
    await client.POST("/api/review/repos/{owner}/{repo}/confirm", {
      params: { path: split(repo), header: withIdempotency(key) },
    }),
  );
}

type PrParams = { path: { owner: string; repo: string; number: number } };
const prParams = (repo: string, number: number): PrParams => ({ path: { ...split(repo), number } });

/**
 * Open the PR (the author is looking now): start what its round lacks, or say which gate holds
 * it. `request` may choose the depth, or ask to walk everything (spec §3.1).
 */
export async function openPr(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  request?: OpenRequest,
): Promise<OpenResult> {
  const params = { ...prParams(repo, number), header: withIdempotency(key) };
  const body = request ? { walk_everything: false, quiz: false, ...request } : undefined;
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/open", body ? { params, body } : { params }),
  );
}

export async function getDrafts(client: JesiniaClient, repo: string, number: number): Promise<DraftSet> {
  return unwrap(await client.GET("/api/review/prs/{owner}/{repo}/{number}/drafts", { params: prParams(repo, number) }));
}

/** Replace the latest round's summary and drafts (deleted ones stay deleted). */
export async function putDrafts(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  update: DraftsUpdate,
): Promise<DraftSet> {
  return unwrap(
    await client.PUT("/api/review/prs/{owner}/{repo}/{number}/drafts", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: update,
    }),
  );
}

/** Exactly what posting `event` would send, its warnings, and the digest to post it with. */
export async function previewPost(
  client: JesiniaClient,
  repo: string,
  number: number,
  event: ReviewEvent,
  roundId: number | null = null,
): Promise<PostPreview> {
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/post/preview", {
      params: prParams(repo, number),
      body: { event, round_id: roundId },
    }),
  );
}

/** Post the confirmed preview to GitHub; resolves to the review's URL. */
export async function postReview(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  post: { event: ReviewEvent; confirm: PostWarning[]; digest: string; round_id: number | null },
): Promise<string> {
  const posted = unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/post", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: post,
    }),
  );
  return posted.review_url;
}

/** Finish the latest round without posting. */
/** Finish the round shown (`roundId`) without posting; a newer round refuses it. */
export async function markDone(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  roundId: number | null = null,
): Promise<Round> {
  return unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/done", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: { round_id: roundId },
    }),
  );
}

/**
 * Record a finished quiz for the knowledge model (spec §3.1): how many of the answered questions
 * were right. The first result of a round counts; `recorded` is false for a later one.
 */
export async function recordQuiz(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  result: { roundId: number; right: number; answered: number },
): Promise<boolean> {
  const out = unwrap(
    await client.POST("/api/review/prs/{owner}/{repo}/{number}/quiz", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: { round_id: result.roundId, right: result.right, answered: result.answered },
    }),
  );
  return out.recorded;
}

/** The stack holding a PR (spec §4.3), from the cache: each PR's view, the stack review, cross-PR findings. */
export async function getStack(client: JesiniaClient, repo: string, number: number): Promise<StackView> {
  return unwrap(await client.GET("/api/review/stacks/{owner}/{repo}/{number}", { params: prParams(repo, number) }));
}

/** Open the stack: each PR under its own gates, then the stack's Overview, Background and walkthrough. */
export async function openStack(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  retry = false,
): Promise<StackOpenResult> {
  return unwrap(
    await client.POST("/api/review/stacks/{owner}/{repo}/{number}/open", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: { retry },
    }),
  );
}

/**
 * Post every PR's confirmed review, bottom-up; the server stops at the first that can't be posted
 * (a `ReviewRequestError` whose `facts.stackOutcomes` say what was posted). A retry reuses the key.
 */
export async function postAll(
  client: JesiniaClient,
  repo: string,
  number: number,
  key: string,
  items: PostAllItem[],
): Promise<PostOutcome[]> {
  const done = unwrap(
    await client.POST("/api/review/stacks/{owner}/{repo}/{number}/post-all", {
      params: { ...prParams(repo, number), header: withIdempotency(key) },
      body: { items },
    }),
  );
  return done.outcomes;
}
