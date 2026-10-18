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
  // GitHub's casing is kept (the server casefolds only to compare); tabs compare without case.
  if (issue) return { kind: "issue", repo: `${issue[1]}/${issue[2]}`, number: Number(issue[3]) };
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
    /** `EarlierBatchError`: the batch to retry or abandon first. */
    readonly batchId: string | null = null,
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
  GroupEpicExistsError: "An epic for these issues already exists: add them to it instead",
  ThemeEpicClosedError: "The theme's epic is closed: reopen it on GitHub, or ask for a new epic",
  BriefEditedOnGitHubError: "The brief was edited on GitHub since it was posted: refresh, or overwrite it",
  BriefRecoveryIncompleteError: "Jesinia couldn't tell whether the brief was posted: check GitHub",
  ThemeNearDuplicateError: "That's very close to a tag you already have: use that one",
  ThemesUnavailableError: "Themes need the knowledge base, which isn't running",
  NothingToPublishError: "Every issue of the theme in that repo has its label already",
  ConversionOutdatedError: "The checklist changed since the preview: preview it again",
  NothingToConvertError: "Nothing in the checklist is left to convert",
  QueueNameTakenError: "A queue with that name exists already",
  QueueItemExistsError: "That's in the queue already",
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
      typeof body.batch_id === "string" ? body.batch_id : null,
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

/** A value without its dimension's own prefix: "priority: high" reads "high" after "priority". */
export function shortValue(dimension: string, value: string): string {
  const prefix = `${dimension}:`;
  const short = value.toLowerCase().startsWith(prefix) ? value.slice(prefix.length).trim() : value;
  return short || value;
}

/** Line endings are ordinary text: only what lies between them is checked for hidden characters
 * (R62 tokens are for bidi and invisible characters, never a newline). */
export function revealLines(value: string, reveal: (line: string) => string): string {
  return value
    .split(/(\r\n|\n|\r)/)
    .map((part, i) => (i % 2 === 1 ? part : reveal(part)))
    .join("");
}

/** Take a settled batch out of the tray for good (a reload won't list it again). */
export async function dismissBatch(client: JesiniaClient, id: string, key: string): Promise<void> {
  unwrap(
    await client.POST("/api/issues/pending/{batch}/dismiss", {
      params: { path: { batch: id }, header: withIdempotency(key) },
    }),
  );
}

/** The batches the server says didn't finish (for the tray after a reload). */
export async function getUnfinishedBatches(client: JesiniaClient): Promise<ApplyBatch[]> {
  return unwrap(await client.GET("/api/issues/batches")).batches;
}

