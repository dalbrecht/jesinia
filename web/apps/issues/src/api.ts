// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes and calls the Issues PWA uses (`/api/issues`, Issues spec §6), and the pure rules it
// renders with. Every title, body, label, comment and agent reason here was written by someone
// who could open an issue, or by the agent: it is only ever rendered as text (R62, IR49).
import { errorText, withIdempotency, type components, type JesiniaClient } from "@jesinia/client";

type Schemas = components["schemas"];
export type Repo = Schemas["IssuesRepoOut"];
export type LabelSchema = Schemas["LabelSchemaBody"];
export type IssueRow = Schemas["IssueRowOut"];
export type IssueView = Schemas["IssueViewOut"];
export type IssueDetail = Schemas["IssueDetailOut"];
export type Suggestion = Schemas["IssueSuggestionOut"];
export type SuggestionList = Schemas["IssueSuggestionsOut"];
export type SuggestionRun = Schemas["IssueSuggestionRunOut"];
export type Pending = Schemas["PendingOut"];
export type PendingChange = Pending["change"];
/** A change the app may stage: anything the tray shows except a theme's label setup step. */
export type StageableChange = Schemas["PendingIn"]["change"];
export type PendingSource = Pending["source"];
export type ApplyBatch = Schemas["ApplyBatchOut"];
export type ApplyIssue = ApplyBatch["issues"][number];
export type Approval = Schemas["SuggestionApproveOut"];
export type Abandoned = Schemas["AbandonedOut"];
export type Dimension = "readiness" | "priority" | "type";
export type ViewName = "inbox" | "backlog";
export type SavedFilter = "incomplete" | "stale_90d" | "human_needed_could_be_ready" | "possible_duplicates";

export const SAVED_FILTERS: { id: SavedFilter; label: string }[] = [
  { id: "incomplete", label: "Incomplete" },
  { id: "stale_90d", label: "Stale: no activity in 90 days" },
  { id: "human_needed_could_be_ready", label: "human-needed that could be agent-ready" },
  { id: "possible_duplicates", label: "Possible duplicates" },
];

type Ref = { repo: string; number: number };

/** `owner/repo#n`. */
export const refOf = (issue: Ref): string => `${issue.repo}#${issue.number}`;

/** The issue an `owner/repo#n` names, or null for anything else. */
export function parseRef(value: string): { repo: string; number: number } | null {
  const m = /^([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,8})$/.exec(value);
  return m ? { repo: m[1] ?? "", number: Number(m[2]) } : null;
}

// --- routes ------------------------------------------------------------------------------------

export type Route =
  | { kind: "inbox" }
  | { kind: "backlog" }
  | { kind: "epics" }
  | { kind: "queues" }
  | { kind: "issue"; repo: string; number: number };

export function issuePath(repo: string, number: number): string {
  return `/issues/${repo}/${number}`;
}

const NAME = "([A-Za-z0-9_.-]+)";
const ISSUE_ROUTE = new RegExp(`^/issues/${NAME}/${NAME}/([1-9][0-9]{0,8})/?$`);
const FIXED = new RegExp("^/issues/(backlog|epics|queues)/?$");

/** The screen a path names; anything unknown is the Inbox. */
export function routeOf(pathname: string): Route {
  const issue = ISSUE_ROUTE.exec(pathname);
  if (issue) return { kind: "issue", repo: `${issue[1]}/${issue[2]}`.toLowerCase(), number: Number(issue[3]) };
  const fixed = FIXED.exec(pathname)?.[1];
  if (fixed === "backlog" || fixed === "epics" || fixed === "queues") return { kind: fixed };
  return { kind: "inbox" };
}

export function routePath(route: Route): string {
  return route.kind === "issue" ? issuePath(route.repo, route.number) : route.kind === "inbox" ? "/issues/" : `/issues/${route.kind}`;
} // prettier-ignore

// --- errors and keys ---------------------------------------------------------------------------

/** A failed Issues request, carrying the server's cause (`detail`) as its message. */
export class IssuesRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** The typed error's name, such as `EarlierBatchError`; null for an untyped answer. */
    readonly code: string | null,
    readonly retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "IssuesRequestError";
  }

  /** The server couldn't say whether the action happened: a retry is the same action. */
  get isTransient(): boolean {
    return this.status >= 500 || this.retryAfter !== null;
  }
}

