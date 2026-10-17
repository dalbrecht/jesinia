// SPDX-License-Identifier: AGPL-3.0-or-later
// Your comments (spec §7): the review summary and every draft comment, edited in Jesinia and
// saved to the round (`PUT /drafts`). A save is keyed by its content: retrying the same save
// reuses its key, and any edit makes a new one.
import type { JesiniaClient } from "@jesinia/client";
import { Badge, Button } from "@jesinia/design";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActionKeys,
  errorCause,
  getDrafts,
  putDrafts,
  ReviewRequestError,
  type Draft,
  type DraftSet,
  type DraftsUpdate,
} from "../api";
import { lines, visibleText, type Finding } from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";

export type LocalDraft = { key: string; draft: Draft; isNew: boolean };
type Status =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string; invalid: string | null }
  | { kind: "stale" };

/** Where a draft is anchored, in words: "path:14–15", or the path for a note on the file. */
export function draftLocation(d: Draft): string {
  if (d.line === null || d.line === undefined) return d.path;
  return `${d.path}:${lines({ start_line: d.start_line ?? d.line, end_line: d.line })}`;
}

function toBody(d: Draft): Draft {
  const body: Draft = {
    path: d.path,
    body: d.body,
    line: d.line ?? null,
    side: d.side ?? null,
    start_line: d.start_line ?? null,
    start_side: d.start_side ?? null,
    origin_finding: d.origin_finding ?? null,
    outdated: d.outdated ?? false,
    outside_diff: d.outside_diff ?? false,
  };
  return d.id === null || d.id === undefined ? body : { id: d.id, ...body };
}

export type DraftsState = ReturnType<typeof useDrafts>;

type UseDraftsArgs = {
  client: JesiniaClient;
  repo: string;
  number: number;
  /** False while the PR has no round (nothing to draft on yet). */
  enabled: boolean;
  /** Changes whenever the review was read again: the drafts are read again too, unless edited. */
  refresh: number;
  /** Whether the review is shown: a hidden one reads nothing until shown again (final I1). */
  active?: boolean;
};

/** Typing pauses this long before the drafts save on their own (R59). */
export const AUTOSAVE_MS = 800;

/**
 * The drafts of a PR's latest round, with one serialised save queue (R59, I1, I2):
 * - every edit bumps a revision; the queue saves until the saved revision is the latest, one
 *   PUT at a time, so saves never race and the latest content wins;
 * - each distinct content has its own idempotency key, reused only to retry that same content;
 * - text typed while a save is in flight is kept (only ids and placement are taken from the
 *   answer), and draft keys stay stable, so nobody loses a word or their cursor;
 * - it saves after a pause in typing, on blur, when the window is hidden and on unmount, and
 *   guards leaving the page while anything is unsaved.
 */
