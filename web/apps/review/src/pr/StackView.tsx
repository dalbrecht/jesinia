// SPDX-License-Identifier: AGPL-3.0-or-later
// A stack reviewed as one unit (spec §4.3): one Overview and Background for the whole stack, the
// stack walkthrough across its PRs, a tab per PR (its own walkthrough, findings and verdict, with
// the cross-PR findings that concern it called out), every cross-PR finding, and Post all: one
// confirmation listing each PR and its action, then each PR posted bottom-up, stopping at the
// first that can't be. Every PR-derived string is shown with its hidden characters made visible
// (R62); agent text only through the safe Markdown renderer.
import type { JesiniaClient } from "@jesinia/client";
import { Button, Tabs } from "@jesinia/design";
import { SafeMarkdown, useClient } from "@jesinia/shell";
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  ActionKeys,
  errorCause,
  getStack,
  openStack,
  postAll,
  previewPost,
  ReviewRequestError,
  safeHttpUrl,
  type CrossFinding,
  type PostAllItem,
  type PostOutcome,
  type PostPreview,
  type PostWarning,
  type PrView,
  type ReviewEvent,
  type StackOpenResult,
  type StackView as StackData,
} from "../api";
import { Background } from "./Background";
import { Findings } from "./Findings";
import { readArtifacts, visibleText, type BackgroundSection, type Overview as OverviewData } from "./model";
import { Overview } from "./Overview";
import s from "./pr.module.css";
import { verdictWords } from "./Recommendation";
import { Visible } from "./Visible";

export type StackViewProps = {
  repo: string;
  /** Any PR of the stack. */
  number: number;
  /** Changes whenever the queue was read again (a review's progress): the stack is read again. */
  refresh: number;
  active?: boolean;
  /** Back to the single PR's review. */
  onClose: () => void;
};

type StackHunk = { pr: number; path: string; hunk: number };
type StackStep = { title: string; body: string; hunks: StackHunk[] };
type StackWalk = {
  prs: { number: number; summary: string }[];
  steps: StackStep[];
  skipped: { pr: number; path: string; hunk: number | null; reason: string }[];
};

const ACTION: Record<ReviewEvent, string> = {
  APPROVE: "Approve",
  REQUEST_CHANGES: "Request changes",
  COMMENT: "Comment",
};
const STATE: Record<PostOutcome["state"], string> = {
  posted: "posted",
  already_posted: "already posted (nothing sent)",
  failed: "failed",
  not_attempted: "not attempted",
};
const STACK_JOBS = {
  overview: "The stack's overview",
  background: "The stack's background",
  stack_walkthrough: "The stack walkthrough",
} as const;
type StackJob = keyof typeof STACK_JOBS;

type Json = Record<string, unknown>;
const record = (x: unknown): Json | null =>
  x !== null && typeof x === "object" && !Array.isArray(x) ? (x as Json) : null;
const array = <T,>(x: unknown): T[] => (Array.isArray(x) ? (x as T[]) : []);

function walkOf(x: unknown): StackWalk | null {
  const w = record(x);
  if (!w) return null;
  return { prs: array(w.prs), steps: array(w.steps), skipped: array(w.skipped) };
}

/** The action Post all proposes for a PR: its verdict's, and only a comment on your own PR. */
export function proposedEvent(view: PrView): ReviewEvent {
  if (view.pr.author_kind === "you") return "COMMENT";
  const verdict = view.round?.verdict ?? view.pr.recommendation;
  if (verdict === "approve") return "APPROVE";
  if (verdict === "request_changes") return "REQUEST_CHANGES";
  return "COMMENT";
}

// The most a PR's tab label shows of its title (the rest on hover).
const TAB_CHARS = 40;
const clip = (text: string, most: number) => {
  const chars = [...text];
  return chars.length > most ? `${chars.slice(0, most - 1).join("")}…` : text;
};

const titleOf = (view: PrView) => `#${view.pr.number} ${view.pr.title}`;
const crossLine = (f: CrossFinding) => `${f.severity} ${f.group} · ${f.note}`;

