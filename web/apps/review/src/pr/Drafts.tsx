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
import { lines, type Finding } from "./model";
import s from "./pr.module.css";

export type LocalDraft = { key: string; draft: Draft; isNew: boolean };
type Status =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string; invalid: string | null };

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
};

export function useDrafts({ client, repo, number, enabled, refresh }: UseDraftsArgs) {
  const keys = useRef(new ActionKeys());
  const counter = useRef(0);
  const [set, setSet] = useState<DraftSet | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [summary, setSummaryState] = useState("");
  const [items, setItemsState] = useState<LocalDraft[]>([]);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [again, setAgain] = useState(0);
  const summaryRef = useRef("");
  const itemsRef = useRef<LocalDraft[]>([]);
  const dirty = useRef(false);

  const nextKey = () => `d${(counter.current += 1)}`;
  const setSummary = (text: string) => {
    summaryRef.current = text;
    setSummaryState(text);
  };
  const setItems = (next: LocalDraft[]) => {
    itemsRef.current = next;
    setItemsState(next);
  };
  const adopt = useCallback((d: DraftSet, keepLocal: LocalDraft[] = []) => {
    setSet(d);
    summaryRef.current = d.summary;
    setSummaryState(d.summary);
    const next = [...d.drafts.map((draft) => ({ key: nextKey(), draft, isNew: false })), ...keepLocal];
    itemsRef.current = next;
    setItemsState(next);
  }, []);

  useEffect(() => {
    if (!enabled || dirty.current) return;
    let current = true;
    getDrafts(client, repo, number).then(
      (d) => {
        if (!current || dirty.current) return;
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
  }, [client, repo, number, enabled, refresh, again, adopt]);

  const save = useCallback(async (): Promise<boolean> => {
    const kept = itemsRef.current.filter((i) => i.draft.body.trim() !== "");
    const unsaved = itemsRef.current.filter((i) => i.draft.body.trim() === "");
    const update: DraftsUpdate = { summary: summaryRef.current, drafts: kept.map((i) => toBody(i.draft)) };
    const action = `drafts:${JSON.stringify(update)}`;
    setStatus({ kind: "saving" });
    try {
      const saved = await putDrafts(client, repo, number, keys.current.keyFor(action), update);
      keys.current.settle(action);
      dirty.current = false;
      adopt(saved, unsaved);
      setStatus({ kind: "saved" });
      return true;
    } catch (e) {
      keys.current.settle(action, e);
      let message = `Couldn't save the drafts: ${errorCause(e)}`;
      let invalid: string | null = null;
      if (e instanceof ReviewRequestError && e.code === "InvalidDraftContent") {
        const which = /draft (\d+)/.exec(e.message);
        const item = which ? kept[Number(which[1]) - 1] : undefined;
        if (item && which) {
          invalid = item.key;
          message = `Draft ${which[1]} (${draftLocation(item.draft)}) can't be saved: ${errorCause(e)}`;
        } else {
          invalid = "summary";
          message = `The summary can't be saved: ${errorCause(e)}`;
        }
      }
      setStatus({ kind: "error", message, invalid });
      return false;
    }
  }, [client, repo, number, adopt]);

  return {
    set,
    loadError,
    summary,
    items,
    status,
    focusKey,
    clearFocus: () => setFocusKey(null),
    reload: () => setAgain((n) => n + 1),
    editSummary: (text: string) => {
      dirty.current = true;
      setSummary(text);
    },
    editBody: (key: string, body: string) => {
      dirty.current = true;
      setItems(itemsRef.current.map((i) => (i.key === key ? { ...i, draft: { ...i.draft, body } } : i)));
    },
    /** Saves after an edit (on blur): nothing to do when nothing changed. */
    commit: async () => (dirty.current ? save() : true),
    save,
    remove: async (key: string) => {
      dirty.current = true;
      setItems(itemsRef.current.filter((i) => i.key !== key));
      await save();
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
                <span className={s.loc}>{where}</span>
                {item.isNew ? " (new)" : null}
              </span>
              <textarea
                ref={(el) => {
                  if (el) boxes.current.set(item.key, el);
                  else boxes.current.delete(item.key);
                }}
                className={s.textarea}
                aria-label={`Comment on ${where}${item.isNew ? " (new)" : ""}`}
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
                aria-label={`Delete comment on ${where}`}
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
