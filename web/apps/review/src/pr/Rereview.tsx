// SPDX-License-Identifier: AGPL-3.0-or-later
// An incremental re-review (spec §4.2): **Your requests** (each of your previous comments with what
// the new commits did about it), the previous review beside the new one aligned by file and hunk,
// and **New since your last look**. Comments, paths and hunks are PR or GitHub text, and the
// explanations are agent output: paths and comments render as text with hidden characters made
// visible (R62), explanations through the safe Markdown renderer only.
import { Badge } from "@jesinia/design";
import { SafeMarkdown } from "@jesinia/shell";
import type { ReactNode } from "react";
import type { PrFile } from "../api";
import {
  fileOf,
  GROUP_LABEL,
  hunksOf,
  REASONS,
  SEVERITY,
  short,
  visibleText,
  type Hunk,
  type HunkRef,
  type PreviousComment,
  type PreviousFinding,
  type PreviousReview,
  type PreviousStep,
  type Request,
  type Rereview,
  type WalkCoverage,
} from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";
import { FileDiff, type DiffLayout } from "./Walkthrough";

const STATUS = {
  addressed: { tone: "success", icon: "✓", text: "Addressed" },
  partly: { tone: "warn", icon: "◐", text: "Partly addressed" },
  not_addressed: { tone: "danger", icon: "!", text: "Not addressed" },
} as const;

/** A hunk as the UI names it everywhere: its path and its 1-based place in the file. */
const hunkLabel = (ref: HunkRef) => `${ref.path} · hunk ${ref.hunk + 1}`;

function StatusBadge({ request }: { request: Request | undefined }) {
  if (!request) return <Badge>No status given</Badge>;
  const status = STATUS[request.status];
  return (
    <Badge tone={status.tone} icon={status.icon}>
      {status.text}
    </Badge>
  );
}

function Where({ comment }: { comment: PreviousComment }) {
  if (comment.path === null) return <span className={s.muted}>On the review</span>;
  const lines = comment.line === null ? "" : `:${comment.start_line ? `${comment.start_line}–` : ""}${comment.line}`;
  const old = comment.side === "LEFT" ? " (old)" : "";
  return (
    <span className={s.loc}>
      <Visible text={`${comment.path}${lines}${old}`} />
    </span>
  );
}

function CommentText({ comment }: { comment: PreviousComment }) {
  return (
    <p className={s.previousComment}>
      <Visible text={comment.body} />
      {comment.resolved ? <span className={s.muted}> (resolved on GitHub)</span> : null}
    </p>
  );
}

const byRef = (rereview: Rereview | null) => new Map((rereview?.requests ?? []).map((r) => [r.comment_ref, r]));

function sourceLine(previous: PreviousReview): string {
  const where = previous.source === "round" ? "as Jesinia posted it" : "on GitHub";
  return `From your review of ${short(previous.head_sha)}, ${where}.`;
}

// --- the previous review, aligned -------------------------------------------------------------

type Row = {
  key: string;
  label: string;
  hunk: Hunk | null;
  isNew: boolean;
  comments: PreviousComment[];
  steps: PreviousStep[];
  findings: PreviousFinding[];
};
type Group = { name: string; rows: Row[] };

const CHANGED = "changed since your last review";
const UNPLACED = "couldn't place on this head";
type Place = number | "changed" | "unplaced" | null;

/** Which hunk of this head holds new-file lines `start`–`end` (old-file lines on the left). */
function hunkAt(hunks: Hunk[], start: number, end: number, left = false): number | null {
  const at = hunks.findIndex((h) =>
    left ? h.oldStart <= end && start <= h.oldEnd : h.newStart <= end && start <= h.newEnd,
  );
  return at < 0 ? null : at;
}

/**
 * Where a comment sits on this head (I2): outdated when its text changed; else the hunk its request
 * cites on its current path, else the hunk holding the line its text is on now, else elsewhere in
 * the file. Never by the line it had at the previous head.
 */
function placeOf(comment: PreviousComment, request: Request | undefined, hunks: Hunk[]): Place {
  if (comment.unplaced) return "unplaced";
  if (comment.outdated) return "changed";
  const path = comment.now_path ?? comment.path;
  const cited = request?.hunks.find((h) => h.path === path);
  if (cited && cited.hunk < hunks.length) return cited.hunk;
  if (comment.now_line === null) return null;
  return hunkAt(hunks, comment.now_start_line ?? comment.now_line, comment.now_line, comment.side === "LEFT");
}

