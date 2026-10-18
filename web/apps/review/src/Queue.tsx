// SPDX-License-Identifier: AGPL-3.0-or-later
// The queue (spec §2, design pass `review-queue.html`): a repo filter on the left; the Load PR
// field; sections as tabs with counts, each with an intro bar and its bulk actions; rows as
// cards; and a summary strip of the other sections. PR text is rendered only as text.
import { Badge, Button, Tabs } from "@jesinia/design";
import { useId, useState, type ReactNode } from "react";
import {
  ageOf,
  coverageText,
  groupRows,
  SECTIONS,
  sectionInfo,
  type Queue as QueueData,
  type QueueRow,
  type SectionId,
} from "./api";
import { LoadPr } from "./LoadPr";
import { visibleText } from "./pr/model";
import { verdictWords } from "./pr/Recommendation";
import { Visible } from "./pr/Visible";
import s from "./review.module.css";

export const SUGGESTED_NOTE = "Suggested repo — confirm to review automatically";

/** The row's own handle, as GitHub writes it. */
export const refOf = (row: { display_repo: string; number: number }) => `${row.display_repo}#${row.number}`;

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const KIND: Record<QueueRow["kind"], string> = { new: "New", rereview: "Re-review", rebase: "Rebase", stack: "Stack" };
const CI: Record<
  QueueRow["ci_state"],
  { text: string; tone: "success" | "warn" | "danger" | "neutral"; icon: string }
> = {
  passing: { text: "CI passing", tone: "success", icon: "✓" },
  pending: { text: "CI running", tone: "warn", icon: "…" },
  failing: { text: "CI failing", tone: "danger", icon: "✕" },
  none: { text: "No CI", tone: "neutral", icon: "–" },
};

/** The recommendation as the queue shows it (glyph and words, never color alone). */
export const VERDICT: Record<string, { text: string; tone: "success" | "danger" | "warn" }> = {
  approve: { text: "✓ Approve", tone: "success" },
  request_changes: { text: "✕ Request changes", tone: "danger" },
  needs_closer_look: { text: "? Needs a closer look", tone: "warn" },
};

/** What the pre-review is doing, in words; `tone` marks what needs the author. */
function stateOf(row: QueueRow): { text: string; tone: "neutral" | "warn" | "danger" | "success" } {
  if (row.fetch_error !== null || row.prereview_state === "fetch_failed") {
    return { text: `Couldn't load: ${row.fetch_error ?? row.prereview_reason}`, tone: "danger" };
  }
  switch (row.prereview_state) {
    case "waiting_ci":
      return { text: "Waiting for CI", tone: "warn" };
    case "ci_failing":
      return { text: "CI failing", tone: "danger" };
    case "waiting_auto_review":
      return { text: "Waiting for automated review", tone: "warn" };
    case "debouncing":
      return { text: "Waiting for pushes to settle", tone: "warn" };
    case "queued":
      return { text: "Pre-review queued", tone: "neutral" };
    case "running":
      return { text: "Pre-review running", tone: "neutral" };
    case "ready":
      return { text: "Pre-review ready", tone: "success" };
    case "failed":
      return { text: "Pre-review failed", tone: "danger" };
    default:
      return { text: "Runs when you open it", tone: "neutral" };
  }
}

const STATE_ICON = { neutral: "•", warn: "!", danger: "✕", success: "✓" } as const;

function reviewsOf(row: QueueRow): string[] {
  const out: string[] = [];
  if (row.auto_review_state === "pending" && row.bot_review_count === 0) out.push("Automated review pending");
  else if (row.bot_review_count > 0 || row.auto_review_state === "landed") {
    out.push(plural(row.bot_review_count, "automated review"));
  }
  out.push(plural(row.human_review_count, "human review"));
  out.push(plural(row.review_comment_count, "review comment"));
  return out;
}

function evidenceOf(row: QueueRow): string[] {
  const e = row.evidence;
  if (!e) return [];
  const findings = Object.entries(e.findings)
    .map(([group, n]) => `${n} ${group}`)
    .join(" · ");
  return [`CI ${e.ci}`, ...(e.tests ? [`tests ${e.tests}`] : []), ...(findings ? [findings] : [])];
}

/** Whether Review anyway is offered: a failing CI, or a loaded PR still held by a gate. */
function offersReviewAnyway(row: QueueRow): boolean {
  if (row.suggested || row.fetch_error !== null) return false;
  if (row.prereview_state === "ci_failing") return true;
  return row.loaded && (row.prereview_state === "waiting_ci" || row.prereview_state === "waiting_auto_review");
}