/** A staged change in a few words, for the tray. */
export function describeChange(change: PendingChange): string {
  switch (change.kind) {
    case "set_dimension":
      return `Set ${change.dimension} to ${shortValue(change.dimension, change.value)}`;
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
    case "group": {
      const count = Array.isArray(p.issues) ? p.issues.length : 0;
      const epic = p.epic as { repo?: unknown; n?: unknown } | undefined;
      return epic
        ? `Add ${count === 1 ? "1 issue" : `${count} issues`} to ${text(epic.repo)}#${text(epic.n)}`
        : `A new epic for ${count} issues`;
    }
    case "split": {
      const whole = p.issue as { repo?: unknown; n?: unknown } | undefined;
      const parts = Array.isArray(p.parts) ? p.parts.length : 0;
      return `Split ${text(whole?.repo)}#${text(whole?.n)} into ${parts} parts`;
    }
    case "brief":
      return "A delivery brief";
    case "queue_next":
      return "Items to add next";
    case "queue_order":
      return "A new order";
    case "queue_split":
      return "Which queue each item fits";
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

export type IssueOutcome = { state: "applied" | "failed" | "skipped" | "pending"; text: string; conflict: boolean };

/**
 * One issue's result: applied, or the first step that failed or was skipped, with its cause.
 * While its batch runs, steps not reached yet are reported skipped ("not run yet"): pending.
 */
export function issueResult(issue: { steps: ApplyIssue["steps"] }, isRunning = false): IssueOutcome {
  const failed = issue.steps.find((step) => step.state === "failed");
  if (!failed && isRunning && issue.steps.some((step) => step.state !== "done")) {
    return { state: "pending", text: "In progress", conflict: false };
  }
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
 * PR is open (sync reads the linked PRs of the issues it sees change); Triage while incomplete;
 * else Backlog. The spec names In review without defining it, and the cache holds a linked PR's
 * state only (open, merged, closed), not its review state, so nothing lands there yet.
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

// --- epics, queues and themes (Task 13) --------------------------------------------------------

export type Epic = Schemas["EpicOut"];
export type EpicDetail = Schemas["EpicDetailOut"];
export type Member = Schemas["MemberOut"];
export type Readiness = Schemas["ReadinessOut"];
export type ConversionPlan = Schemas["ConversionPlanOut"];
export type PlanChild = Schemas["ConversionChildOut"];
export type AskRun = Schemas["IssueAskRunOut"];
export type Milestone = Schemas["MilestoneDetailOut"];
export type QueueSummary = Schemas["WorkQueueSummaryOut"];
export type Queue = Schemas["WorkQueueOut"];
export type QueueItem = Schemas["WorkQueueItemOut"];
export type QueueAction = Schemas["WorkQueueActionOut"];
export type QueueApproval = Schemas["WorkQueueApprovalOut"];
export type ItemKind = QueueItem["kind"];
export type Theme = Schemas["ThemeSummaryOut"];
export type ThemeDetail = Schemas["ThemeOut"];
export type ThemeAdded = Schemas["ThemeAddOut"];
export type ThemePublished = Schemas["ThemePublishOut"];
export type ThemeEpicPlan = Schemas["ThemeEpicPlanOut"];
export type IssueTarget = { repo: string; n: number };

const epicParams = (repo: string, n: number) => ({ path: { ...split(repo), n } });

export async function getEpics(client: JesiniaClient, repo?: string): Promise<Epic[]> {
  const query = repo ? { repo } : {};
  return unwrap(await client.GET("/api/issues/epics", { params: { query } })).epics;
}

export async function getEpic(client: JesiniaClient, repo: string, n: number): Promise<EpicDetail> {
  return unwrap(await client.GET("/api/issues/epics/{owner}/{repo}/{n}", { params: epicParams(repo, n) }));
}

export async function getConversion(client: JesiniaClient, repo: string, n: number): Promise<ConversionPlan> {
  return unwrap(await client.GET("/api/issues/epics/{owner}/{repo}/{n}/conversion", { params: epicParams(repo, n) }));
}

/** Approve the conversion previewed with `digest`: one batch, in the background (202). */
export async function approveConversion(
  client: JesiniaClient,
  repo: string,
  n: number,
  digest: string,
  key: string,
): Promise<ApplyBatch> {
  return unwrap(
    await client.POST("/api/issues/epics/{owner}/{repo}/{n}/conversion", {
      params: { ...epicParams(repo, n), header: withIdempotency(key) },
      body: { digest },
    }),
  );
}

/** Ask the agent for the epic's brief, in the background; poll its `scope`. */
export async function askBrief(client: JesiniaClient, repo: string, n: number, key: string): Promise<AskRun> {
  return unwrap(
    await client.POST("/api/issues/epics/{owner}/{repo}/{n}/brief/draft", {
      params: { ...epicParams(repo, n), header: withIdempotency(key) },
    }),
  );
}

/** Post the brief (or edit its comment); `overwrite` replaces one edited on GitHub since. */
export async function postBrief(
  client: JesiniaClient,
  repo: string,
  n: number,
  body: { text: string; suggestion_id?: string | null; overwrite: boolean },
  key: string,
): Promise<Schemas["EpicBriefOut"]> {
  return unwrap(
    await client.POST("/api/issues/epics/{owner}/{repo}/{n}/brief", {
      params: { ...epicParams(repo, n), header: withIdempotency(key) },
      body,
    }),
  );
}

/** Ask the agent for groups (candidate epics, additions) in one repo or all. */
export async function askGroups(client: JesiniaClient, repo: string | null, key: string): Promise<AskRun> {
  return unwrap(
    await client.POST("/api/issues/epics/proposals", { params: { header: withIdempotency(key) }, body: { repo } }),
  );
}

/** Ask the agent how to split an issue too big for one PR. */
export async function askSplit(client: JesiniaClient, repo: string, n: number, key: string): Promise<AskRun> {
  return unwrap(
    await client.POST("/api/issues/suggestions/split", {
      params: { header: withIdempotency(key) },
      body: { repo, n },
    }),
  );
}

/** Drafts (and the latest background ask) of one ask `scope`, or of one `kind`. */
export async function getDrafts(
  client: JesiniaClient,
  query: { scope?: string; kind?: string; repo?: string },
): Promise<SuggestionList> {
  return unwrap(await client.GET("/api/issues/suggestions", { params: { query } }));
}

export async function getMilestone(client: JesiniaClient, repo: string, n: number): Promise<Milestone> {
  return unwrap(await client.GET("/api/issues/milestones/{owner}/{repo}/{n}", { params: epicParams(repo, n) }));
}

export async function getQueues(client: JesiniaClient): Promise<QueueSummary[]> {
  return unwrap(await client.GET("/api/issues/queues")).queues;
}

export async function createQueue(client: JesiniaClient, name: string, key: string): Promise<Queue> {
  return unwrap(
    await client.POST("/api/issues/queues", {
      params: { header: withIdempotency(key) },
      body: { name, description: "" },
    }),
  );
}

export async function getQueue(client: JesiniaClient, id: string): Promise<Queue> {
  return unwrap(await client.GET("/api/issues/queues/{queue_id}", { params: { path: { queue_id: id } } }));
}

export async function addQueueItem(
  client: JesiniaClient,
  id: string,
  item: { kind: ItemKind; repo: string; n: number; position?: number },
  key: string,
): Promise<Queue> {
  return unwrap(
    await client.POST("/api/issues/queues/{queue_id}/items", {
      params: { path: { queue_id: id }, header: withIdempotency(key) },
      body: item,
    }),
  );
}

type ItemRef = { kind: ItemKind; repo: string; n: number };
const itemPath = (id: string, item: ItemRef) => ({
  path: { queue_id: id, kind: item.kind, ...split(item.repo), n: item.n },
});

/** Move an open item to `position` among the open items (local, at once). */
export async function moveQueueItem(
  client: JesiniaClient,
  id: string,
  item: ItemRef,
  position: number,
  key: string,
): Promise<Queue> {
  return unwrap(
    await client.PATCH("/api/issues/queues/{queue_id}/items/{kind}/{owner}/{repo}/{n}", {
      params: { ...itemPath(id, item), header: withIdempotency(key) },
      body: { position },
    }),
  );
}

export async function removeQueueItem(client: JesiniaClient, id: string, item: ItemRef, key: string): Promise<Queue> {
  return unwrap(
    await client.DELETE("/api/issues/queues/{queue_id}/items/{kind}/{owner}/{repo}/{n}", {
      params: { ...itemPath(id, item), header: withIdempotency(key) },
    }),
  );
}

/** Done: the item moves to the Done group at once, and its close (or agent-ready) is staged. */
export async function doneQueueItem(
  client: JesiniaClient,
  id: string,
  item: ItemRef,
  body: { then: "close" | "agent_ready"; comment?: string },
  key: string,
): Promise<QueueAction> {
  return unwrap(
    await client.POST("/api/issues/queues/{queue_id}/items/{kind}/{owner}/{repo}/{n}/done", {
      params: { ...itemPath(id, item), header: withIdempotency(key) },
      body,
    }),
  );
}

export async function undoQueueItem(client: JesiniaClient, id: string, item: ItemRef, key: string): Promise<Queue> {
  return unwrap(
    await client.POST("/api/issues/queues/{queue_id}/items/{kind}/{owner}/{repo}/{n}/undo", {
      params: { ...itemPath(id, item), header: withIdempotency(key) },
    }),
  );
}

/** A drop into a priority band: reorders at once, and stages the priority when the band differs. */
export async function bandQueueItem(
  client: JesiniaClient,
  id: string,
  item: ItemRef,
  band: string,
  position: number,
  key: string,
): Promise<QueueAction> {
  return unwrap(
    await client.POST("/api/issues/queues/{queue_id}/items/{kind}/{owner}/{repo}/{n}/band", {
      params: { ...itemPath(id, item), header: withIdempotency(key) },
      body: { band, position },
    }),
  );
}

export type QueueAsk = "next" | "order";

export async function askQueue(client: JesiniaClient, id: string, what: QueueAsk, key: string): Promise<AskRun> {
  const params = { path: { queue_id: id }, header: withIdempotency(key) };
  return unwrap(
    what === "next"
      ? await client.POST("/api/issues/queues/{queue_id}/suggestions/next", { params })
      : await client.POST("/api/issues/queues/{queue_id}/suggestions/order", { params }),
  );
}

export async function askQueueSplit(client: JesiniaClient, ids: string[], key: string): Promise<AskRun> {
  return unwrap(
    await client.POST("/api/issues/queues/suggestions/split", {
      params: { header: withIdempotency(key) },
      body: { queue_ids: ids },
    }),
  );
}

/** Approve a queue draft: only the local queues change. */
export async function approveQueueDraft(client: JesiniaClient, id: string, key: string): Promise<QueueApproval> {
  return unwrap(
    await client.POST("/api/issues/queues/suggestions/{suggestion_id}/approve", {
      params: { path: { suggestion_id: id }, header: withIdempotency(key) },
    }),
  );
}

export const queueScope = (what: QueueAsk, id: string): string => `queue_${what}:${id}`;
export const splitScope = (ids: string[]): string => `queue_split:${[...new Set(ids)].sort().join(",")}`;
export const groupsScope = (repo: string | null): string => `groups:${repo === null ? "*" : repo.toLowerCase()}`;
export const issueSplitScope = (repo: string, n: number): string => `split:${repo.toLowerCase()}#${n}`;
export const briefScope = (repo: string, n: number): string => `brief:${repo.toLowerCase()}#${n}`;

export async function getThemes(client: JesiniaClient): Promise<Theme[]> {
  return unwrap(await client.GET("/api/issues/themes")).themes;
}

export async function getTheme(client: JesiniaClient, tag: string): Promise<ThemeDetail> {
  return unwrap(await client.GET("/api/issues/themes/{tag}", { params: { path: { tag } } }));
}

/** Give issues a theme (local, at once); `added` is what an Undo takes off again. */
export async function addTheme(client: JesiniaClient, tag: string, issues: IssueTarget[], key: string): Promise<ThemeAdded> {
  return unwrap(
    await client.POST("/api/issues/themes/{tag}/issues", {
      params: { path: { tag }, header: withIdempotency(key) },
      body: { issues },
    }),
  );
} // prettier-ignore

export async function removeTheme(client: JesiniaClient, tag: string, issues: IssueTarget[], key: string): Promise<void> {
  unwrap(
    await client.POST("/api/issues/themes/{tag}/issues/remove", {
      params: { path: { tag }, header: withIdempotency(key) },
      body: { issues },
    }),
  );
} // prettier-ignore

/** Stage the theme's label setup step and label additions in the tray. */
export async function publishTheme(client: JesiniaClient, tag: string, repo: string, key: string): Promise<ThemePublished> {
  return unwrap(
    await client.POST("/api/issues/themes/{tag}/publish", {
      params: { path: { tag }, header: withIdempotency(key) },
      body: { repo },
    }),
  );
} // prettier-ignore

export async function getThemeEpic(
  client: JesiniaClient,
  tag: string,
  repo: string,
  newEpic: boolean,
): Promise<ThemeEpicPlan> {
  return unwrap(
    await client.GET("/api/issues/themes/{tag}/epic", {
      params: { path: { tag }, query: newEpic ? { repo, new_epic: true } : { repo } },
    }),
  );
}

export async function approveThemeEpic(
  client: JesiniaClient,
  tag: string,
  body: { repo: string; digest: string; new_epic: boolean },
  key: string,
): Promise<ApplyBatch> {
  return unwrap(
    await client.POST("/api/issues/themes/{tag}/epic", {
      params: { path: { tag }, header: withIdempotency(key) },
      body,
    }),
  );
}

/** Whether a refusal is the typed error `code`. */
export const isRefusal = (e: unknown, code: string): e is IssuesRequestError =>
  e instanceof IssuesRequestError && e.code === code;

/** The issue a refusal's cause names in parentheses, "(owner/repo#n)" (an existing epic). */
export function refInCause(cause: string): { repo: string; number: number } | null {
  const m = /\(([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+#[1-9][0-9]{0,8})\)/.exec(cause);
  return m?.[1] ? parseRef(m[1]) : null;
}

/** An epic's readiness in a few words. */
export function readinessText(r: Readiness): string {
  if (r.ready) return "Ready";
  if (r.blockers.length > 0) return `${r.blockers.length} blocking`;
  return r.note ?? "Not ready";
}

/** Why `target` can't hold `dragged` (an epic or issue), or null (ruling IR17's shape). The cached
 * parent chain is walked (`parentOf`); GitHub checks again at apply time. */
export function containmentRefusal(
  target: { repo: string; number: number; is_initiative: boolean },
  dragged: { repo: string; number: number; is_initiative?: boolean },
  parentOf: (ref: string) => string | null,
): { code: string; reason: string } | null {
  const targetRef = refOf(target).toLowerCase();
  const draggedRef = refOf(dragged).toLowerCase();
  if (targetRef === draggedRef) return { code: "self", reason: "An epic can't contain itself" };
  const owner = (repo: string) => repo.split("/")[0]?.toLowerCase();
  if (owner(target.repo) !== owner(dragged.repo)) {
    return { code: "cross_owner", reason: "GitHub's sub-issues need the same owner" };
  }
  if (dragged.is_initiative === true && !target.is_initiative) {
    return { code: "initiative", reason: "An initiative sits above epics, so an epic can't contain it" };
  }
  const seen = new Set<string>();
  for (let at = parentOf(targetRef); at !== null && !seen.has(at); at = parentOf(at)) {
    if (at === draggedRef) return { code: "cycle", reason: `${refOf(dragged)} contains ${refOf(target)}, so it can't go inside it` }; // prettier-ignore
    seen.add(at);
  }
  return null;
}

/**
 * A staged parent link (ruling IR39): made against the parent the issue has now (`current`,
 * "owner/repo#n" or null), so a move on GitHub since is a conflict at apply time. The server
 * records the expected parent from its cache when staging; this states the same intent.
 */
export function setParentChange(parent: IssueTarget | null, current: string | null): StageableChange {
  const was = current === null ? null : parseRef(current);
  return {
    kind: "set_parent",
    parent,
    expects_parent: true,
    expected_parent: was === null ? null : { repo: was.repo, n: was.number },
  };
}