/** What each typed refusal means, in the author's terms; the server's cause follows. */
const COPY: Record<string, string> = {
  SuggestionStaleError: "GitHub changed since this suggestion was made",
  SuggestionStateError: "This suggestion was already approved or dismissed, or is being approved",
  EarlierBatchError: "An earlier batch for this hasn't finished: retry or abandon it in Pending changes",
  PendingHeldError: "A queue item's Done holds this change: undo the Done in its queue",
  TrayChangeStagedError: "You already staged a change for this in Pending changes: undo it there first",
  NothingPendingError: "Nothing is staged to apply",
  BatchRunningError: "That batch is still running",
  BatchAbandonedError: "That batch was abandoned, so it can't run again",
  DuplicateNotCachedError: "The original issue isn't cached yet: sync its repo, then try again",
  IssueNotCachedError: "That issue isn't cached yet: sync its repo first",
  ContainmentRefusedError: "That epic can't hold that issue",
  PendingNotFoundError: "That change is no longer staged",
};

export const UNREACHABLE = "Check that Jesinia is running, then try again.";

function asSentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

export function errorCause(e: unknown): string {
  if (e instanceof IssuesRequestError) {
    const lead = e.code === null ? undefined : COPY[e.code];
    return lead === undefined ? asSentence(e.message) : `${lead}. ${asSentence(e.message)}`;
  }
  const raw = e instanceof Error ? e.message : String(e);
  return `Jesinia didn't answer (${raw}). ${UNREACHABLE}`;
}

/**
 * One idempotency key per user action. While an action's outcome is unknown (no answer, a 5xx)
 * a retry of it reuses its key, so the server acts at most once; once answered, the next try is
 * a new action with a new key.
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
    const unknownOutcome = error !== undefined && (!(error instanceof IssuesRequestError) || error.isTransient);
    if (!unknownOutcome) this.keys.delete(action);
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

function unwrap<T>(r: Result<T>): T {
  if (!r.response.ok) {
    const body = r.error !== null && typeof r.error === "object" ? (r.error as Record<string, unknown>) : {};
    const header = Number(r.response.headers.get("Retry-After"));
    const retry = typeof body.retry_after === "number" ? body.retry_after : header > 0 ? header : null;
    throw new IssuesRequestError(
      errorText(r.error, `HTTP ${r.response.status}`),
      r.response.status,
      typeof body.error === "string" ? body.error : null,
      retry,
    );
  }
  return r.data as T;
}

function split(repo: string): { owner: string; repo: string } {
  const [owner = "", name = ""] = repo.split("/");
  return { owner, repo: name };
}

// --- calls -------------------------------------------------------------------------------------

export async function getRepos(client: JesiniaClient): Promise<Repo[]> {
  return unwrap(await client.GET("/api/issues/repos")).repos;
}

export async function getView(
  client: JesiniaClient,
  view: ViewName,
  filters: { repo?: string | undefined; filter?: SavedFilter | undefined } = {},
): Promise<IssueView> {
  const query: { repo?: string; filter?: string } = {};
  if (filters.repo) query.repo = filters.repo;
  if (filters.filter) query.filter = filters.filter;
  return unwrap(await client.GET("/api/issues/{view}", { params: { path: { view }, query } }));
}

const itemParams = (repo: string, n: number) => ({ path: { ...split(repo), n } });

export async function getItem(client: JesiniaClient, repo: string, n: number): Promise<IssueDetail> {
  return unwrap(await client.GET("/api/issues/item/{owner}/{repo}/{n}", { params: itemParams(repo, n) }));
}

/** Fetch the issue's comments and timeline (and an epic's children) from GitHub now. */
export async function refreshItem(client: JesiniaClient, repo: string, n: number, key: string): Promise<IssueDetail> {
  return unwrap(
    await client.POST("/api/issues/item/{owner}/{repo}/{n}/refresh", {
      params: { ...itemParams(repo, n), header: withIdempotency(key) },
    }),
  );
}

/** Open suggestions: one issue's (with its latest run), or a repo's. */
export async function getSuggestions(
  client: JesiniaClient,
  query: { repo: string; n?: number },
): Promise<SuggestionList> {
  return unwrap(await client.GET("/api/issues/suggestions", { params: { query } }));
}

export async function runSuggestions(
  client: JesiniaClient,
  refs: { repo: string; n: number }[],
  key: string,
): Promise<void> {
  unwrap(
    await client.POST("/api/issues/suggestions/run", { params: { header: withIdempotency(key) }, body: { refs } }),
  );
}

export async function editSuggestion(
  client: JesiniaClient,
  id: string,
  payload: Record<string, string>,
  key: string,
): Promise<Suggestion> {
  return unwrap(
    await client.PATCH("/api/issues/suggestions/{suggestion_id}", {
      params: { path: { suggestion_id: id }, header: withIdempotency(key) },
      body: { payload },
    }),
  );
}

