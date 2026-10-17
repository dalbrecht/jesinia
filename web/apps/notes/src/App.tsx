// SPDX-License-Identifier: AGPL-3.0-or-later
import type { JesiniaClient } from "@jesinia/client";
import { Banner, Button } from "@jesinia/design";
import { AppShell, lineDiff, useClient, type ServerStatus } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState } from "react";
import { pageFromPath, search, type Page } from "./api";
import { Chat } from "./Chat";
import { Editor } from "./Editor";
import s from "./notes.module.css";
import { isViewShortcut, loadView, pageKind, ReadView, saveView, viewShortcutLabel, type ViewMode } from "./read";
import { Sidebar } from "./Sidebar";
import { usePage } from "./usePage";

/** Under 1024 px the page gets the screen; the chat is a sheet opened from the page toolbar. */
export const CHAT_SHEET_QUERY = "(max-width: 1023px)";

function mediaList(query: string): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => mediaList(query)?.matches ?? false);
  useEffect(() => {
    const mql = mediaList(query);
    if (!mql) return;
    const onChange = () => setMatches(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

const STATE_LABEL = {
  saved: "Saved",
  dirty: "Editing…",
  saving: "Saving…",
  conflict: "Changed on disk",
  error: "Not saved",
};

function Workspace({ status }: { status: ServerStatus }) {
  const client = useClient();
  const [id, setId] = useState<string | null>(() => pageFromPath(window.location.pathname));
  const [treeKey, setTreeKey] = useState(0);
  const open = useCallback((next: string) => {
    window.history.pushState(null, "", `/notes/page/${encodeURIComponent(next)}`);
    setId(next);
  }, []);
  useEffect(() => {
    const onPop = () => setId(pageFromPath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const doc = usePage(id);
  const [comparing, setComparing] = useState(false);
  useEffect(() => {
    if (doc.state !== "conflict") setComparing(false);
  }, [doc.state]);
  // Read or Edit: the choice made for this page, else the one remembered for its kind.
  const [chosen, setChosen] = useState<{ id: string; mode: ViewMode } | null>(null);
  const page = doc.page;
  const view: ViewMode =
    page === null ? "edit" : chosen?.id === page.id ? chosen.mode : loadView(pageKind(page.jesinia));
  const { flush } = doc;
  const choose = useCallback(
    (target: Page, mode: ViewMode) => {
      // Read shows the current text, unsaved edits included; they are saved now as well.
      if (mode === "read") void flush();
      saveView(pageKind(target.jesinia), mode);
      setChosen({ id: target.id, mode });
    },
    [flush],
  );
  useEffect(() => {
    if (page === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (!isViewShortcut(e)) return;
      e.preventDefault();
      choose(page, view === "read" ? "edit" : "read");
    };
    // Capture: the editor must not take ⌘E/Ctrl+E for itself.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [choose, view, page]);
  const sheet = useMediaQuery(CHAT_SHEET_QUERY);
  // Per-session UI state: the sheet starts closed on every load.
  const [chatOpen, setChatOpen] = useState(false);
  const asker = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const closeChat = useCallback(() => {
    setChatOpen(false);
    asker.current?.focus();
  }, []);
  const findPages = useCallback(
    async (q: string) => (await search(client, q)).map((h) => ({ id: h.key, title: h.title })),
    [client],
  );

  return (
    <div className={s.layout}>
      <Sidebar current={id} open={open} refreshKey={treeKey} />
      <section className={s.main} aria-label="Page">
        {page ? (
          <>
            <header className={s.pageHead}>
              <h1 className={s.title}>{page.title}</h1>
              <div className={s.headTools}>
                <span className={s.muted} data-testid="save-state">
                  {STATE_LABEL[doc.state]}
                </span>
                <div role="group" aria-label="View" className={s.viewToggle}>
                  {(["read", "edit"] as const).map((mode) => (
                    <Button
                      key={mode}
                      size="sm"
                      variant={view === mode ? "secondary" : "ghost"}
                      aria-pressed={view === mode}
                      title={`${mode === "read" ? "Read" : "Edit"} (${viewShortcutLabel()})`}
                      onClick={() => choose(page, mode)}
                    >
                      {mode === "read" ? "Read" : "Edit"}
                    </Button>
                  ))}
                </div>
                {sheet ? (
                  <Button
                    ref={asker}
                    size="sm"
                    variant="secondary"
                    aria-expanded={chatOpen}
                    aria-controls="notes-chat"
                    onClick={() => setChatOpen(true)}
                  >
                    Ask about this page
                  </Button>
                ) : null}
              </div>
            </header>
            <p className={s.properties}>
              <span className={s.mono}>{page.path}</span> · <span className={s.mono}>id: {page.id}</span>
            </p>
            {doc.state === "conflict" ? (
              <>
                <Banner
                  tone="warn"
                  cause="Changed on disk:"
                  action={{ label: "Keep mine", onAction: () => void doc.keepMine() }}
                  secondaryAction={{ label: "Use theirs", onAction: doc.useTheirs }}
                >
                  this page changed elsewhere while you had unsaved edits.{" "}
                  <Button variant="ghost" size="sm" aria-expanded={comparing} onClick={() => setComparing(!comparing)}>
                    {comparing ? "Hide comparison" : "Compare"}
                  </Button>
                </Banner>
                {comparing && doc.theirs !== null ? (
                  <section aria-label="Comparison: on disk (−) and yours (+)" className={s.compare}>
                    <pre>{lineDiff(doc.theirs.split("\n"), doc.mine().split("\n"))}</pre>
                  </section>
                ) : null}
              </>
            ) : null}
            {doc.error ? <p className={s.error}>{doc.error}</p> : null}
            <div className={s.editor}>
              {view === "read" ? (
                <ReadView markdown={doc.text} page={page} onOpenPage={open} />
              ) : (
                <Editor
                  key={page.id}
                  value={doc.text}
                  revision={doc.revision}
                  label={`Edit ${page.title}`}
                  onChange={doc.edit}
                  onBlur={() => void doc.flush()}
                  findPages={findPages}
                />
              )}
            </div>
          </>
        ) : doc.missing ? (
          <p className={s.empty}>This page isn't in the KB any more.</p>
        ) : id ? (
          <p className={s.empty}>Loading…</p>
        ) : (
          <div className={s.empty}>
            <p>Open a page from the tree, or search the KB.</p>
            <Button href="/settings/" size="sm">
              KB settings
            </Button>
          </div>
        )}
      </section>
      {page ? (
        <Chat
          page={{ id: page.id, title: page.title }}
          runners={status.runners}
          onSaved={() => setTreeKey((k) => k + 1)}
          sheet={sheet}
          open={chatOpen}
          onClose={closeChat}
        />
      ) : null}
    </div>
  );
}

export function App({ client }: { client?: JesiniaClient }) {
  return (
    <AppShell
      app={{ id: "notes", name: "Notes", glyph: "/notes/icons/monochrome.svg" }}
      ownSearch
      {...(client ? { client } : {})}
    >
      {(status) => <Workspace status={status} />}
    </AppShell>
  );
}
