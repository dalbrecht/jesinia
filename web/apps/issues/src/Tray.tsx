// SPDX-License-Identifier: AGPL-3.0-or-later
// The Pending changes tray (spec §6.1, §6.2). Every GitHub-changing action except an approval
// stages here (drags, the toolbar pickers, menus, the bulk bar); Apply (⌘↵) sends the tray as one
// batch. Applies, retries and approvals answer 202 with a batch (ruling IR45), polled here to its
// per-issue results with Retry, and Abandon for a batch that creates issues (IR58). Every write
// carries an Idempotency-Key, reused while its outcome is unknown, so a replay answers the same
// batch (IR46). Batches that didn't finish are read from the server on load, and an
// EarlierBatchError's batch is followed, so Retry and Abandon are always where the copy says.
import { Badge, Button, Spinner, useToast } from "@jesinia/design";
import { useClient, VisibleText } from "@jesinia/shell";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  abandonBatch,
  ActionKeys,
  applyPending,
  approveSuggestion,
  batchHeadline,
  clearPending,
  createsIssues,
  describeChange,
  dismissBatch,
  describeSuggestion,
  errorCause,
  getBatch,
  getPending,
  getUnfinishedBatches,
  issueResult,
  IssuesRequestError,
  retryBatch,
  stageChange,
  unstageChange,
  type ApplyBatch,
  type ApplyIssue,
  type Pending,
  type PendingSource,
  type StageableChange,
  type Suggestion,
} from "./api";
import s from "./issues.module.css";

/** How often a running batch is read again (doubling while reads fail, up to the cap). */
export const BATCH_POLL_MS = 1000;
export const BATCH_POLL_MAX_MS = 10_000;

type Tracked = { batch: ApplyBatch; title: string };
export type Staging = { repo: string; n: number; change: StageableChange; source: PendingSource };

export type TrayApi = {
  /** Stage a change; resolves to whether it was staged (a refusal is toasted). */
  stage: (repo: string, n: number, change: StageableChange, source: PendingSource) => Promise<boolean>;
  /** Stage several changes in order (one drop of a multi-selection); resolves to how many. */
  stageAll: (changes: Staging[]) => Promise<number>;
  /** Approve a suggestion: it applies at once, and its batch is followed here. */
  approve: (suggestion: Suggestion) => Promise<boolean>;
  /** Whether an approval of this suggestion is being sent. */
  isApproving: (suggestionId: string) => boolean;
  /** Bumped whenever a batch settles, so views read the cache again. */
  revision: number;
};

const TrayContext = createContext<TrayApi | null>(null);

export function useTray(): TrayApi {
  const tray = useContext(TrayContext);
  if (tray === null) throw new Error("useTray needs a TrayProvider");
  return tray;
}

const plural = (n: number, one: string, other: string) => `${n} ${n === 1 ? one : other}`;

const UNFINISHED_TITLE: Record<ApplyBatch["origin"], string> = {
  pending: "Unfinished: staged changes",
  suggestion: "Unfinished: an approval",
  epic: "Unfinished: an epic batch",
};

/** Where focus goes when what held it leaves: the tray's heading, its first result, else home. */
function refocus(section: HTMLElement | null) {
  const active = document.activeElement;
  if (active !== null && active !== document.body && document.contains(active)) return;
  const inside = section?.querySelector<HTMLElement>("[data-tray-focus]") ?? section?.querySelector<HTMLElement>("button");
  const home = document.querySelector<HTMLElement>("[data-focus-home]");
  (inside ?? home)?.focus();
} // prettier-ignore

const isEditable = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.isContentEditable ||
    target instanceof HTMLTextAreaElement ||
    (target instanceof HTMLInputElement && !["checkbox", "radio", "button", "submit"].includes(target.type)));