export async function dismissSuggestion(client: JesiniaClient, id: string, key: string): Promise<Suggestion> {
  return unwrap(
    await client.POST("/api/issues/suggestions/{suggestion_id}/dismiss", {
      params: { path: { suggestion_id: id }, header: withIdempotency(key) },
    }),
  );
}

/** Approve: applies to GitHub at once, in the background (202 with the batch to poll). */
export async function approveSuggestion(client: JesiniaClient, id: string, key: string): Promise<Approval> {
  return unwrap(
    await client.POST("/api/issues/suggestions/{suggestion_id}/approve", {
      params: { path: { suggestion_id: id }, header: withIdempotency(key) },
    }),
  );
}

export async function getPending(client: JesiniaClient): Promise<Pending[]> {
  return unwrap(await client.GET("/api/issues/pending")).changes;
}

export async function stageChange(
  client: JesiniaClient,
  stage: { repo: string; n: number; change: StageableChange; source: PendingSource },
  key: string,
): Promise<Pending> {
  return unwrap(await client.POST("/api/issues/pending", { params: { header: withIdempotency(key) }, body: stage }));
}

export async function unstageChange(client: JesiniaClient, id: string, key: string): Promise<void> {
  unwrap(
    await client.DELETE("/api/issues/pending/{pending_id}", {
      params: { path: { pending_id: id }, header: withIdempotency(key) },
    }),
  );
}

export async function clearPending(client: JesiniaClient, key: string): Promise<void> {
  unwrap(await client.DELETE("/api/issues/pending", { params: { header: withIdempotency(key) } }));
}

/** Apply the whole tray as one batch, in the background (202 with the batch to poll). */
export async function applyPending(client: JesiniaClient, key: string): Promise<ApplyBatch> {
  return unwrap(await client.POST("/api/issues/pending/apply", { params: { header: withIdempotency(key) }, body: {} }));
}

export async function getBatch(client: JesiniaClient, id: string): Promise<ApplyBatch> {
  return unwrap(await client.GET("/api/issues/pending/{batch}", { params: { path: { batch: id } } }));
}

export async function retryBatch(client: JesiniaClient, id: string, key: string): Promise<ApplyBatch> {
  return unwrap(
    await client.POST("/api/issues/pending/{batch}/retry", {
      params: { path: { batch: id }, header: withIdempotency(key) },
    }),
  );
}

/** Give up on an unfinished batch that creates issues; what it created is reused (IR58). */
export async function abandonBatch(client: JesiniaClient, id: string, key: string): Promise<Abandoned> {
  return unwrap(
    await client.POST("/api/issues/pending/{batch}/abandon", {
      params: { path: { batch: id }, header: withIdempotency(key) },
      body: { created: [] },
    }),
  );
}

// --- how things read ---------------------------------------------------------------------------

type Target = { repo: string; n: number };
const targetRef = (t: Target) => `${t.repo}#${t.n}`;
const CLOSE_REASON: Record<"completed" | "not_planned" | "duplicate", string> = {
  completed: "completed",
  not_planned: "not planned",
  duplicate: "a duplicate",
};

/** A staged change in a few words, for the tray. */
export function describeChange(change: PendingChange): string {
  switch (change.kind) {
    case "set_dimension":
      return `Set ${change.dimension} to ${change.value}`;
    case "add_area":
      return `Add area ${change.value}`;
    case "remove_area":
      return `Remove area ${change.value}`;
    case "add_label":
      return `Add label ${change.name}`;
    case "ensure_label":
      return `Create label ${change.name}`;
    case "set_parent":
      return change.parent ? `Move under ${targetRef(change.parent)}` : "Remove from its epic";
    case "reorder_child":
      return change.after
        ? `Move ${targetRef(change.child)} after ${targetRef(change.after)}`
        : `Move ${targetRef(change.child)} to the top`;
    case "close":
      return change.duplicate_of
        ? `Close as a duplicate of ${targetRef(change.duplicate_of)}`
        : `Close as ${CLOSE_REASON[change.reason]}`;
    case "comment":
      return "Comment";
  }
}

const text = (value: unknown): string => (typeof value === "string" ? value : String(value));

/** A suggestion in a few words (Jesinia's own words around the agent's values). */
export function describeSuggestion(s: Suggestion): string {
  const p = s.payload;
  switch (s.kind) {
    case "label":
      return `Set ${text(p.dimension)} to ${text(p.value)}`;
    case "new_area":
      return `Add a new area: ${text(p.value)}`;
    case "duplicate":
      return `Duplicate of ${text(p.repo)}#${text(p.n)}`;
    case "parent":
      return `Belongs to epic ${text(p.repo)}#${text(p.n)}`;
    case "readiness_gap": {
      if (p.ready === true) return "Could be agent-ready";
      const missing = Array.isArray(p.missing) ? p.missing.map(text).join(", ") : "";
      return `Not agent-ready yet: missing ${missing}`;
    }
    default:
      return s.kind;
  }
}

