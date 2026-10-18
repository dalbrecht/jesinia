// SPDX-License-Identifier: AGPL-3.0-or-later
// The Pending changes tray (spec §6.1, §6.2). Every GitHub-changing action except an approval
// stages here (drags, the toolbar pickers, menus, the bulk bar); Apply (⌘↵) sends the tray as one
// batch. Applies, retries and approvals answer 202 with a batch (ruling IR45), polled here to its
// per-issue results with Retry, and Abandon for a batch that creates issues (IR58). Every write
// carries an Idempotency-Key, reused while its outcome is unknown, so a replay answers the same
// batch (IR46).
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
  describeSuggestion,
  errorCause,
  getBatch,
  getPending,
  issueResult,
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

/** How often a running batch is read again. */
export const BATCH_POLL_MS = 1000;

type Tracked = { batch: ApplyBatch; title: string };

export type TrayApi = {
  pending: Pending[];
  /** Stage a change; resolves to whether it was staged (a refusal is toasted). */
  stage: (repo: string, n: number, change: StageableChange, source: PendingSource) => Promise<boolean>;
  /** Approve a suggestion: it applies at once, and its batch is followed here. */
  approve: (suggestion: Suggestion) => Promise<boolean>;
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

/** Holds the tray's state and renders it, pinned below `children`, whenever it isn't empty. */
export function TrayProvider({ children }: { children: ReactNode }) {
  const client = useClient();
  const toast = useToast();
  const keys = useRef(new ActionKeys());
  const [pending, setPending] = useState<Pending[]>([]);
  const [tracked, setTracked] = useState<Tracked[]>([]);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [revision, setRevision] = useState(0);

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

  const readPending = useCallback(async () => {
    try {
      setPending(await getPending(client));
    } catch (e) {
      fail("read the pending changes", e);
    }
  }, [client, fail]);

  useEffect(() => {
    void readPending();
  }, [readPending]);

  /** Show `batch`'s latest state where it is followed; a settled batch refreshes the views. */
  const update = useCallback((batch: ApplyBatch) => {
    setTracked((prev) => prev.map((t) => (t.batch.id === batch.id ? { ...t, batch } : t)));
    if (batch.status !== "running") setRevision((r) => r + 1);
  }, []);

  /** Follow a batch just started (a replayed key answers one already followed: it updates). */
  const track = useCallback(
    (batch: ApplyBatch, title: string) => {
      setTracked((prev) => (prev.some((t) => t.batch.id === batch.id) ? prev : [...prev, { batch, title }]));
      update(batch);
    },
    [update],
  );

  // Poll the running batches until each settles (IR45: clients poll GET /pending/{batch}).
  const running = tracked.filter((t) => t.batch.status === "running").map((t) => t.batch.id);
  const runningKey = running.join(",");
  useEffect(() => {
    if (runningKey === "") return;
    const timer = setTimeout(() => {
      for (const id of runningKey.split(",")) {
        getBatch(client, id).then(
          (batch) => update(batch),
          () => {
            // A failed read is tried again on the next tick; the batch runs on regardless.
            setTracked((prev) => [...prev]);
          },
        );
      }
    }, BATCH_POLL_MS);
    return () => clearTimeout(timer);
  }, [client, runningKey, tracked, update]);

  const stage = useCallback<TrayApi["stage"]>(
    async (repo, n, change, source) => {
      try {
        await keyed(`stage:${repo}#${n}:${JSON.stringify(change)}`, (key) =>
          stageChange(client, { repo, n, change, source }, key),
        );
        await readPending();
        return true;
      } catch (e) {
        fail(`stage "${describeChange(change)}" on ${repo}#${n}`, e);
        return false;
      }
    },
    [client, keyed, readPending, fail],
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
        return false;
      }
    },
    [client, keyed, track, fail],
  );

  const apply = useCallback(async () => {
    const count = pending.length;
    try {
      const batch = await keyed("apply", (key) => applyPending(client, key));
      track(batch, `${plural(count, "staged change", "staged changes")} from the tray`);
    } catch (e) {
      fail("apply the pending changes", e);
    }
    await readPending();
  }, [client, keyed, pending.length, track, readPending, fail]);

  const undo = async (change: Pending) => {
    try {
      await keyed(`undo:${change.id}`, (key) => unstageChange(client, change.id, key));
    } catch (e) {
      fail(`undo "${describeChange(change.change)}"`, e);
    }
    await readPending();
  };

  const undoAll = async () => {
    try {
      await keyed("undo-all", (key) => clearPending(client, key));
    } catch (e) {
      fail("undo the pending changes", e);
    }
    await readPending();
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
      update({ ...batch, status: "abandoned" });
      toast.show({ tone: "info", message: "Gave up on that batch: the next approval reuses what it created." });
    } catch (e) {
      fail("abandon the batch", e);
    }
  };

  const dismiss = (batch: ApplyBatch) => setTracked((prev) => prev.filter((t) => t.batch.id !== batch.id));

  // ⌘↵ (Ctrl+Enter elsewhere) applies the tray from anywhere in the app.
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const hasPending = pending.length > 0;
  useEffect(() => {
    if (!hasPending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      void applyRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hasPending]);

  const api = useMemo<TrayApi>(() => ({ pending, stage, approve, revision }), [pending, stage, approve, revision]);
  const isEmpty = pending.length === 0 && tracked.length === 0;

  return (
    <TrayContext.Provider value={api}>
      {children}
      {isEmpty ? null : (
        <section className={s.tray} aria-label="Pending changes">
          {pending.length > 0 ? (
            <>
              <header className={s.trayHead}>
                <h2 className={s.trayTitle}>Pending changes</h2>
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
                  onDismiss={() => dismiss(t.batch)}
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

const TONE = { applied: "success", failed: "danger", skipped: "warn" } as const;
const ICON = { applied: "✓", failed: "✕", skipped: "–" } as const;

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
          <Button size="sm" variant="ghost" onClick={onDismiss}>
            Dismiss
          </Button>
        )}
      </div>
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
          const outcome = isRunning && issue.steps.every((step) => step.state !== "done") ? null : issueResult(issue);
          return (
            <li key={label} className={s.outcome} aria-label={label}>
              <span className={s.ref}>{label}</span>
              {outcome ? (
                <Badge tone={TONE[outcome.state]} icon={ICON[outcome.state]}>
                  <VisibleText text={outcome.text} />
                </Badge>
              ) : (
                <span className={s.muted}>Not run yet</span>
              )}
              {outcome?.conflict ? (
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