export function useDrafts({ client, repo, number, enabled, refresh, active = true }: UseDraftsArgs) {
  const keys = useRef(new ActionKeys());
  const counter = useRef(0);
  const [set, setSet] = useState<DraftSet | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [summary, setSummaryState] = useState("");
  const [items, setItemsState] = useState<LocalDraft[]>([]);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [again, setAgain] = useState(0);
  const [unsaved, setUnsaved] = useState(false);
  const summaryRef = useRef("");
  const itemsRef = useRef<LocalDraft[]>([]);
  const revision = useRef(0);
  const savedRevision = useRef(0);
  const inflight = useRef<Promise<boolean> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The round the editor shows, and whether a save found a newer one (Codex P1).
  const roundId = useRef<number | null>(null);
  const stale = useRef(false);

  const nextKey = () => `d${(counter.current += 1)}`;
  const isDirty = () => revision.current !== savedRevision.current;
  const setSummary = (text: string) => {
    summaryRef.current = text;
    setSummaryState(text);
  };
  const setItems = (next: LocalDraft[]) => {
    itemsRef.current = next;
    setItemsState(next);
  };
  // Each draft keeps its React key for as long as it exists (final I1): a re-read never
  // remounts a textarea, so focus and the cursor stay where the author left them.
  const keyById = useRef(new Map<number, string>());
  const keyFor = (draft: Draft): string => {
    if (draft.id === null || draft.id === undefined) return nextKey();
    let key = keyById.current.get(draft.id);
    if (key === undefined) {
      key = nextKey();
      keyById.current.set(draft.id, key);
    }
    return key;
  };
  const adopt = useCallback((d: DraftSet) => {
    const sameRound = roundId.current === null || roundId.current === d.round_id;
    if (!sameRound) keyById.current = new Map(); // a new round: nothing of the old one carries
    roundId.current = d.round_id;
    setSet(d);
    setSummary(d.summary);
    // A draft the author just added (not saved yet: no id) stays, after the server's list.
    const local = sameRound
      ? itemsRef.current.filter((i) => i.isNew && (i.draft.id === null || i.draft.id === undefined))
      : [];
    setItems([...d.drafts.map((draft) => ({ key: keyFor(draft), draft, isNew: false })), ...local]);
  }, []);

  useEffect(() => {
    if (!enabled || !active || isDirty()) return;
    const busy = inflight.current;
    if (busy) {
      // A save (or a blur's flush) is settling: read once it has.
      void busy.then(() => setAgain((n) => n + 1));
      return;
    }
    let current = true;
    const startedAt = revision.current;
    getDrafts(client, repo, number).then(
      (d) => {
        // Only an answer to the content as it still is replaces it: nothing typed since.
        if (!current || revision.current !== startedAt || isDirty() || inflight.current) {
          // Dropped, but the read may have seeded drafts (N2): any id never seen is the
          // server's addition, kept as a clean item so the next save keeps it.
          const unseen = (d.round_id === roundId.current ? d.drafts : []).filter(
            (x) => x.id !== null && x.id !== undefined && !keyById.current.has(x.id),
          );
          if (unseen.length) {
            setItems([...itemsRef.current, ...unseen.map((draft) => ({ key: keyFor(draft), draft, isNew: false }))]);
          }
          return;
        }
        adopt(d);
        setLoadError(null);
      },
      (e: unknown) => {
        if (current) setLoadError(errorCause(e));
      },
    );
    return () => {
      current = false;
    };
  }, [client, repo, number, enabled, active, refresh, again, adopt]);

  /** One PUT of the current content. */
  const saveOnce = useCallback(async (): Promise<boolean> => {
    const rev = revision.current;
    const kept = itemsRef.current.filter((i) => i.draft.body.trim() !== "");
    // Bound to the round shown (Codex P1): a newer round refuses it rather than taking it.
    const update: DraftsUpdate = {
      summary: summaryRef.current,
      drafts: kept.map((i) => toBody(i.draft)),
      round_id: roundId.current,
    };
    const action = `drafts:${JSON.stringify(update)}`;
    setStatus({ kind: "saving" });
    try {
      const saved = await putDrafts(client, repo, number, keys.current.keyFor(action), update);
      // Saved: keys kept for content whose outcome was unknown are stale now (the server holds
      // this content), so none can replay an older answer.
      keys.current = new ActionKeys();
      savedRevision.current = Math.max(savedRevision.current, rev);
      setSet(saved);
      // The answer lists the saved drafts in the order sent: they keep their keys, and take the
      // server's ids and placement. Their text is taken only if nothing was typed meanwhile.
      const answer = new Map(kept.map((item, i) => [item.key, saved.drafts[i]]));
      const untouched = revision.current === rev;
      if (untouched) setSummary(saved.summary);
      const merged = itemsRef.current.map((item) => {
        const server = answer.get(item.key);
        if (!server) return item;
        if (server.id !== null && server.id !== undefined) keyById.current.set(server.id, item.key);
        const draft = untouched ? server : { ...item.draft, id: server.id ?? null, in_body: server.in_body ?? null };
        return { ...item, draft, isNew: false };
      });
      // Drafts the server added to this save (findings seeded just now, final I2) are shown too.
      const added = saved.drafts.slice(kept.length).map((draft) => ({ key: keyFor(draft), draft, isNew: false }));
      setItems([...merged, ...added]);
      setStatus({ kind: "saved" });
      return true;
    } catch (e) {
      keys.current.settle(action, e);
      if (e instanceof ReviewRequestError && e.code === "RoundChanged") {
        // The author's text stays here, unsaved, until they copy it or discard it.
        stale.current = true;
        setStatus({ kind: "stale" });
        return false;
      }
      let message = `Couldn't save the drafts: ${errorCause(e)}`;
      let invalid: string | null = null;
      if (e instanceof ReviewRequestError && e.code === "InvalidDraftContent") {
        const index = e.facts.draftIndex;
        const item = index === null ? undefined : kept[index];
        if (item && index !== null) {
          invalid = item.key;
          message = `Draft ${index + 1} (${draftLocation(item.draft)}) can't be saved: ${errorCause(e)}`;
        } else {
          invalid = "summary";
          message = `The summary can't be saved: ${errorCause(e)}`;
        }
      }
      setStatus({ kind: "error", message, invalid });
      return false;
    }
  }, [client, repo, number]);

  /** Save until the latest content is saved, after any save already running (never two at once). */
  const flush = useCallback((): Promise<boolean> => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const run = async (): Promise<boolean> => {
      if (stale.current) return false; // never save an old round's text into a new one
      while (isDirty()) {
        if (!(await saveOnce())) return false;
      }
      return true;
    };
    const next = (inflight.current ?? Promise.resolve(true)).then(run, run);
    inflight.current = next;
    void next.then((ok) => {
      if (inflight.current !== next) return;
      inflight.current = null;
      setUnsaved(!ok || isDirty());
    });
    return next;
  }, [saveOnce]);

  const edited = () => {
    revision.current += 1;
    setUnsaved(true);
    if (timer.current) clearTimeout(timer.current);
    if (stale.current) return;
    timer.current = setTimeout(() => void flush(), AUTOSAVE_MS);
  };

  // Hidden (another app, a closed lid) or gone: save now. Leaving with unsaved text asks first.
  const latestFlush = useRef(flush);
  latestFlush.current = flush;
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden" && isDirty()) void latestFlush.current();
    };
    document.addEventListener("visibilitychange", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      if (isDirty()) void latestFlush.current();
    };
  }, []);
  useEffect(() => {
    if (!unsaved) return;
    const guard = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // older browsers ask only when it is set
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [unsaved]);

  return {
    set,
    loadError,
    summary,
    items,
    status,
    focusKey,
    unsaved,
    clearFocus: () => setFocusKey(null),
    reload: () => setAgain((n) => n + 1),
    /** Drops the old round's unsaved edits and reads the current round's drafts. */
    discard: () => {
      stale.current = false;
      savedRevision.current = revision.current;
      roundId.current = null;
      keyById.current = new Map();
      setItems([]);
      setUnsaved(false);
      setStatus({ kind: "idle" });
      setAgain((n) => n + 1);
    },
    editSummary: (text: string) => {
      setSummary(text);
      edited();
    },
    editBody: (key: string, body: string) => {
      setItems(itemsRef.current.map((i) => (i.key === key ? { ...i, draft: { ...i.draft, body } } : i)));
      edited();
    },
    /** Saves now (on blur, before a preview): resolves once the latest content is saved. */
    commit: flush,
    save: flush,
    remove: async (key: string) => {
      setItems(itemsRef.current.filter((i) => i.key !== key));
      revision.current += 1;
      setUnsaved(true);
      await flush();
    },
    /** A new draft of the author's own, anchored on a finding's lines. */
    addOn: (f: Finding) => {
      const key = nextKey();
      const range = f.start_line < f.end_line;
      const draft: Draft = {
        path: f.path,
        body: "",
        line: f.end_line,
        side: "RIGHT",
        start_line: range ? f.start_line : null,
        start_side: range ? "RIGHT" : null,
        origin_finding: null,
        outdated: false,
        outside_diff: false,
      };
      setItems([...itemsRef.current, { key, draft, isNew: true }]);
      setFocusKey(key);
    },
  };
}

