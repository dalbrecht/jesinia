// SPDX-License-Identifier: AGPL-3.0-or-later
// The posting confirmation (spec §7, design pass `followups.html`): exactly what will be posted
// (the review body and every inline comment, as text), and every warning the server raised,
// each confirmed with its own box before the action unlocks. Posting is keyed by the preview's
// digest: a retry of the same post reuses its key, and any edit (a new digest) gets a new one.
import type { JesiniaClient } from "@jesinia/client";
import { Button } from "@jesinia/design";
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  ActionKeys,
  errorCause,
  postReview,
  previewPost,
  ReviewRequestError,
  safeHttpUrl,
  type PostPreview,
  type PostWarning,
  type ReviewEvent,
} from "../api";
import { lines, short, visibleText } from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";

const TITLE: Record<ReviewEvent, string> = {
  APPROVE: "Approve",
  REQUEST_CHANGES: "Request changes on",
  COMMENT: "Comment on",
};
const ACTION: Record<ReviewEvent, string> = {
  APPROVE: "Approve",
  REQUEST_CHANGES: "Request changes",
  COMMENT: "Comment",
};
const BLOCKING = /blocking/i;
const AGAINST_NOTE = "Approving goes against the evidence";

function sentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

export type PostDialogProps = {
  client: JesiniaClient;
  repo: string;
  number: number;
  /** "Acme/web#3", for the title. */
  prRef: string;
  initial: ReviewEvent;
  own: boolean;
  /** The verdict in words, and its reasons, for approving against the evidence. */
  verdict: string | null;
  reasons: string[];
  /** The PR's head now (the round's head is the preview's). */
  prHead: string;
  /** The round the author was shown: previews and posts name it (Codex P1). */
  roundId: number | null;
  keys: ActionKeys;
  onClose: () => void;
  onPosted: (url: string) => void;
  onEditDrafts: () => void;
  onReviewNewCommits: () => void;
};

type Failure = { message: ReactNode; final: boolean };

