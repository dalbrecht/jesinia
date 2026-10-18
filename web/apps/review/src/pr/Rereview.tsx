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
  hunksOf,
  REASONS,
  short,
  visibleText,
  type Hunk,
  type HunkRef,
  type PreviousComment,
  type PreviousReview,
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

type Row = { key: string; label: string; hunk: Hunk | null; isNew: boolean; comments: PreviousComment[] };
type Group = { name: string; rows: Row[] };

/** The hunk a comment sits on now: the one its request cites, else the one holding its line. */
function placeOf(comment: PreviousComment, request: Request | undefined, hunks: Hunk[]): number | null {
  const cited = request?.hunks.find((h) => h.path === comment.path);
  if (cited && cited.hunk < hunks.length) return cited.hunk;
  if (comment.line === null) return null;
  const line = comment.line;
  const at = hunks.findIndex((h) =>
    comment.side === "LEFT" ? h.oldStart <= line && line <= h.oldEnd : h.newStart <= line && line <= h.newEnd,
  );
  return at < 0 ? null : at;
}

/**
 * Your previous comments beside this head, grouped by file and aligned by hunk: a row for every
 * hunk with a comment or new since your last look, and one for comments on no hunk of this head.
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
          rows: [{ key: "review", label: "the review", hunk: null, isNew: false, comments: general }],
        },
      ]
    : [];
  const paths = [
    ...new Set([
      ...previous.comments.flatMap((c) => (c.path === null ? [] : [c.path])),
      ...(rereview?.new_hunks ?? []).map((h) => h.path),
    ]),
  ];
  for (const path of paths) {
    const patch = fileOf(files, path)?.patch;
    const hunks = patch ? hunksOf(patch) : [];
    const fresh = new Set((rereview?.new_hunks ?? []).filter((h) => h.path === path).map((h) => h.hunk));
    const placed = new Map<number, PreviousComment[]>();
    const elsewhere: PreviousComment[] = [];
    for (const comment of previous.comments.filter((c) => c.path === path)) {
      const at = placeOf(comment, requests.get(comment.ref), hunks);
      if (at === null) elsewhere.push(comment);
      else placed.set(at, [...(placed.get(at) ?? []), comment]);
    }
    const indices = [...new Set([...placed.keys(), ...[...fresh].filter((i) => i < hunks.length)])].sort(
      (x, y) => x - y,
    );
    const rows: Row[] = indices.map((i) => ({
      key: `hunk-${i}`,
      label: `hunk ${i + 1}`,
      hunk: hunks[i] ?? null,
      isNew: fresh.has(i),
      comments: placed.get(i) ?? [],
    }));
    if (elsewhere.length)
      rows.push({ key: "elsewhere", label: "elsewhere in the file", hunk: null, isNew: false, comments: elsewhere });
    groups.push({ name: path, rows });
  }
  return groups;
}

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
  return (
    <section className={s.group} role="region" aria-label="Previous review, aligned by file and hunk">
      <h3>Previous review, aligned by file and hunk</h3>
      {groups.length === 0 ? <p className={s.muted}>Your previous review left no comments.</p> : null}
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
                    {row.comments.length === 0 ? <span className={s.muted}>No comment here.</span> : null}
                    {row.comments.map((c) => (
                      <div key={c.ref}>
                        <Where comment={c} />
                        <CommentText comment={c} />
                      </div>
                    ))}
                  </td>
                  <td>
                    {row.hunk ? (
                      <pre className={s.hunkHead}>
                        <Visible text={row.hunk.header} />
                      </pre>
                    ) : null}
                    {row.isNew ? <Badge>New since your last look</Badge> : null}
                    {row.comments.map((c) => (
                      <StatusBadge key={c.ref} request={requests.get(c.ref)} />
                    ))}
                    {!row.hunk && !row.isNew && row.comments.length === 0 ? (
                      <span className={s.muted}>Nothing changed here.</span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
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