function findingPlace(finding: PreviousFinding, hunks: Hunk[]): Place {
  if (finding.unplaced) return "unplaced";
  if (finding.outdated || finding.now_start_line === null || finding.now_end_line === null) return "changed";
  return hunkAt(hunks, finding.now_start_line, finding.now_end_line);
}

/** Steps whose every hunk changed since: they have no place on this head. */
export const REWRITTEN_STEPS = "Steps on code that changed since";
/** Steps whose hunks couldn't be matched to this head's (why is said with each). */
export const UNPLACED_STEPS = "Steps Jesinia couldn't place";

/**
 * The previous round beside this head, grouped by file (this head's paths, after renames) and
 * aligned by hunk: a row for every hunk with a comment, a previous step or finding, or new since
 * your last look; one for what sits on no hunk of this head; and one for what changed since.
 */
export function alignPrevious(
  previous: PreviousReview,
  rereview: Rereview | null,
  files: PrFile[] | null | undefined,
): Group[] {
  const requests = byRef(rereview);
  const general = previous.comments.filter((c) => c.path === null);
  const groups: Group[] = general.length
    ? [
        {
          name: "The review",
          rows: [
            {
              key: "review",
              label: "the review",
              hunk: null,
              isNew: false,
              comments: general,
              steps: [],
              findings: [],
            },
          ],
        },
      ]
    : [];
  const pathOf = (c: { path: string | null; now_path: string | null }) => c.now_path ?? c.path;
  const placedSteps = previous.steps.filter((st) => st.now_hunks.length > 0 && !st.unplaced);
  const paths = [
    ...new Set([
      ...previous.comments.flatMap((c) => (c.path === null ? [] : [pathOf(c) as string])),
      ...previous.findings.map((f) => pathOf(f) as string),
      ...placedSteps.flatMap((st) => st.now_hunks.map((h) => h.path)),
      ...(rereview?.new_hunks ?? []).map((h) => h.path),
    ]),
  ];
  for (const path of paths) {
    const patch = fileOf(files, path)?.patch;
    const hunks = patch ? hunksOf(patch) : [];
    const fresh = new Set((rereview?.new_hunks ?? []).filter((h) => h.path === path).map((h) => h.hunk));
    const rows = new Map<Place, Row>();
    const row = (at: Place): Row => {
      let found = rows.get(at);
      if (!found) {
        const [key, label] =
          at === "changed"
            ? ["changed", CHANGED]
            : at === "unplaced"
              ? ["unplaced", UNPLACED]
              : at === null
                ? ["elsewhere", "elsewhere in the file"]
                : [`hunk-${at}`, `hunk ${at + 1}`];
        found = {
          key,
          label,
          hunk: typeof at === "number" ? (hunks[at] ?? null) : null,
          isNew: typeof at === "number" && fresh.has(at),
          comments: [],
          steps: [],
          findings: [],
        };
        rows.set(at, found);
      }
      return found;
    };
    for (const comment of previous.comments.filter((c) => c.path !== null && pathOf(c) === path))
      row(placeOf(comment, requests.get(comment.ref), hunks)).comments.push(comment);
    for (const finding of previous.findings.filter((f) => pathOf(f) === path))
      row(findingPlace(finding, hunks)).findings.push(finding);
    for (const step of placedSteps)
      for (const at of new Set(
        step.now_hunks.filter((h) => h.path === path && h.hunk < hunks.length).map((h) => h.hunk),
      ))
        row(at).steps.push(step);
    for (const at of fresh) if (at < hunks.length) row(at);
    const order = (at: Place) =>
      typeof at === "number" ? at : at === null ? 1e9 : at === "changed" ? 1e9 + 1 : 1e9 + 2;
    groups.push({ name: path, rows: [...rows.entries()].sort(([x], [y]) => order(x) - order(y)).map(([, r]) => r) });
  }
  return groups;
}