/** The batch's state in one line. */
export function batchHeadline(batch: ApplyBatch): string {
  if (batch.status === "running") return "Applying…";
  if (batch.status === "interrupted") return "Interrupted by a stop: Retry resumes it";
  if (batch.status === "abandoned") return "Abandoned";
  if (batch.stopped !== null) return `GitHub stopped this batch: ${batch.stopped}`;
  return batch.complete ? "All changes applied" : "Some changes didn't apply";
}

export type IssueOutcome = { state: "applied" | "failed" | "skipped"; text: string; conflict: boolean };

/** One issue's result: applied, or the first step that failed or was skipped, with its cause. */
export function issueResult(issue: { steps: ApplyIssue["steps"] }): IssueOutcome {
  const failed = issue.steps.find((step) => step.state === "failed");
  if (failed) {
    return { state: "failed", text: `${failed.kind} failed: ${failed.cause ?? "no cause given"}`, conflict: failed.conflict === true };
  } // prettier-ignore
  const skipped = issue.steps.find((step) => step.state === "skipped");
  if (skipped) {
    return { state: "skipped", text: `${skipped.kind} skipped: ${skipped.cause ?? "no cause given"}`, conflict: false };
  } // prettier-ignore
  return { state: "applied", text: "Applied", conflict: false };
}

/** Whether a batch makes new issues (its origin is then abandonable, IR58). */
export const createsIssues = (batch: ApplyBatch): boolean =>
  batch.repo_steps.some((step) => step.kind === "create_issue");

export type StatusId = "triage" | "backlog" | "in_progress" | "in_review" | "done";
export const STATUS_COLUMNS: { id: StatusId; label: string }[] = [
  { id: "triage", label: "Triage" },
  { id: "backlog", label: "Backlog" },
  { id: "in_progress", label: "In progress" },
  { id: "in_review", label: "In review" },
  { id: "done", label: "Done" },
];

/**
 * The derived status (spec §6.2): Done once closed or its PR merged; In progress while a linked
 * PR is open; Triage while incomplete; else Backlog. In review needs a PR's review state, which
 * the cache doesn't hold yet, so nothing lands there for now.
 */
export function statusOf(row: IssueRow): StatusId {
  if (row.state === "closed" || row.linked_pr_state === "merged") return "done";
  if (row.linked_pr_state === "open") return "in_progress";
  return row.complete ? "backlog" : "triage";
}

// --- freshness ---------------------------------------------------------------------------------

const DAY = 86_400;
const WEEK = 7 * DAY;

/** How long ago a sync happened, never as a bare clock time; null for an unreadable time. */
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

/** "Updated 20 min ago · refreshing…" (spec §6.3). */
export function freshness(view: IssueView, now: Date = new Date()): string {
  const ago = view.synced_at === null ? null : syncedAgo(view.synced_at, now);
  const base = ago === null ? "Not updated yet" : `Updated ${ago}`;
  return view.syncing ? `${base} · refreshing…` : base;
}

/** While a sync runs (or the first is due) the cache is read often. */
export const SYNCING_POLL_MS = 2000;
/** Otherwise the background sync (every 5 minutes) is picked up at this pace. */
export const IDLE_POLL_MS = 60_000;

/** When to read a list again: often while syncing, backing off while it can't be read. */
export function nextPollMs(view: IssueView | null, failedReads: number): number {
  if (failedReads > 0) return Math.min(IDLE_POLL_MS, SYNCING_POLL_MS * 2 ** (failedReads - 1));
  if (view === null) return SYNCING_POLL_MS;
  return view.syncing || view.synced_at === null ? SYNCING_POLL_MS : IDLE_POLL_MS;
}

/** A cause with its backticked commands split out, to show them as code (never as markup). */
export function splitCode(value: string): { code: boolean; text: string }[] {
  return value
    .split("`")
    .map((part, i) => ({ code: i % 2 === 1, text: part }))
    .filter((p) => p.text !== "");
}

/** The schema values of `dimension` across `repos` (first repo's order first). */
export function schemaValues(repos: Repo[], dimension: Dimension): string[] {
  const seen: string[] = [];
  for (const r of repos) for (const c of r.label_schema[dimension]) if (!seen.includes(c.name)) seen.push(c.name);
  return seen;
}