/** Holds the tray's state and renders it, pinned below `children`, whenever it isn't empty. */
export function TrayProvider({ children }: { children: ReactNode }) {
  const client = useClient();
  const toast = useToast();
  const keys = useRef(new ActionKeys());
  const sectionRef = useRef<HTMLElement>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [tracked, setTracked] = useState<Tracked[]>([]);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [revision, setRevision] = useState(0);
  const [focusFix, setFocusFix] = useState(0);
  const [failedPolls, setFailedPolls] = useState(0);
  // Reads of the tray can resolve out of order: only a newer one than the last shown is kept.
  const reads = useRef({ issued: 0, shown: 0 });

  const fail = useCallback(
    (what: string, e: unknown) => toast.show({ tone: "danger", message: `Couldn't ${what}: ${errorCause(e)}` }),
    [toast],
  );

  /** Runs a keyed write for `action`: a retry while its outcome is unknown reuses its key. */
  const keyed = useCallback(async <T,>(action: string, work: (key: string) => Promise<T>): Promise<T> => {
    setBusy((prev) => new Set(prev).add(action));
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(action);
        return next;
      });
    }
  }, []);

  /** Reads the tray; resolves to the changes now staged (null when it couldn't be read). */
  const readPending = useCallback(async (): Promise<Pending[] | null> => {
    const id = ++reads.current.issued;
    try {
      const found = await getPending(client);
      if (id > reads.current.shown) {
        reads.current.shown = id;
        setPending(found);
      }
      return found;
    } catch (e) {
      fail("read the pending changes", e);
      return null;
    }
  }, [client, fail]);

  /** Show `batch`'s latest state where it is followed; a settled batch refreshes the views. */
  const update = useCallback((batch: ApplyBatch) => {
    setTracked((prev) => prev.map((t) => (t.batch.id === batch.id ? { ...t, batch } : t)));
    if (batch.status !== "running") setRevision((r) => r + 1);
  }, []);

  /** Follow a batch (a replayed key answers one already followed: it updates). */
  const track = useCallback(
    (batch: ApplyBatch, title: string) => {
      setTracked((prev) => (prev.some((t) => t.batch.id === batch.id) ? prev : [...prev, { batch, title }]));
      update(batch);
    },
    [update],
  );

  useEffect(() => {
    void readPending();
    // A reload (or another window) mustn't lose a batch the author still has to retry or abandon.
    getUnfinishedBatches(client).then(
      (batches) => {
        for (const batch of batches) track(batch, UNFINISHED_TITLE[batch.origin]);
      },
      (e: unknown) => fail("read the unfinished batches", e),
    );
  }, [client, readPending, track, fail]);

  /** Follow the batch an EarlierBatchError names, so its Retry and Abandon are right here. */
  const followEarlier = useCallback(
    (e: unknown) => {
      if (!(e instanceof IssuesRequestError) || e.batchId === null) return;
      getBatch(client, e.batchId).then(
        (batch) => track(batch, "Earlier batch: retry or abandon it first"),
        (error: unknown) => fail("read the earlier batch", error),
      );
    },
    [client, track, fail],
  );

  // Poll the running batches until each settles (IR45), backing off while reads fail.
  const runningKey = tracked
    .filter((t) => t.batch.status === "running")
    .map((t) => t.batch.id)
    .join(",");
  useEffect(() => {
    if (runningKey === "") return;
    const wait = Math.min(BATCH_POLL_MAX_MS, BATCH_POLL_MS * 2 ** failedPolls);
    const timer = setTimeout(() => {
      Promise.all(runningKey.split(",").map((id) => getBatch(client, id))).then(
        (batches) => {
          setFailedPolls(0);
          for (const batch of batches) update(batch);
        },
        // Read again later; the batch runs on regardless.
        () => setFailedPolls((n) => n + 1),
      );
    }, wait);
    return () => clearTimeout(timer);
  }, [client, runningKey, tracked, failedPolls, update]);

  const send = useCallback(
    async ({ repo, n, change, source }: Staging): Promise<boolean> => {
      try {
        await keyed(`stage:${repo}#${n}:${JSON.stringify(change)}`, (key) =>
          stageChange(client, { repo, n, change, source }, key),
        );
        return true;
      } catch (e) {
        fail(`stage "${describeChange(change)}" on ${repo}#${n}`, e);
        return false;
      }
    },
    [client, keyed, fail],
  );

  const stageAll = useCallback<TrayApi["stageAll"]>(
    async (changes) => {
      let staged = 0;
      for (const change of changes) if (await send(change)) staged += 1;
      const now = await readPending();
      if (staged > 0) {
        const what = staged === 1 && changes[0] ? describeChange(changes[0].change) : plural(staged, "change", "changes");
        const count = now === null ? "" : ` (${plural(now.length, "change", "changes")} pending)`;
        toast.show({ tone: "info", message: `Staged: ${what}${count}. Nothing is sent until you apply.` });
      }
      return staged;
    },
    [send, readPending, toast],
  ); // prettier-ignore

  const stage = useCallback<TrayApi["stage"]>(
    async (repo, n, change, source) => (await stageAll([{ repo, n, change, source }])) === 1,
    [stageAll],
  );

  const approve = useCallback<TrayApi["approve"]>(
    async (suggestion) => {
      const where = `${suggestion.repo ?? ""}#${suggestion.n ?? ""}`;
      const what = describeSuggestion(suggestion);
      try {
        const approval = await keyed(`approve:${suggestion.id}`, (key) =>
          approveSuggestion(client, suggestion.id, key),
        );
        track(approval.batch, `Approved: ${what} on ${where}`);
        return true;
      } catch (e) {
        fail(`approve "${what}" on ${where}`, e);
        followEarlier(e);
        return false;
      }
    },
    [client, keyed, track, fail, followEarlier],
  );

  const apply = useCallback(async () => {
    const count = pending.length;
    try {
      const batch = await keyed("apply", (key) => applyPending(client, key));
      track(batch, `${plural(count, "staged change", "staged changes")} from the tray`);
    } catch (e) {
      fail("apply the pending changes", e);
      followEarlier(e);
    }
    await readPending();
    setFocusFix((n) => n + 1);
  }, [client, keyed, pending.length, track, readPending, fail, followEarlier]);

  const undo = async (change: Pending) => {
    try {
      await keyed(`undo:${change.id}`, (key) => unstageChange(client, change.id, key));
    } catch (e) {
      fail(`undo "${describeChange(change.change)}"`, e);
    }
    await readPending();
    setFocusFix((n) => n + 1);
  };

  const undoAll = async () => {
    try {
      await keyed("undo-all", (key) => clearPending(client, key));
    } catch (e) {
      fail("undo the pending changes", e);
    }
    await readPending();
    setFocusFix((n) => n + 1);
  };

  const retry = async (batch: ApplyBatch) => {
    try {
      update(await keyed(`retry:${batch.id}`, (key) => retryBatch(client, batch.id, key)));
    } catch (e) {
      fail("retry the batch", e);
    }
  };

  const abandon = async (batch: ApplyBatch) => {
    try {
      await keyed(`abandon:${batch.id}`, (key) => abandonBatch(client, batch.id, key));
      toast.show({ tone: "info", message: "Gave up on that batch: the next approval reuses what it created." });
      update(await getBatch(client, batch.id));
    } catch (e) {
      fail("abandon the batch", e);
    }
  };

  // Dismissed on the server, so a reload never brings it back (re-review N2).
  const dismiss = async (batch: ApplyBatch) => {
    try {
      await keyed(`dismiss:${batch.id}`, (key) => dismissBatch(client, batch.id, key));
    } catch (e) {
      fail("dismiss the batch", e);
      return;
    }
    setTracked((prev) => prev.filter((t) => t.batch.id !== batch.id));
    setFocusFix((n) => n + 1);
  };

  // A row, a result or the whole tray that held focus is gone: put focus somewhere sensible.
  useEffect(() => {
    if (focusFix > 0) refocus(sectionRef.current);
  }, [focusFix]);

  // ⌘↵ (Ctrl+Enter elsewhere) applies the tray from anywhere in the app, but not from a field
  // being typed in (where it may mean something else).
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const hasPending = pending.length > 0;
  useEffect(() => {
    if (!hasPending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey) || isEditable(e.target)) return;
      e.preventDefault();
      void applyRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hasPending]);

  const isApproving = useCallback((id: string) => busy.has(`approve:${id}`), [busy]);
  const api = useMemo<TrayApi>(
    () => ({ stage, stageAll, approve, isApproving, revision }),
    [stage, stageAll, approve, isApproving, revision],
  );
  const isEmpty = pending.length === 0 && tracked.length === 0;

  return (
    <TrayContext.Provider value={api}>
      {children}
      {isEmpty ? null : (
        <section ref={sectionRef} className={s.tray} aria-label="Pending changes">
          {pending.length > 0 ? (
            <>
              <header className={s.trayHead}>
                <h2 className={s.trayTitle} tabIndex={-1} data-tray-focus="">
                  Pending changes
                </h2>
                <span className={s.muted}>{plural(pending.length, "change", "changes")}, nothing sent yet</span>
                <span className={s.spacer} />
                <Button variant="ghost" size="sm" loading={busy.has("undo-all")} onClick={() => void undoAll()}>
                  Undo all
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  loading={busy.has("apply")}
                  aria-keyshortcuts="Meta+Enter Control+Enter"
                  onClick={() => void apply()}
                >
                  Apply <kbd className={s.kbd}>⌘↵</kbd>
                </Button>
              </header>
              <ul className={s.trayList}>
                {pending.map((change) => {
                  const what = describeChange(change.change);
                  const where = change.n === null ? change.repo : `${change.repo}#${change.n}`;
                  return (
                    <li key={change.id} className={s.trayRow}>
                      <span className={s.ref}>{where}</span>
                      <span className={s.trayWhat}>
                        <VisibleText text={what} />
                      </span>
                      <Badge>{change.source}</Badge>
                      <Button
                        variant="ghost"
                        size="sm"
                        loading={busy.has(`undo:${change.id}`)}
                        aria-label={`Undo: ${what} on ${where}`}
                        onClick={() => void undo(change)}
                      >
                        Undo
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : null}
          {tracked.length > 0 ? (
            <ul className={s.batches} aria-label="Results">
              {tracked.map((t) => (
                <BatchResult
                  key={t.batch.id}
                  tracked={t}
                  busy={busy}
                  onRetry={() => void retry(t.batch)}
                  onAbandon={() => void abandon(t.batch)}
                  onDismiss={() => void dismiss(t.batch)}
                />
              ))}
            </ul>
          ) : null}
        </section>
      )}
    </TrayContext.Provider>
  );
}

function issueLabel(issue: ApplyIssue, batch: ApplyBatch): string {
  if (issue.repo !== null && issue.n !== null) return `${issue.repo}#${issue.n}`;
  const made = batch.created.find((c) => c.temp_id === issue.temp_id);
  return made ? `${made.repo}#${made.n}` : `new issue ${issue.temp_id ?? ""}`.trim();
}

const TONE = { applied: "success", failed: "danger", skipped: "warn", pending: "neutral" } as const;
const ICON = { applied: "✓", failed: "✕", skipped: "–", pending: "…" } as const;

const clock = (at: string) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function BatchResult({
  tracked,
  busy,
  onRetry,
  onAbandon,
  onDismiss,
}: {
  tracked: Tracked;
  busy: ReadonlySet<string>;
  onRetry: () => void;
  onAbandon: () => void;
  onDismiss: () => void;
}) {
  const { batch, title } = tracked;
  const isRunning = batch.status === "running";
  const isUnfinished = batch.status === "interrupted" || (batch.status === "finished" && !batch.complete);
  const repoProblems = batch.repo_steps.filter((step) => step.state !== "done");
  const resetAt = batch.rate_limited_until;
  return (
    <li className={s.batch}>
      <div className={s.batchHead}>
        <span className={s.batchTitle}>
          <VisibleText text={title} />
        </span>
        <span className={s.batchState} data-state={batch.status} role="status">
          {isRunning ? <Spinner /> : null} {batchHeadline(batch)}
        </span>
        <span className={s.spacer} />
        {isUnfinished ? (
          <Button size="sm" loading={busy.has(`retry:${batch.id}`)} onClick={onRetry}>
            Retry
          </Button>
        ) : null}
        {isUnfinished && createsIssues(batch) ? (
          <Button size="sm" variant="ghost" loading={busy.has(`abandon:${batch.id}`)} onClick={onAbandon}>
            Abandon
          </Button>
        ) : null}
        {isRunning ? null : (
          <Button size="sm" variant="ghost" loading={busy.has(`dismiss:${batch.id}`)} onClick={onDismiss}>
            Dismiss
          </Button>
        )}
      </div>
      {isUnfinished && resetAt !== null ? (
        <p className={s.muted}>{`GitHub's rate limit resets at ${clock(resetAt)}: a retry before then stops again.`}</p>
      ) : null}
      <ul className={s.outcomes}>
        {repoProblems.map((step, i) => (
          <li
            key={`repo-${i}`}
            className={s.outcome}
            aria-label={`${step.repo} ${step.label ?? step.temp_id ?? step.kind}`}
          >
            <span className={s.ref}>{step.repo}</span>
            <Badge tone={step.state === "failed" ? "danger" : "warn"} icon={step.state === "failed" ? "✕" : "–"}>
              <VisibleText text={`${step.kind} ${step.state}: ${step.cause ?? "no cause given"}`} />
            </Badge>
          </li>
        ))}
        {batch.issues.map((issue) => {
          const label = issueLabel(issue, batch);
          const outcome = issueResult(issue, isRunning);
          return (
            <li key={label} className={s.outcome} aria-label={label}>
              <span className={s.ref}>{label}</span>
              <Badge tone={TONE[outcome.state]} icon={ICON[outcome.state]}>
                <VisibleText text={outcome.text} />
              </Badge>
              {outcome.conflict ? (
                <Badge tone="warn" icon="⚠">
                  Changed on GitHub
                </Badge>
              ) : null}
            </li>
          );
        })}
      </ul>
    </li>
  );
}
