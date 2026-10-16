// SPDX-License-Identifier: AGPL-3.0-or-later
// The open page: load, autosave (~1 s idle, blur, page switch) and the changed-on-disk flow.
import { subscribe } from "@jesinia/client";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState } from "react";
import { getPage, saveBody, type Page } from "./api";

export const AUTOSAVE_MS = 1000;
export type SaveState = "saved" | "dirty" | "saving" | "conflict" | "error";

export type OpenPage = {
  page: Page | null;
  missing: boolean;
  text: string;
  state: SaveState;
  error: string | null;
  /** Bumped when the editor text is replaced from outside (Use theirs, reload). */
  revision: number;
  edit: (text: string) => void;
  flush: (resolving?: boolean) => Promise<void>;
  keepMine: () => Promise<void>;
  useTheirs: () => void;
  /** The on-disk text that conflicts with the local edit (null when no conflict). */
  theirs: string | null;
  mine: () => string;
};

export function usePage(id: string | null): OpenPage {
  const client = useClient();
  const [page, setPage] = useState<Page | null>(null);
  const [missing, setMissing] = useState(false);
  const [state, setState] = useState<SaveState>("saved");
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [theirs, setTheirs] = useState<{ rev: string; body: string } | null>(null);
  const text = useRef("");
  const rev = useRef("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dirty = useRef(false);
  const conflicted = useRef(false); // while open, only "Keep mine" may write
  const saving = useRef(false);
  const openGeneration = useRef(0); // bumped on every page switch
  const [, force] = useState(0);

  const flush = useCallback(
    async (resolving = false) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      if (!id || !dirty.current || (conflicted.current && !resolving)) return;
      const generation = openGeneration.current;
      setState("saving");
      saving.current = true;
      const sent = text.current;
      const r = await saveBody(client, id, sent, rev.current).finally(() => {
        saving.current = false;
      });
      // The user switched pages while this save was in flight: it still landed, but it
      // must not touch the newly opened page's state.
      if (generation !== openGeneration.current) return;
      if (r.ok) {
        rev.current = r.page.rev;
        setPage(r.page);
        dirty.current = text.current !== sent;
        setState(dirty.current ? "dirty" : "saved");
        setError(null);
      } else if ("conflict" in r) {
        conflicted.current = true;
        setTheirs(r.conflict);
        setState("conflict");
      } else {
        setError(r.error);
        setState("error");
      }
    },
    [client, id],
  );

  // Load (and save the previous page on switch).
  useEffect(() => {
    let live = true;
    openGeneration.current += 1;
    setPage(null);
    setMissing(false);
    setTheirs(null);
    setState("saved");
    dirty.current = false;
    conflicted.current = false;
    if (!id) return;
    void getPage(client, id).then((p) => {
      if (!live) return;
      if (!p) {
        setMissing(true);
        return;
      }
      text.current = p.body;
      rev.current = p.rev;
      setPage(p);
    });
    return () => {
      live = false;
      void flush();
    };
  }, [client, id, flush]);

  // Someone else changed this page on disk.
  useEffect(() => {
    if (!id) return;
    return subscribe<{ id: string; origin: string; change: string }>(
      "/api/events?topics=kb.page_changed",
      (e) => {
        // Any writer counts (an approved agent write, another window, an editor); only
        // our own acknowledged save matches rev.current and is ignored below.
        if (e.data.id !== id || saving.current) return;
        void getPage(client, id).then((p) => {
          if (!p || p.rev === rev.current || saving.current) return;
          if (dirty.current) {
            conflicted.current = true;
            setTheirs({ rev: p.rev, body: p.body });
            setState("conflict");
          } else {
            text.current = p.body;
            rev.current = p.rev;
            setPage(p);
            setRevision((n) => n + 1);
          }
        });
      },
      { events: ["kb.page_changed"] },
    );
  }, [client, id]);

  const edit = useCallback(
    (t: string) => {
      text.current = t;
      dirty.current = true;
      setState((s) => (s === "conflict" ? s : "dirty"));
      force((n) => n + 1);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        if (dirty.current) void flush();
      }, AUTOSAVE_MS);
    },
    [flush],
  );

  const keepMine = useCallback(async () => {
    if (!theirs) return;
    rev.current = theirs.rev;
    setTheirs(null);
    conflicted.current = false;
    dirty.current = true;
    await flush(true);
  }, [theirs, flush]);

  const mine = useCallback(() => text.current, []);

  const useTheirs = useCallback(() => {
    if (!theirs) return;
    text.current = theirs.body;
    rev.current = theirs.rev;
    dirty.current = false;
    conflicted.current = false;
    setTheirs(null);
    setState("saved");
    setRevision((n) => n + 1);
  }, [theirs]);

  return {
    page,
    missing,
    text: text.current,
    state,
    error,
    revision,
    theirs: theirs?.body ?? null,
    mine,
    edit,
    flush,
    keepMine,
    useTheirs,
  };
}