export type RowActions = {
  onOpen: (row: QueueRow) => void;
  onDismiss: (row: QueueRow) => void;
  onReviewAnyway: (row: QueueRow) => void;
  onRetry: (row: QueueRow) => void;
  /** Actions in flight, by `<action>:<repo>#<n>`. */
  busy: ReadonlySet<string>;
};

export const actionId = (action: string, row: { repo: string; number: number }) =>
  `${action}:${row.repo}#${row.number}`;

/** Every spec §2 field of a row; the card's actions only when `actions` is given. */
export function RowCard({ row, actions }: { row: QueueRow; actions?: RowActions }) {
  const titleId = useId();
  const ref = refOf(row);
  const ci = CI[row.ci_state];
  const state = stateOf(row);
  // Your own PR reads as ready (or not) to request review (spec §3.3), as in the PR view.
  const verdict = verdictWords(row.recommendation, row.author_kind === "you");
  const coverage = coverageText(row);
  const evidence = evidenceOf(row);
  const author = row.author_kind === "developer" ? row.author : `${row.author} (${row.author_kind})`;
  const reason = row.prereview_reason.trim();
  const paths = row.differing_paths ?? [];
  const showReason = (row.prereview_state === "ci_failing" || row.prereview_state === "failed") && reason !== "";
  return (
    <article className={s.card} aria-labelledby={titleId}>
      <h3 className={s.cardTitle} id={titleId}>
        {actions ? (
          <button
            type="button"
            className={s.titleButton}
            aria-label={`Open ${ref} · ${visibleText(row.title)}`}
            onClick={() => actions.onOpen(row)}
          >
            <span className={s.ref}>{ref}</span> · <Visible text={row.title} />
          </button>
        ) : (
          <>
            <span className={s.ref}>{ref}</span> · <Visible text={row.title} />
          </>
        )}
      </h3>
      <div className={s.meta}>
        <span>{author}</span>
        <span>{`${plural(row.size_lines, "line")} · ${plural(row.files, "file")}`}</span>
        <span>{ageOf(row.created_at)}</span>
        <span className={s.pill}>{KIND[row.kind]}</span>
        <span className={row.depth === "deep" ? s.pillDeep : s.pill}>{row.depth === "deep" ? "Deep" : "Quick"}</span>
        <span className={s.ci} data-ci={row.ci_state}>
          <span aria-hidden="true">{ci.icon}</span> {ci.text}
        </span>
        {reviewsOf(row).map((r) => (
          <span key={r}>{r}</span>
        ))}
        {row.review_requested ? <span className={s.pill}>Review requested</span> : null}
      </div>
      <div className={s.right}>
        {row.suggested ? (
          <Badge tone="warn" icon="?">
            {SUGGESTED_NOTE}
          </Badge>
        ) : state.tone === "neutral" ? (
          <Badge>{`${STATE_ICON.neutral} ${state.text}`}</Badge>
        ) : (
          <Badge tone={state.tone} icon={STATE_ICON[state.tone]}>
            {state.text}
          </Badge>
        )}
        {verdict ? (
          <span className={s.verdict} data-tone={verdict.tone}>
            {verdict.text}
            {row.recommendation_stale ? <span className={s.pillWarn}>stale</span> : null}
          </span>
        ) : null}
        {evidence.length || coverage ? (
          <span className={s.evidence}>
            {evidence.map((e) => (
              <span key={e}>{e}</span>
            ))}
            {coverage ? <span className={s.coverage}>{coverage}</span> : null}
          </span>
        ) : null}
      </div>
      {showReason ? <p className={s.reason}>{reason}</p> : null}
      {row.followup_note ? (
        <p className={s.reason}>
          {row.followup_note}
          {paths.length ? (
            <>
              {": "}
              {paths.slice(0, 5).map((path, i) => (
                <span key={path}>
                  {i ? ", " : ""}
                  <code>
                    <Visible text={path} />
                  </code>
                </span>
              ))}
              {paths.length > 5 ? ` +${paths.length - 5} more` : ""}
            </>
          ) : null}
        </p>
      ) : null}
      {actions ? (
        <div className={s.cardActions}>
          {offersReviewAnyway(row) ? (
            <Button
              size="sm"
              variant="secondary"
              aria-label={`Review ${ref} anyway`}
              loading={actions.busy.has(actionId("anyway", row))}
              onClick={() => actions.onReviewAnyway(row)}
            >
              Review anyway
            </Button>
          ) : null}
          {row.prereview_state === "failed" && !row.suggested ? (
            <Button
              size="sm"
              variant="secondary"
              aria-label={`Retry ${ref}`}
              loading={actions.busy.has(actionId("retry", row))}
              onClick={() => actions.onRetry(row)}
            >
              Retry
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            icon="✕"
            aria-label={`Dismiss ${ref}`}
            title="Dismiss until loaded again"
            loading={actions.busy.has(actionId("dismiss", row))}
            onClick={() => actions.onDismiss(row)}
          />
        </div>
      ) : null}
    </article>
  );
}

/** What a waiting row waits on, for the summary strip. */
const WAITING_ON: Record<QueueRow["prereview_state"], string> = {
  fetch_failed: "couldn't load",
  waiting_ci: "waiting for CI",
  ci_failing: "CI failing",
  waiting_auto_review: "on automated review",
  debouncing: "settling after pushes",
  ready: "ready",
  queued: "queued",
  running: "running",
  failed: "failed",
  not_started: "not started",
};

/** One line on what a section holds, for the summary strip. */
function summaryOf(id: SectionId, rows: QueueRow[]): string {
  if (rows.length === 0) return "Nothing here";
  if (id === "ready") {
    const quick = rows.filter((r) => r.depth === "quick").length;
    return `${quick} quick · ${rows.length - quick} deep`;
  }
  if (id === "loaded") {
    const shown = rows.slice(0, 2).map(refOf).join(", ");
    return rows.length > 2 ? `${shown} +${rows.length - 2} more` : shown;
  }
  if (id === "approved") return "Rebased, unchanged, CI green";
  const counts = new Map<string, number>();
  for (const r of rows) {
    const what = r.suggested
      ? "from suggested repos"
      : r.fetch_error !== null
        ? WAITING_ON.fetch_failed
        : WAITING_ON[r.prereview_state];
    counts.set(what, (counts.get(what) ?? 0) + 1);
  }
  return [...counts].map(([what, n]) => `${n} ${what}`).join(" · ");
}

type RepoCount = { repo: string; display: string; count: number };

function repoCounts(rows: QueueRow[]): RepoCount[] {
  const by = new Map<string, RepoCount>();
  for (const r of rows) {
    const entry = by.get(r.repo) ?? { repo: r.repo, display: r.display_repo, count: 0 };
    entry.count += 1;
    by.set(r.repo, entry);
  }
  return [...by.values()].sort((a, b) => a.display.localeCompare(b.display));
}

export type QueueProps = RowActions & {
  queue: QueueData;
  narrow: boolean;
  onLoad: (ref: string) => Promise<void>;
  onOpenMany: (rows: QueueRow[]) => void;
  onConfirmRepo: (repo: string) => void;
};

/** The first section with rows, Ready for you when it has any. */
function firstSection(groups: Record<SectionId, QueueRow[]>): SectionId {
  return SECTIONS.find((sec) => groups[sec.id].length > 0)?.id ?? "ready";
}

export function Queue(props: QueueProps) {
  const { queue, narrow } = props;
  const [repo, setRepo] = useState<string | null>(null);
  const [chosen, setChosen] = useState<SectionId | null>(null);
  const [showApproved, setShowApproved] = useState(false);
  const repos = repoCounts(queue.rows);
  // A repo whose last PR left the queue stops filtering it.
  const current = repos.find((r) => r.repo === repo);
  const filter = current?.repo ?? null;
  const rows = filter === null ? queue.rows : queue.rows.filter((r) => r.repo === filter);
  const groups = groupRows(rows);
  const selected = chosen ?? firstSection(groups);
  const firstRun = queue.rows.length === 0 && queue.synced_at === null;

  const repoButton = (key: string | null, label: string, count: number) => (
    <button
      key={key ?? "all"}
      type="button"
      className={s.repo}
      aria-current={filter === key}
      onClick={() => setRepo(key)}
    >
      <span className={s.repoName}>{label}</span>
      <span className={s.count}>{count}</span>
    </button>
  );
  const suggested = queue.suggested_repos.length ? (
    <div role="group" aria-label="Suggested repos" className={s.suggested}>
      <h3 className={s.paneTitle}>Suggested repos</h3>
      <p className={s.muted}>Not yours: confirm one to review its PRs automatically.</p>
      {queue.suggested_repos.map((r) => (
        <div key={r} className={s.suggestedRow}>
          <span className={s.repoName}>{r}</span>
          <Button
            size="sm"
            variant="secondary"
            aria-label={`Confirm ${r}`}
            loading={props.busy.has(`confirm:${r}`)}
            onClick={() => props.onConfirmRepo(r)}
          >
            Confirm
          </Button>
        </div>
      ))}
    </div>
  ) : null;

  const repoPane = narrow ? (
    <div className={s.repoSelect}>
      <label>
        <span className={s.muted}>Repo </span>
        <select value={filter ?? ""} onChange={(e) => setRepo(e.target.value || null)}>
          <option value="">{`All watched (${queue.rows.length})`}</option>
          {repos.map((r) => (
            <option key={r.repo} value={r.repo}>{`${r.display} (${r.count})`}</option>
          ))}
        </select>
      </label>
      {suggested}
    </div>
  ) : (
    <aside className={s.repoPane}>
      <nav aria-label="Repos" className={s.repoList}>
        <h2 className={s.paneTitle}>Repos</h2>
        {repoButton(null, "All watched", queue.rows.length)}
        {repos.map((r) => repoButton(r.repo, r.display, r.count))}
      </nav>
      {suggested}
    </aside>
  );

  const open = (id: SectionId): ReactNode => {
    const list = groups[id];
    const meta = sectionInfo(id);
    const quick = list.filter((r) => r.depth === "quick");
    const bulk =
      id === "ready" || id === "loaded" ? (
        <span className={s.introActions}>
          <Button
            size="sm"
            variant="secondary"
            disabled={list.length === 0}
            onClick={() => list[0] && props.onOpen(list[0])}
          >
            Open next
          </Button>
          {id === "ready" && quick.length > 0 ? (
            <Button size="sm" variant="ghost" onClick={() => props.onOpenMany(quick)}>
              {`Open all quick (${quick.length})`}
            </Button>
          ) : null}
        </span>
      ) : null;
    const collapsed = id === "approved" && !showApproved;
    return (
      <div className={s.sectionBody}>
        <div className={s.intro} data-section={id}>
          <span>
            <b>{meta.label}</b>
            <br />
            <span>{meta.intro}</span>
          </span>
          {bulk}
        </div>
        {id === "approved" && list.length > 0 ? (
          <Button
            size="sm"
            variant="ghost"
            className={s.collapse}
            aria-expanded={!collapsed}
            onClick={() => setShowApproved(!showApproved)}
          >
            {collapsed
              ? `Show ${list.length} still approved ${list.length === 1 ? "PR" : "PRs"}`
              : `Hide ${list.length} still approved ${list.length === 1 ? "PR" : "PRs"}`}
          </Button>
        ) : null}
        {collapsed ? null : list.length === 0 ? (
          <p className={s.empty}>Nothing in {meta.label}.</p>
        ) : (
          <div className={s.cards}>
            {list.map((r) => (
              <RowCard key={`${r.repo}#${r.number}`} row={r} actions={props} />
            ))}
          </div>
        )}
        <ul aria-label="Other sections" className={s.strip}>
          {SECTIONS.filter((sec) => sec.id !== id).map((sec) => (
            <li key={sec.id}>
              <button type="button" className={s.stripItem} onClick={() => setChosen(sec.id)}>
                <b>{`${sec.label} · ${groups[sec.id].length}`}</b>
                <span>{summaryOf(sec.id, groups[sec.id])}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  };

  let body: ReactNode;
  if (queue.rows.length === 0) {
    body = (
      <p className={s.empty}>
        {queue.sync_error !== null && queue.synced_at === null
          ? "Nothing is cached yet. Once GitHub answers, your pull requests appear here."
          : firstRun
            ? "Fetching your pull requests from GitHub for the first time…"
            : "Nothing needs your review. Load any PR above, or press ⌘K."}
      </p>
    );
  } else {
    body = (
      <Tabs
        aria-label="Queue sections"
        tabs={SECTIONS.map((sec) => ({ id: sec.id, label: sec.label, count: groups[sec.id].length }))}
        selected={selected}
        onSelect={(id) => setChosen(id as SectionId)}
      >
        {(id) => open(id as SectionId)}
      </Tabs>
    );
  }

  return (
    <div className={narrow ? s.queueNarrow : s.queue}>
      {repoPane}
      <section className={s.queueMain} aria-label="Queue">
        <div className={s.toolbar}>
          <span className={s.crumb}>
            <b>{current?.display ?? "All watched repos"}</b>
            {` · ${rows.length} open`}
          </span>
          <LoadPr onLoad={props.onLoad} />
        </div>
        {body}
      </section>
    </div>
  );
}