export function DraftsEditor({ drafts }: { drafts: DraftsState }) {
  const { set, items, status, focusKey, clearFocus } = drafts;
  const boxes = useRef(new Map<string, HTMLTextAreaElement>());
  useEffect(() => {
    if (focusKey === null) return;
    const box = boxes.current.get(focusKey);
    if (!box) return;
    box.focus();
    clearFocus();
  });
  const invalid = status.kind === "error" ? status.invalid : null;
  return (
    <section className={s.drafts} aria-label="Drafts">
      {drafts.loadError ? (
        <div className={s.problem} role="alert">
          <span>{`Couldn't read the drafts: ${drafts.loadError}`}</span>
          <Button size="sm" variant="secondary" onClick={drafts.reload}>
            Read the drafts again
          </Button>
        </div>
      ) : null}
      {set?.findings_pending ? (
        <p className={s.muted} role="status">
          Findings are still running: their drafts are added next to yours when they finish.
        </p>
      ) : null}
      {set?.files_pending ? (
        <p className={s.muted}>
          GitHub's file list for this head isn't cached yet, so where each draft posts (inline or in the review body) is
          shown once it is.
        </p>
      ) : null}
      <label className={s.group}>
        <span className={s.label}>Review summary</span>
        <textarea
          className={s.textarea}
          value={drafts.summary}
          aria-invalid={invalid === "summary" || undefined}
          onChange={(e) => drafts.editSummary(e.target.value)}
          onBlur={() => void drafts.commit()}
        />
      </label>
      {items.length === 0 ? <p className={s.muted}>No draft comments yet.</p> : null}
      {items.map((item) => {
        const where = draftLocation(item.draft);
        const placed = item.draft.in_body;
        return (
          <div key={item.key} className={s.draft}>
            <label className={s.group}>
              <span className={s.label}>
                <span className={s.loc}>
                  <Visible text={where} />
                </span>
                {item.isNew ? " (new)" : null}
              </span>
              <textarea
                ref={(el) => {
                  if (el) boxes.current.set(item.key, el);
                  else boxes.current.delete(item.key);
                }}
                className={s.textarea}
                aria-label={`Comment on ${visibleText(where)}${item.isNew ? " (new)" : ""}`}
                aria-invalid={invalid === item.key || undefined}
                value={item.draft.body}
                onChange={(e) => drafts.editBody(item.key, e.target.value)}
                onBlur={() => void drafts.commit()}
              />
            </label>
            <div className={s.row}>
              {item.draft.outdated ? (
                <Badge tone="warn" icon="!">
                  Outdated
                </Badge>
              ) : null}
              {placed === true ? (
                <span className={s.muted}>Posts in the review body: GitHub can't anchor it inline.</span>
              ) : placed === false ? (
                <span className={s.muted}>Posts inline.</span>
              ) : null}
              {item.draft.origin_finding ? <span className={s.muted}>From a finding.</span> : null}
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Delete comment on ${visibleText(where)}`}
                onClick={() => void drafts.remove(item.key)}
              >
                Delete
              </Button>
            </div>
          </div>
        );
      })}
      <div className={s.row} aria-live="polite">
        {status.kind === "saving" ? <span className={s.muted}>Saving…</span> : null}
        {status.kind === "saved" ? <span className={s.muted}>Drafts saved.</span> : null}
        {status.kind === "stale" ? (
          <>
            <span role="alert">A new review round started; your edits are kept here — copy them or discard.</span>
            <Button size="sm" variant="secondary" onClick={drafts.discard}>
              Discard my edits
            </Button>
          </>
        ) : null}
        {status.kind === "error" ? (
          <>
            <span role="alert">{status.message}</span>
            <Button size="sm" variant="secondary" onClick={() => void drafts.save()}>
              Save again
            </Button>
          </>
        ) : null}
      </div>
    </section>
  );
}