function CrossList({ findings }: { findings: CrossFinding[] }) {
  if (findings.length === 0) return <p className={s.muted}>No cross-PR findings.</p>;
  return (
    <ul className={s.refs}>
      {findings.map((f, i) => (
        <li key={i} className={s.finding} data-severity={f.severity}>
          <span className={s.loc}>
            <Visible text={crossLine(f)} />
          </span>
          {f.path ? (
            <span className={s.loc}>
              <Visible text={`${f.path}${f.start_line ? `:${f.start_line}` : ""}`} />
            </span>
          ) : null}
          <div className={s.prose}>
            <SafeMarkdown markdown={f.explanation} />
          </div>
        </li>
      ))}
    </ul>
  );
}

function StepList({ steps, label }: { steps: StackStep[] | { title: string; body: string }[]; label: string }) {
  if (steps.length === 0) return <p className={s.muted}>{`${label}: no steps yet.`}</p>;
  return (
    <ol className={s.refs} aria-label={label}>
      {steps.map((step, i) => (
        <li key={i} className={s.group}>
          <h3>
            {`${i + 1}. `}
            <Visible text={step.title} />
          </h3>
          <div className={s.prose}>
            <SafeMarkdown markdown={step.body} />
          </div>
          {"hunks" in step && step.hunks.length ? (
            <ul className={s.refs} aria-label="Hunks">
              {step.hunks.map((h, j) => (
                <li key={j} className={s.loc}>
                  <Visible text={`#${h.pr} ${h.path} · hunk ${h.hunk + 1}`} />
                </li>
              ))}
            </ul>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

type PostAllProps = {
  client: JesiniaClient;
  repo: string;
  number: number;
  members: PrView[];
  keys: ActionKeys;
  signature: string;
  onClose: () => void;
  onPosted: () => void;
};

const EVENTS: ReviewEvent[] = ["APPROVE", "REQUEST_CHANGES", "COMMENT"];
// GitHub takes only a comment on your own PR (spec §7).
const OWN_EVENTS: ReviewEvent[] = ["COMMENT"];

/** What one PR's preview says it posts, or why it couldn't be read. */
function Answered({ answer }: { answer: { preview: PostPreview | null; error: string | null } | undefined }) {
  if (answer?.preview) {
    const n = answer.preview.comments.length;
    const body = answer.preview.body ? " and a review body" : "";
    return <span className={s.muted}>{`${n} inline ${n === 1 ? "comment" : "comments"}${body}`}</span>;
  }
  if (answer?.error)
    return (
      <span
        className={s.problem}
        role="alert"
      >{`Couldn't read what would be posted: ${visibleText(answer.error)}`}</span>
    );
  return <span className={s.muted}>Reading what would be posted…</span>;
}

/** A PR Post all posts: its view (with its round) and the action chosen for it. */
type Choice = { view: PrView; round: NonNullable<PrView["round"]>; event: ReviewEvent };
type Answer = { preview: PostPreview | null; error: string | null };
type LeftOut = { number: number; reason: string };

/** What Post all can post of the stack as it is now, and every PR it leaves out, with why. */
function plan(members: PrView[]): { choices: Choice[]; leftOut: LeftOut[] } {
  const choices: Choice[] = [];
  const leftOut: LeftOut[] = [];
  for (const view of members) {
    const round = view.round;
    const number = view.pr.number;
    if (!round) leftOut.push({ number, reason: "no review yet" });
    else if (round.posted_review_url) leftOut.push({ number, reason: "posted already" });
    else if (round.frozen) leftOut.push({ number, reason: "moved on since this review; review its new head first" });
    else choices.push({ view, round, event: proposedEvent(view) });
  }
  return { choices, leftOut };
}

// Each warning in Jesinia's words (as the single-PR dialog says it); each is ticked by the author.
const WARNING: Record<PostWarning, string> = {
  approve_against_evidence: "Jesinia's verdict isn't Approve",
  blocking_comment: "You're approving, but the review says BLOCKING",
  newer_head: "There are newer commits than this review",
  own_pr_comment_only: "This is your own PR: GitHub only takes a comment",
};

/**
 * Post all's one confirmation: every PR of the stack with its action, or why it is left out,
 * and every warning a PR's preview raises, each ticked by the author (nothing is confirmed for
 * them); then one request that posts them bottom-up and stops at the first that can't be
 * posted. A retry while the outcome is unknown reuses the request's key, so nothing is ever
 * posted twice.
 */
export function PostAllDialog({ client, repo, number, members, keys, signature, onClose, onPosted }: PostAllProps) {
  const heading = useId();
  const dialog = useRef<HTMLDivElement>(null);
  // Fixed when the dialog opens (and on Check again): what the author confirms is what posts.
  const [planned, setPlanned] = useState(() => plan(members));
  const [choices, setChoices] = useState<Choice[]>(planned.choices);
  const [answers, setAnswers] = useState<Answer[] | null>(null);
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const [reads, setReads] = useState(0);
  const [posting, setPosting] = useState(false);
  const [outcomes, setOutcomes] = useState<PostOutcome[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setAnswers(null);
    setTicked(new Set()); // a new preview's warnings are confirmed afresh
    void Promise.all(
      choices.map((c) =>
        previewPost(client, repo, c.view.pr.number, c.event, c.round.id).then(
          (preview): Answer => ({ preview, error: null }),
          (e: unknown): Answer => ({ preview: null, error: errorCause(e) }),
        ),
      ),
    ).then((got) => {
      if (current) setAnswers(got);
    });
    return () => {
      current = false;
    };
  }, [client, repo, choices, reads]);

  useEffect(() => dialog.current?.focus(), []);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  const choose = (at: number, event: ReviewEvent) =>
    setChoices((prev) => prev.map((c, i) => (i === at ? { ...c, event } : c)));
  const tickOf = (n: number, w: PostWarning) => `${n}:${w}`;
  const tick = (id: string, on: boolean) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const checkAgain = () => {
    const next = plan(members);
    setPlanned(next);
    setChoices(next.choices);
    setOutcomes(null);
  };

  // Every preview is in and every warning it raised is ticked: exactly what each PR posts.
  const previews = answers?.map((a) => a.preview) ?? [];
  const confirmedAll = choices.every((c, i) =>
    (previews[i]?.warnings ?? []).every((w) => ticked.has(tickOf(c.view.pr.number, w))),
  );
  const items: PostAllItem[] | null =
    choices.length > 0 && previews.length === choices.length && previews.every((p) => p !== null) && confirmedAll
      ? choices.map((c, i) => {
          const p = previews[i] as PostPreview;
          return {
            number: c.view.pr.number,
            round_id: c.round.id,
            event: p.event,
            digest: p.digest,
            confirm: p.warnings.filter((w) => ticked.has(tickOf(c.view.pr.number, w))),
          };
        })
      : null;
  const ready = items !== null && !posting && outcomes === null;
  const action = `post-all:${signature}`;
  const post = async (send: PostAllItem[]) => {
    setPosting(true);
    setFailure(null);
    try {
      const done = await postAll(client, repo, number, keys.keyFor(action), send);
      keys.settle(action);
      setOutcomes(done);
    } catch (e) {
      keys.settle(action, e);
      const stopped = e instanceof ReviewRequestError ? e.facts.stackOutcomes : null;
      if (stopped) setOutcomes(stopped);
      const again = e instanceof ReviewRequestError && e.isTransient;
      setFailure(
        `${stopped ? "Post all stopped" : "Couldn't post"}: ${errorCause(e)}${again ? " Post all again to finish: nothing is posted twice." : ""}`,
      );
    } finally {
      setPosting(false);
      onPosted();
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
        onKeyDown={onKey}
      >
        <h2
          id={heading}
          className={s.dialogHead}
        >{`Post all: ${choices.length} ${choices.length === 1 ? "PR" : "PRs"} on GitHub`}</h2>
        <div className={s.dialogBody}>
          {choices.length === 0 ? <p className={s.muted}>No PR of the stack can be posted now.</p> : null}
          {planned.leftOut.length ? (
            <ul className={s.refs} aria-label="Left out">
              {planned.leftOut.map((l) => (
                <li key={l.number} className={s.muted}>{`#${l.number}: ${l.reason}`}</li>
              ))}
            </ul>
          ) : null}
          <ol className={s.refs} aria-label="What will be posted">
            {choices.map((c, i) => {
              const own = c.view.pr.author_kind === "you";
              const answer = answers?.[i];
              const warnings = answer?.preview?.warnings ?? [];
              return (
                <li key={c.view.pr.number} className={s.comment}>
                  <span className={s.label}>
                    <Visible text={titleOf(c.view)} />
                  </span>
                  <label className={s.row}>
                    {`Action for #${c.view.pr.number}`}
                    <select
                      value={c.event}
                      disabled={posting || outcomes !== null}
                      onChange={(e) => choose(i, e.target.value as ReviewEvent)}
                    >
                      {(own ? OWN_EVENTS : EVENTS).map((ev) => (
                        <option key={ev} value={ev}>
                          {ACTION[ev]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Answered answer={answer} />
                  {warnings.map((w) => {
                    const id = tickOf(c.view.pr.number, w);
                    return (
                      <div
                        key={w}
                        role="group"
                        aria-label={`#${c.view.pr.number}: ${WARNING[w]}`}
                        className={s.warning}
                      >
                        <b>{`#${c.view.pr.number}: ${WARNING[w]}`}</b>
                        <label className={s.row}>
                          <input
                            type="checkbox"
                            checked={ticked.has(id)}
                            disabled={posting || outcomes !== null}
                            onChange={(e) => tick(id, e.target.checked)}
                          />
                          {`${ACTION[c.event]} #${c.view.pr.number} anyway`}
                        </label>
                      </div>
                    );
                  })}
                </li>
              );
            })}
          </ol>
          {answers?.some((a) => a.error) ? (
            <span>
              <Button size="sm" variant="secondary" onClick={() => setReads((n) => n + 1)}>
                Try again
              </Button>
            </span>
          ) : null}
          {outcomes ? (
            <ul className={s.refs} aria-label="Post all results">
              {outcomes.map((o) => {
                const url = o.review_url ? safeHttpUrl(o.review_url) : null;
                return (
                  <li key={o.number}>
                    {`#${o.number} ${ACTION[o.event]}: ${STATE[o.state]}`}
                    {o.error ? <span className={s.muted}>{` (${visibleText(o.error)})`}</span> : null}
                    {url ? (
                      <>
                        {" "}
                        <a href={url} target="_blank" rel="noopener noreferrer">
                          on GitHub ↗
                        </a>
                      </>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {failure ? (
            <p role="alert" className={s.problem}>
              {visibleText(failure)}
            </p>
          ) : null}
        </div>
        <div className={s.dialogFoot}>
          <span>Each PR is posted as its own review, bottom to top; Post all stops at the first that fails.</span>
          <Button size="sm" variant="ghost" onClick={onClose}>
            {outcomes ? "Close" : "Cancel"}
          </Button>
          {failure && outcomes ? (
            <Button size="sm" variant="primary" onClick={checkAgain}>
              Check again
            </Button>
          ) : (
            <Button
              size="sm"
              variant="primary"
              disabled={!ready}
              loading={posting}
              onClick={() => items && void post(items)}
            >
              {`Post all (${choices.length})`}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function noticeOf(result: StackOpenResult): string | null {
  if (result.state === "started") {
    const n = result.jobs.length;
    return `Reviewing the stack: ${n} ${n === 1 ? "job" : "jobs"} running. Pages fill in as they finish.`;
  }
  if (result.state === "held") return result.reason;
  if (result.state === "running") return "The stack is being reviewed: pages fill in as they finish.";
  return null;
}

export function StackView({ repo, number, refresh, active = true, onClose }: StackViewProps) {
  const client = useClient();
  const keys = useRef(new ActionKeys());
  const [view, setView] = useState<StackData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reads, setReads] = useState(0);
  const [tab, setTab] = useState("overview");
  const [notice, setNotice] = useState<string | null>(null);
  const [held, setHeld] = useState(false);
  const [postingAll, setPostingAll] = useState(false);
  const opened = useRef<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let current = true;
    getStack(client, repo, number).then(
      (v) => {
        if (!current) return;
        setView(v);
        setError(null);
      },
      (e: unknown) => {
        if (current) setError(errorCause(e));
      },
    );
    return () => {
      current = false;
    };
  }, [client, repo, number, refresh, reads, active]);

  const open = useCallback(
    (retry: boolean) => {
      const action = `stack-open:${repo}#${number}:${retry ? "retry" : "open"}`;
      openStack(client, repo, number, keys.current.keyFor(action), retry).then(
        (r) => {
          keys.current.settle(action);
          setNotice(noticeOf(r));
          setHeld(r.state === "held");
          setReads((n) => n + 1);
        },
        (e: unknown) => {
          keys.current.settle(action, e);
          setNotice(`Couldn't start the stack's review: ${errorCause(e)}`);
        },
      );
    },
    [client, repo, number],
  );

  // A selected PR that left the stack (a restack, a merge) takes the view back to the
  // Overview (Codex round 2 P2 on PR #63).
  useEffect(() => {
    if (view && tab.startsWith("pr-") && !view.members.some((m) => `pr-${m.pr.number}` === tab)) setTab("overview");
  }, [view, tab]);

  // Once per stack review (its PRs at their heads), like a PR's open (R60).
  const signature = view?.signature ?? null;
  useEffect(() => {
    if (signature === null || opened.current === signature) return;
    opened.current = signature;
    open(false);
  }, [signature, open]);

  if (!view) {
    return (
      <section className={s.waiting} aria-label="Stack">
        {error ? (
          <div className={s.problem} role="alert">
            <span>{`Couldn't read this stack: ${error}`}</span>
            <Button size="sm" variant="secondary" onClick={() => setReads((n) => n + 1)}>
              Try again
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              Back to the PR
            </Button>
          </div>
        ) : null}
      </section>
    );
  }

  const a = view.artifacts;
  const jobs = record(a.jobs) ?? {};
  const allOwn = view.members.every((m) => m.pr.author_kind === "you");
  const walk = walkOf(a.stack_walkthrough);
  const numbers = view.members.map((m) => m.pr.number);
  // Failed or cut short by a stop (R73): either runs again only through Retry.
  const retryable = Object.entries(STACK_JOBS).flatMap(([kind, what]) => {
    const state = record(jobs[kind])?.state;
    if (state === "failed") return [`${what} failed.`];
    if (state === "interrupted") return [`${what} was cut short by a stop.`];
    return [];
  });
  const generated = Object.keys(STACK_JOBS).some((kind) => kind in a || kind in jobs);

  const pending = (kind: StackJob) => {
    const job = record(jobs[kind]);
    const what = STACK_JOBS[kind];
    if (job?.state === "running")
      return <p className={s.muted}>{`${what} is being generated: it shows here when it finishes.`}</p>;
    if (job?.state === "failed" || job?.state === "interrupted")
      return (
        <div className={s.row}>
          <p>
            {`${what} ${job.state === "failed" ? "failed" : "was cut short"}: ${visibleText(String(job.error ?? "no cause given"))}`}
          </p>
          <Button size="sm" variant="secondary" onClick={() => open(true)}>
            Retry
          </Button>
        </div>
      );
    return <p className={s.muted}>{`${what} hasn't been generated yet.`}</p>;
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "background", label: allOwn ? "Background · minimised" : "Background" },
    { id: "walk", label: "Stack walkthrough" },
    ...view.members.map((m) => {
      // Long titles are shortened to fit the tab strip; the whole title shows on hover (R73).
      const full = visibleText(`#${m.pr.number} ${m.pr.title}`);
      return { id: `pr-${m.pr.number}`, label: clip(full, TAB_CHARS), title: full };
    }),
    { id: "cross", label: "Cross-PR findings", count: view.cross_findings.length },
  ];

  const shownTab = tabs.some((t) => t.id === tab) ? tab : "overview";

  const memberPage = (m: PrView) => {
    const art = readArtifacts(m);
    const own = m.pr.author_kind === "you";
    const words = verdictWords(art.verdict?.verdict ?? m.pr.recommendation, own);
    const mine = view.cross_findings.filter((f) => f.prs.includes(m.pr.number));
    return (
      <article className={s.page} aria-label={`#${m.pr.number}`}>
        <h2>
          <Visible text={titleOf(m)} />
        </h2>
        <p className={s.meta}>
          <Visible text={`${m.pr.author} · ${m.pr.size_lines} lines · ${m.pr.files} files · CI ${m.pr.ci_state}`} />
        </p>
        {words ? (
          <p className={s.verdict} data-tone={words.tone}>
            {words.text}
          </p>
        ) : (
          <p className={s.muted}>No verdict yet</p>
        )}
        {mine.length ? (
          <section className={s.warning} aria-label="Cross-PR findings">
            <b>{`Cross-PR: ${mine.length} ${mine.length === 1 ? "finding concerns" : "findings concern"} other PRs of the stack`}</b>
            <CrossList findings={mine} />
          </section>
        ) : null}
        <section className={s.group}>
          <h3>Walkthrough</h3>
          <StepList steps={art.steps ?? []} label={`#${m.pr.number}'s walkthrough`} />
        </section>
        {art.findings ? <Findings findings={art.findings} /> : <p className={s.muted}>Findings aren't in yet.</p>}
      </article>
    );
  };

  const page = (id: string): ReactNode => {
    if (id === "overview")
      return a.overview ? (
        <Overview overview={a.overview as OverviewData} context={`One overview for #${numbers.join(", #")}`} />
      ) : (
        <article className={s.page} aria-label="Overview">
          <h2>Overview</h2>
          {pending("overview")}
        </article>
      );
    if (id === "background") {
      const sections = array<BackgroundSection>(record(a.background)?.sections);
      return (
        <>
          {allOwn ? <p className={s.context}>You wrote this stack: its background is kept to a minimum.</p> : null}
          {record(a.background) ? (
            <Background sections={sections} />
          ) : (
            <article className={s.page} aria-label="Background">
              <h2>Background</h2>
              {pending("background")}
            </article>
          )}
        </>
      );
    }
    if (id === "walk")
      return (
        <article className={s.page} aria-label="Stack walkthrough">
          <h2>Stack walkthrough</h2>
          {walk ? (
            <>
              <ul className={s.refs} aria-label="The PRs, bottom to top">
                {walk.prs.map((p) => (
                  <li key={p.number}>
                    <b>{`#${p.number}`}</b>
                    <div className={s.prose}>
                      <SafeMarkdown markdown={p.summary} />
                    </div>
                  </li>
                ))}
              </ul>
              <StepList steps={walk.steps} label="Across the stack" />
              {walk.skipped.length ? (
                <ul className={s.refs} aria-label="Left out">
                  {walk.skipped.map((k, i) => (
                    <li key={i} className={s.muted}>
                      <Visible
                        text={`#${k.pr} ${k.path}${k.hunk === null ? "" : ` · hunk ${k.hunk + 1}`}: ${k.reason.replaceAll("_", " ")}`}
                      />
                    </li>
                  ))}
                </ul>
              ) : null}
            </>
          ) : (
            pending("stack_walkthrough")
          )}
        </article>
      );
    if (id === "cross")
      return (
        <article className={s.page} aria-label="Cross-PR findings">
          <h2>Cross-PR findings</h2>
          <CrossList findings={view.cross_findings} />
        </article>
      );
    // Every other tab is a PR's (`pr-<n>`, from the members themselves); one that left the
    // stack shows the Overview until the selection follows.
    const found = view.members.find((m) => `pr-${m.pr.number}` === id);
    return found ? memberPage(found) : page("overview");
  };

  return (
    <section className={s.view} aria-label={`Stack of ${view.members.length} PRs`}>
      <div className={s.toolbar} role="group" aria-label="Stack tools">
        <Button size="sm" variant="ghost" onClick={onClose}>
          ← This PR only
        </Button>
        <span className={s.pill}>{`Stack of ${view.members.length}`}</span>
        <span className={s.muted}>{`#${numbers.join(" → #")}`}</span>
        <span className={s.spacer}>
          <Button size="sm" variant="primary" onClick={() => setPostingAll(true)}>
            Post all…
          </Button>
        </span>
      </div>
      <div className={s.notices}>
        {notice ? (
          <p className={s.notice} role="status">
            {visibleText(notice)}
          </p>
        ) : null}
        {held || !generated ? (
          <span>
            <Button size="sm" variant="secondary" onClick={() => open(false)}>
              Review the stack
            </Button>
          </span>
        ) : null}
        {retryable.length && !notice ? (
          <div className={s.problem} role="status">
            <span>{retryable.join(" ")}</span>
            <Button size="sm" variant="secondary" onClick={() => open(true)}>
              Retry
            </Button>
          </div>
        ) : null}
      </div>
      <div className={s.scroll}>
        <Tabs aria-label="Stack review" tabs={tabs} selected={shownTab} onSelect={setTab}>
          {page}
        </Tabs>
      </div>
      {postingAll ? (
        <PostAllDialog
          client={client}
          repo={repo}
          number={number}
          members={view.members}
          keys={keys.current}
          signature={view.signature}
          onClose={() => setPostingAll(false)}
          onPosted={() => setReads((n) => n + 1)}
        />
      ) : null}
    </section>
  );
}