/** The distinct reasons a row's comments and findings couldn't be placed (N2). */
const unplacedCauses = (row: Row): string[] => [
  ...new Set([...row.comments, ...row.findings].flatMap((x) => (x.unplaced ? [x.unplaced] : []))),
];

function Aligned({
  previous,
  rereview,
  files,
}: {
  previous: PreviousReview;
  rereview: Rereview | null;
  files: PrFile[] | null | undefined;
}) {
  const requests = byRef(rereview);
  const groups = alignPrevious(previous, rereview, files);
  const rewritten = previous.steps.filter((st) => st.now_hunks.length === 0 && !st.unplaced);
  const unmatched = previous.steps.filter((st) => st.unplaced);
  const empty = groups.length === 0 && rewritten.length === 0 && unmatched.length === 0;
  return (
    <section className={s.group} role="region" aria-label="Previous review, aligned by file and hunk">
      <h3>Previous review, aligned by file and hunk</h3>
      {empty ? <p className={s.muted}>Your previous review left no comments.</p> : null}
      {previous.placement_error ? (
        <p className={s.notice} role="status">
          {`Some of your last review couldn't be placed on this head: ${previous.placement_error}`}
        </p>
      ) : null}
      {groups.map((group) => (
        <div key={group.name} role="group" aria-label={visibleText(group.name)} className={s.alignedFile}>
          <p className={s.loc}>{group.name === "The review" ? "The review" : <Visible text={group.name} />}</p>
          <table className={s.aligned}>
            <thead>
              <tr>
                <th scope="col">Previous review</th>
                <th scope="col">Now</th>
              </tr>
            </thead>
            <tbody>
              {group.rows.map((row) => (
                <tr key={row.key} aria-label={row.label}>
                  <td>
                    {row.comments.length + row.steps.length + row.findings.length === 0 ? (
                      <span className={s.muted}>Nothing from your last review here.</span>
                    ) : null}
                    {row.comments.map((c) => (
                      <div key={c.ref}>
                        <Where comment={c} />
                        <CommentText comment={c} />
                      </div>
                    ))}
                    {row.steps.map((st, i) => (
                      <div key={`step-${i}`}>
                        <span className={s.muted}>Walkthrough step </span>
                        <strong>
                          <Visible text={st.title} />
                        </strong>
                      </div>
                    ))}
                    {row.findings.map((f, i) => (
                      <div key={`finding-${i}`} className={s.prose}>
                        <Badge
                          tone={f.severity === "minor" ? "neutral" : "danger"}
                          icon={f.severity === "minor" ? "·" : "!"}
                        >
                          {`${SEVERITY[f.severity] ?? f.severity} ${GROUP_LABEL[f.group] ?? f.group}`}
                        </Badge>
                        <SafeMarkdown markdown={f.explanation} />
                      </div>
                    ))}
                  </td>
                  <td>
                    {row.hunk ? (
                      <pre className={s.hunkHead}>
                        <Visible text={row.hunk.header} />
                      </pre>
                    ) : null}
                    {row.key === "changed" ? (
                      <Badge tone="warn" icon="!">
                        Outdated: its lines changed
                      </Badge>
                    ) : null}
                    {unplacedCauses(row).map((why) => (
                      <p key={why} className={s.muted}>
                        {`Couldn't place this on this head: ${why}`}
                      </p>
                    ))}
                    {row.isNew ? <Badge>New since your last look</Badge> : null}
                    {row.comments.map((c) => (
                      <StatusBadge key={c.ref} request={requests.get(c.ref)} />
                    ))}
                    {!row.hunk && !row.isNew && row.key === "elsewhere" && row.comments.length === 0 ? (
                      <span className={s.muted}>Not on a changed hunk of this head.</span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {unmatched.length ? (
        <div role="group" aria-label={UNPLACED_STEPS} className={s.alignedFile}>
          <p className={s.loc}>{UNPLACED_STEPS}</p>
          <ul className={s.refs}>
            {unmatched.map((st, i) => (
              <li key={i}>
                <strong>
                  <Visible text={st.title} />
                </strong>
                <span className={s.muted}>{st.unplaced}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {rewritten.length ? (
        <div role="group" aria-label={REWRITTEN_STEPS} className={s.alignedFile}>
          <p className={s.loc}>{REWRITTEN_STEPS}</p>
          <ul className={s.refs}>
            {rewritten.map((st, i) => (
              <li key={i}>
                <strong>
                  <Visible text={st.title} />
                </strong>
                <span className={s.muted}>{`${st.changed} of its hunks changed`}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

// --- the pages ----------------------------------------------------------------------------------

type RequestsProps = {
  previous: PreviousReview | null;
  rereview: Rereview | null;
  files: PrFile[] | null | undefined;
  /** What to say while the re-review job has no result (running, failed or not run). */
  status: ReactNode;
};

/** **Your requests**: each previous comment, its status, explanation and new hunks. */
export function YourRequests({ previous, rereview, files, status }: RequestsProps) {
  const requests = byRef(rereview);
  return (
    <article className={s.page} aria-label="Your requests">
      <h2>Your requests</h2>
      {previous ? <p className={s.muted}>{sourceLine(previous)}</p> : null}
      {rereview ? null : status}
      {rereview && previous ? (
        <ol className={s.requests} aria-label="Your requests">
          {previous.comments.map((comment) => {
            const request = requests.get(comment.ref);
            return (
              <li key={comment.ref} className={s.finding}>
                <div className={s.findingHead}>
                  <StatusBadge request={request} />
                  <Where comment={comment} />
                </div>
                <CommentText comment={comment} />
                {request ? (
                  <div className={s.prose}>
                    <SafeMarkdown markdown={request.explanation} />
                  </div>
                ) : null}
                {request?.hunks.length ? (
                  <p className={s.row}>
                    <span className={s.muted}>In</span>
                    {request.hunks.map((h) => (
                      <span key={`${h.path}#${h.hunk}`} className={s.loc}>
                        <Visible text={hunkLabel(h)} />
                      </span>
                    ))}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
      {previous ? <Aligned previous={previous} rereview={rereview} files={files} /> : null}
    </article>
  );
}

/** "Covers 100% of the new changes · 1 of 1 files" (Jesinia's count, never the agent's). */
export function newChangesLine(cov: WalkCoverage | null): string | null {
  if (!cov || cov.pct === null) return null;
  if (cov.nothing_to_walk) return "Nothing new to walk";
  return `Covers ${Math.round(cov.pct)}% of the new changes · ${cov.files_covered} of ${cov.files_total} files`;
}

type NewProps = {
  rereview: Rereview | null;
  files: PrFile[] | null | undefined;
  layout: DiffLayout;
  status: ReactNode;
};

/** **New since your last look**: the changes you didn't ask for, walked through as usual. */
export function NewSinceLastLook({ rereview, files, layout, status }: NewProps) {
  const line = newChangesLine(rereview?.coverage ?? null);
  const skipped = rereview?.coverage?.not_walked ?? [];
  return (
    <article className={s.page} aria-label="New since your last look">
      <h2>New since your last look</h2>
      {rereview ? null : status}
      {line ? <p>{line}</p> : null}
      {rereview && rereview.steps.length === 0 ? <p className={s.muted}>No new change was walked through.</p> : null}
      {rereview?.steps.map((step, i) => (
        <section key={i} className={s.group}>
          <h3>
            <Visible text={step.title} />
          </h3>
          <div className={s.prose}>
            <SafeMarkdown markdown={step.body} />
          </div>
          {[...new Set(step.hunks.map((h) => h.path))].map((path) => {
            const file = fileOf(files, path);
            const only = step.hunks.filter((h) => h.path === path).map((h) => h.hunk);
            return file ? (
              <div key={path} className={s.diff}>
                <FileDiff file={file} only={only} current={only} layout={layout} />
              </div>
            ) : (
              <p key={path} className={s.loc}>
                <Visible text={path} />
              </p>
            );
          })}
        </section>
      ))}
      {skipped.length ? (
        <section className={s.group}>
          <h3>Not walked through</h3>
          <ul className={s.refs}>
            {skipped.map((skip, i) => (
              <li key={i}>
                <span className={s.loc}>
                  <Visible text={skip.hunk === null ? skip.path : hunkLabel({ path: skip.path, hunk: skip.hunk })} />
                </span>
                <span className={s.muted}>{REASONS[skip.reason] ?? skip.reason}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </article>
  );
}