export function PostDialog(props: PostDialogProps) {
  const { client, repo, number, keys } = props;
  const heading = useId();
  const [event, setEvent] = useState<ReviewEvent>(props.own ? "COMMENT" : props.initial);
  const [preview, setPreview] = useState<PostPreview | null>(null);
  const [previewError, setPreviewError] = useState<{ text: string; tooLong: boolean } | null>(null);
  const [confirmed, setConfirmed] = useState<ReadonlySet<PostWarning>>(new Set());
  const [posting, setPosting] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [waiting, setWaiting] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let current = true;
    setPreview(null);
    setPreviewError(null);
    setConfirmed(new Set());
    previewPost(client, repo, number, event, props.roundId).then(
      (p) => {
        if (current) setPreview(p);
      },
      (e: unknown) => {
        if (!current) return;
        const tooLong = e instanceof ReviewRequestError && e.code === "ReviewBodyTooLong";
        setPreviewError({ text: errorCause(e), tooLong });
      },
    );
    return () => {
      current = false;
    };
  }, [client, repo, number, event, reads, props.roundId]);

  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);
  useEffect(() => dialog.current?.focus(), []);

  // Focus stays inside while the dialog is open (I5): Tab and Shift+Tab wrap around.
  const trap = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      props.onClose();
      return;
    }
    if (e.key !== "Tab" || !dialog.current) return;
    const focusable = [
      ...dialog.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
      ),
    ];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) {
      e.preventDefault();
      return;
    }
    const at = document.activeElement;
    if (e.shiftKey && (at === first || at === dialog.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && at === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const switchTo = (next: ReviewEvent) => {
    setFailure(null);
    setEvent(next);
    setReads((n) => n + 1);
  };

  const post = useCallback(
    async (p: PostPreview) => {
      const action = `post:${p.event}:${p.digest}`;
      setPosting(true);
      setFailure(null);
      setWaiting(null);
      try {
        const url = await postReview(client, repo, number, keys.keyFor(action), {
          event: p.event,
          confirm: [...p.warnings],
          digest: p.digest,
          round_id: props.roundId,
        });
        keys.settle(action);
        props.onPosted(url);
      } catch (e) {
        keys.settle(action, e);
        const code = e instanceof ReviewRequestError ? e.code : null;
        const facts = e instanceof ReviewRequestError ? e.facts : null;
        if (facts?.retryAfter != null) {
          // Nothing was sent yet, or another attempt is in flight: the same post, same key, later.
          const secs = facts.retryAfter;
          setWaiting(
            `${sentence(e instanceof Error ? e.message : "")} Trying again in ${secs} s with the same request…`,
          );
          timer.current = setTimeout(() => void post(p), secs * 1000);
          return;
        }
        if (e instanceof ReviewRequestError && e.roundChanged) {
          setFailure({
            final: true,
            message: "A new review round started: nothing was posted. Close this and review the new round.",
          });
        } else if (code === "EarlierReviewPosted") {
          const href = safeHttpUrl(facts?.reviewUrl ?? "");
          setFailure({
            final: true,
            message: (
              <>
                An earlier version of this review is already on GitHub:{" "}
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    the earlier review
                  </a>
                ) : (
                  "the earlier review"
                )}
                ; your newer edits were not sent.
              </>
            ),
          });
        } else if (code === "PreviewOutdated" || code === "NeedsConfirmation") {
          setFailure({ final: false, message: "What would be posted changed since this preview: check it again." });
          setReads((n) => n + 1);
        } else if (code === "CommentOnlyOnOwnPr") {
          switchTo("COMMENT");
          setFailure({ final: false, message: "On your own PR only a comment can be posted: check it again." });
        } else {
          setFailure({ final: false, message: `Couldn't post: ${errorCause(e)} Your drafts are kept.` });
        }
      } finally {
        setPosting(false);
      }
    },
    [client, repo, number, keys, props],
  );

  const warnings = preview?.warnings ?? [];
  const blocked = warnings.includes("own_pr_comment_only");
  const ready =
    preview !== null &&
    !blocked &&
    warnings.every((w) => confirmed.has(w)) &&
    !posting &&
    waiting === null &&
    !(failure?.final ?? false);
  const tick = (w: PostWarning, on: boolean) =>
    setConfirmed((prev) => {
      const next = new Set(prev);
      if (on) next.add(w);
      else next.delete(w);
      return next;
    });
  const anyway = `${ACTION[event]} anyway`;
  const notes = (preview?.notes ?? []).filter((n) => !n.startsWith(AGAINST_NOTE));
  const against = preview?.notes.find((n) => n.startsWith(AGAINST_NOTE));

  const blockingAt = preview
    ? [
        ...(BLOCKING.test(preview.body) ? ["the review body"] : []),
        ...preview.comments
          .filter((c) => BLOCKING.test(c.body))
          .map((c) => visibleText(`${c.path}:${lines({ start_line: c.start_line ?? c.line, end_line: c.line })}`)),
      ]
    : [];

  const confirmBox = (w: PostWarning) => (
    <label className={s.row}>
      <input type="checkbox" checked={confirmed.has(w)} onChange={(e) => tick(w, e.target.checked)} />
      {anyway}
    </label>
  );

  const box = (w: PostWarning): ReactNode => {
    const id = `${heading}-${w}`;
    switch (w) {
      case "blocking_comment":
        return (
          <div key={w} role="group" aria-labelledby={id} className={s.warning}>
            <b id={id}>You're approving, but the review says BLOCKING</b>
            <span>
              {`Jesinia found “blocking” in ${blockingAt.join(", ") || "the review"}. It matches in any case, anywhere in the text, even inside a longer word such as “nonblocking”, so check each one is meant. Approving while a comment is blocking usually means the action is wrong.`}
            </span>
            {confirmBox(w)}
            <span>
              <Button size="sm" variant="danger" onClick={() => switchTo("REQUEST_CHANGES")}>
                Request changes instead
              </Button>
            </span>
          </div>
        );
      case "approve_against_evidence":
        return (
          <div key={w} role="group" aria-labelledby={id} className={s.warning}>
            <b id={id}>Jesinia's verdict isn't Approve</b>
            <span>{`Jesinia's verdict: ${props.verdict ?? "none yet (the review hasn't finished)"}.`}</span>
            {props.reasons.length ? (
              <ul className={s.reasons}>
                {props.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            ) : null}
            {against ? <span>{sentence(against)}</span> : null}
            {confirmBox(w)}
            <span>
              <Button size="sm" variant="danger" onClick={() => switchTo("REQUEST_CHANGES")}>
                Request changes instead
              </Button>
            </span>
          </div>
        );
      case "newer_head":
        return (
          <div key={w} role="group" aria-labelledby={id} className={s.warning}>
            <b id={id}>There are newer commits</b>
            <span>
              {`This review covers ${short(preview?.head_sha ?? "")}; the PR head is now ${short(preview?.pr_head_sha || props.prHead)}. The review will be posted against ${short(preview?.head_sha ?? "")}.`}
            </span>
            {confirmBox(w)}
            <span>
              <Button size="sm" variant="secondary" onClick={props.onReviewNewCommits}>
                Review the new commits first
              </Button>
            </span>
          </div>
        );
      case "own_pr_comment_only":
        return (
          <div key={w} role="group" aria-labelledby={id} className={s.warning}>
            <b id={id}>This is your own PR</b>
            <span>
              GitHub only lets you comment on your own PR: it won't take an approval or a request for changes.
            </span>
            <span>
              <Button size="sm" variant="secondary" onClick={() => switchTo("COMMENT")}>
                Comment instead
              </Button>
            </span>
          </div>
        );
    }
  };

  return (
    <div className={s.scrim}>
      <div
        ref={dialog}
        className={s.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={heading}
        tabIndex={-1}
        onKeyDown={trap}
      >
        <h2 id={heading} className={s.dialogHead}>{`${TITLE[event]} ${props.prRef} on GitHub`}</h2>
        <div className={s.dialogBody}>
          {props.own ? (
            <p className={s.muted}>
              This is your own PR: GitHub doesn't let you approve or request changes on your own PR, so it is posted as
              a comment.
            </p>
          ) : null}
          {previewError ? (
            <div className={s.problem} role="alert">
              <span>
                {previewError.tooLong
                  ? `The review body is too long: ${previewError.text}`
                  : `Couldn't read what would be posted: ${previewError.text}`}
              </span>
              {!previewError.tooLong ? (
                <Button size="sm" variant="secondary" onClick={() => setReads((n) => n + 1)}>
                  Try again
                </Button>
              ) : null}
              <Button size="sm" variant="secondary" onClick={props.onEditDrafts}>
                Edit drafts
              </Button>
            </div>
          ) : null}
          {!preview && !previewError ? <p className={s.muted}>Reading exactly what would be posted…</p> : null}
          {warnings.map(box)}
          {preview ? (
            <>
              <b>What will be posted</b>
              <div className={s.comment}>
                <span className={s.label}>Review body</span>
                {preview.body ? (
                  <pre className={s.exact}>
                    <Visible text={preview.body} multiline />
                  </pre>
                ) : (
                  <span className={s.muted}>(none)</span>
                )}
              </div>
              {preview.comments.map((c, i) => (
                <div key={i} className={s.comment}>
                  <span className={s.loc}>
                    <Visible text={`${c.path}:${lines({ start_line: c.start_line ?? c.line, end_line: c.line })}`} />
                  </span>
                  <pre className={s.exact}>
                    <Visible text={c.body} multiline />
                  </pre>
                </div>
              ))}
              {notes.length ? (
                <ul className={s.refs} aria-label="Notes">
                  {notes.map((n, i) => (
                    <li key={i} className={s.muted}>
                      {sentence(n)}
                    </li>
                  ))}
                </ul>
              ) : null}
              <span className={s.row}>
                <Button size="sm" variant="ghost" onClick={props.onEditDrafts}>
                  Edit drafts
                </Button>
              </span>
            </>
          ) : null}
          {waiting ? (
            <p role="status" className={s.muted}>
              {waiting}
            </p>
          ) : null}
          {failure ? (
            <p role="alert" className={s.problem}>
              {failure.message}
            </p>
          ) : null}
        </div>
        <div className={s.dialogFoot}>
          <span>Posted as one review. Nothing is sent until you choose {ACTION[event]}.</span>
          <Button size="sm" variant="ghost" onClick={props.onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            variant={event === "REQUEST_CHANGES" ? "danger" : "primary"}
            disabled={!ready}
            loading={posting}
            onClick={() => preview && void post(preview)}
          >
            {ACTION[event]}
          </Button>
        </div>
      </div>
    </div>
  );
}
