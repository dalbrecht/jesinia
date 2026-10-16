// SPDX-License-Identifier: AGPL-3.0-or-later
import type { JesiniaClient } from "@jesinia/client";
import { Banner, Button } from "@jesinia/design";
import { AppShell, useClient, type ServerStatus } from "@jesinia/shell";
import { useCallback, useEffect, useState } from "react";
import { pageFromPath, search } from "./api";
import { lineDiff } from "./events";
import { Chat } from "./Chat";
import { Editor } from "./Editor";
import s from "./notes.module.css";
import { Sidebar } from "./Sidebar";
import { usePage } from "./usePage";

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
  const findPages = useCallback(
    async (q: string) => (await search(client, q)).map((h) => ({ id: h.key, title: h.title })),
    [client],
  );

  return (
    <div className={s.layout}>
      <Sidebar current={id} open={open} refreshKey={treeKey} />
      <section className={s.main} aria-label="Page">
        {doc.page ? (
          <>
            <header className={s.pageHead}>
              <h1 className={s.title}>{doc.page.title}</h1>
              <span className={s.muted} data-testid="save-state">
                {STATE_LABEL[doc.state]}
              </span>
            </header>
            <p className={s.properties}>
              <span className={s.mono}>{doc.page.path}</span> · <span className={s.mono}>id: {doc.page.id}</span>
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
              <Editor
                key={doc.page.id}
                value={doc.text}
                revision={doc.revision}
                label={`Edit ${doc.page.title}`}
                onChange={doc.edit}
                onBlur={() => void doc.flush()}
                findPages={findPages}
              />
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
      {doc.page ? (
        <Chat
          page={{ id: doc.page.id, title: doc.page.title }}
          runners={status.runners}
          onSaved={() => setTreeKey((k) => k + 1)}
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
