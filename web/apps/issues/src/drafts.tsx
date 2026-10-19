// SPDX-License-Identifier: AGPL-3.0-or-later
// The agent's drafts for epics and queues (spec §4, §5.2): ask in the background (202 with the
// ask's `scope`), read the scope again every 2 s until the ask settles, then list the drafts.
// Group and split drafts approve through the engine (one batch, followed in the tray); the
// creation guards' refusals (IR56–IR60) come back with what to do: retry or abandon the earlier
// batch (it is followed in the tray), or add the issues to the epic that already exists.
import { Button, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ActionKeys,
  dismissSuggestion,
  errorCause,
  getDrafts,
  epicOfRefusal,
  setParentChange,
  type AskRun,
  type Suggestion,
} from "./api";
import { useTray } from "./Tray";

/** How often an ask is read again while the agent works. */
export const ASK_POLL_MS = 2000;

type Query = { scope?: string; kind?: string; repo?: string };

/** What an ask ended with, in a sentence; null while it runs or when nothing was asked. */
export function askStatus(run: AskRun | null): string | null {
  if (run === null) return null;
  if (run.status === "running") return "Claude is working on it…";
  if (run.status === "waiting") return "No agent runner is set up, so nothing was asked. Set one up in Settings, then ask again."; // prettier-ignore
  if (run.status === "failed") return `The ask failed: ${run.cause ?? "no cause given"}`;
  return run.suggestion_id === null ? "Claude proposed nothing this time." : null;
}

export type Drafts = {
  drafts: Suggestion[];
  run: AskRun | null;
  status: string | null;
  isAsking: boolean;
  /** Ask the agent (`send` posts the ask with its idempotency key). */
  ask: (action: string, send: (key: string) => Promise<AskRun>) => Promise<void>;
  dismiss: (draft: Suggestion) => Promise<void>;
  reload: () => Promise<void>;
};

/** The open drafts `query` names (`keep` filters them), and asks that make more. */
export function useDrafts(query: Query | null, keep: (draft: Suggestion) => boolean = () => true): Drafts {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const keys = useRef(new ActionKeys());
  const [drafts, setDrafts] = useState<Suggestion[]>([]);
  const [run, setRun] = useState<AskRun | null>(null);
  const [isAsking, setIsAsking] = useState(false);
  const key = query === null ? null : JSON.stringify(query);
  const reads = useRef(0);

  // Another scope (another repo, say) starts clean: the last ask's outcome isn't this one's.
  useEffect(() => {
    setRun(null);
    setDrafts([]);
  }, [key]);

  const reload = useCallback(async () => {
    if (key === null) return;
    const id = ++reads.current;
    try {
      const found = await getDrafts(client, JSON.parse(key) as Query);
      if (id === reads.current) setDrafts(found.suggestions);
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't read the drafts: ${errorCause(e)}` });
    }
  }, [client, key, toast]);

  useEffect(() => {
    void reload();
  }, [reload, tray.revision]);

  // While an ask runs, read its scope again; when it settles, list what it made.
  const asked = run?.status === "running" ? run.scope : null;
  useEffect(() => {
    if (asked === null) return;
    const timer = setTimeout(() => {
      getDrafts(client, { scope: asked }).then(
        (found) => {
          const latest = found.ask_run ?? { ...(run as AskRun), status: "done" as const };
          setRun(latest);
          if (latest.status !== "running") void reload();
        },
        // Read again on the next tick; the ask runs on regardless.
        () => setRun((prev) => (prev === null ? prev : { ...prev })),
      );
    }, ASK_POLL_MS);
    return () => clearTimeout(timer);
  }, [client, asked, run, reload]);

  const ask = useCallback<Drafts["ask"]>(
    async (action, send) => {
      setIsAsking(true);
      try {
        const started = await send(keys.current.keyFor(action));
        keys.current.settle(action);
        setRun(started);
      } catch (e) {
        keys.current.settle(action, e);
        toast.show({ tone: "danger", message: `Couldn't ask Claude: ${errorCause(e)}` });
      } finally {
        setIsAsking(false);
      }
    },
    [toast],
  );

  const dismiss = useCallback<Drafts["dismiss"]>(
    async (draft) => {
      const action = `dismiss:${draft.id}`;
      try {
        await dismissSuggestion(client, draft.id, keys.current.keyFor(action));
        keys.current.settle(action);
      } catch (e) {
        keys.current.settle(action, e);
        toast.show({ tone: "danger", message: `Couldn't dismiss the draft: ${errorCause(e)}` });
      }
      await reload();
    },
    [client, reload, toast],
  );

  // Filtered at render, so a filter that depends on what loaded later (an epic's children) applies.
  return {
    drafts: drafts.filter(keep),
    run,
    status: askStatus(run),
    isAsking: isAsking || asked !== null,
    ask,
    dismiss,
    reload,
  };
}

type Refused = { text: string; action: ReactNode };
type RefIssue = { repo?: unknown; n?: unknown };

/** Approving group and split drafts, with the creation guards' refusals turned into actions. */
export function useProposalApproval(onOpenEpic?: (repo: string, n: number) => void) {
  const tray = useTray();
  const [problems, setProblems] = useState<ReadonlyMap<string, Refused>>(new Map());

  const setProblem = (id: string, refused: Refused | null) =>
    setProblems((prev) => {
      const next = new Map(prev);
      if (refused === null) next.delete(id);
      else next.set(id, refused);
      return next;
    });

  const approve = async (draft: Suggestion) => {
    setProblem(draft.id, null);
    await tray.approve(draft, (e) => {
      const text = errorCause(e);
      const existing = epicOfRefusal(e, "GroupEpicExistsError");
      if (existing === null) {
        // An EarlierBatchError's batch is followed in the tray, with its Retry and Abandon.
        setProblem(draft.id, { text, action: null });
        return;
      }
      const issues = (Array.isArray(draft.payload.issues) ? draft.payload.issues : []) as RefIssue[];
      const where = `${existing.repo}#${existing.number}`;
      const addInstead = async () => {
        const staged = await tray.stageAll(
          issues.map((issue) => ({
            repo: String(issue.repo),
            n: Number(issue.n),
            change: setParentChange({ repo: existing.repo, n: existing.number }, null),
            source: "menu" as const,
          })),
        );
        if (staged > 0) setProblem(draft.id, null);
      };
      setProblem(draft.id, {
        text,
        action: (
          <>
            <Button size="sm" onClick={() => void addInstead()}>
              {`Add them to ${where} instead`}
            </Button>
            {onOpenEpic ? (
              <Button size="sm" variant="ghost" onClick={() => onOpenEpic(existing.repo, existing.number)}>
                {`Open ${where}`}
              </Button>
            ) : null}
          </>
        ),
      });
    });
  };

  return { approve, problems, isApproving: tray.isApproving };
}
