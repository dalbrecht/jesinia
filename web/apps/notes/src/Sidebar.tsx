// SPDX-License-Identifier: AGPL-3.0-or-later
import { Input } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useEffect, useState } from "react";
import { search, tree, type Hit, type TreeNode } from "./api";
import s from "./notes.module.css";

export const SEARCH_DEBOUNCE_MS = 200;

const ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#x27;": "'",
  "&#39;": "'",
};

/** Undo the server's html.escape (the only markup left after <mark> is split out). */
function plain(snippet: string): string {
  return snippet.replace(/<\/?mark>/g, "").replace(/&(amp|lt|gt|quot|#x27|#39);/g, (e) => ENTITIES[e] ?? e);
}

/** Snippet HTML from the server is escaped text plus <mark>; render only those marks. */
function Snippet({ html }: { html: string }) {
  const parts = html.split(/(<mark>.*?<\/mark>)/g);
  return (
    <span className={s.snippet}>
      {parts.map((p, i) =>
        p.startsWith("<mark>") ? <mark key={i}>{plain(p)}</mark> : <span key={i}>{plain(p)}</span>,
      )}
    </span>
  );
}

function Tree({ nodes, current, open }: { nodes: TreeNode[]; current: string | null; open: (id: string) => void }) {
  return (
    <ul className={s.tree}>
      {nodes.map((n) =>
        n.kind === "folder" ? (
          <li key={n.path}>
            <details open>
              <summary>
                {n.path.split("/").pop()} <span className={s.count}>{n.pages}</span>
              </summary>
              <Tree nodes={n.children} current={current} open={open} />
            </details>
          </li>
        ) : (
          <li key={n.path}>
            <button
              type="button"
              className={s.treePage}
              aria-current={n.id === current ? "page" : undefined}
              onClick={() => n.id && open(n.id)}
            >
              {n.title ?? n.path}
            </button>
          </li>
        ),
      )}
    </ul>
  );
}

export function Sidebar({
  current,
  open,
  refreshKey,
}: {
  current: string | null;
  open: (id: string) => void;
  refreshKey: number;
}) {
  const client = useClient();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [nodes, setNodes] = useState<TreeNode[]>([]);

  useEffect(() => {
    void tree(client).then(setNodes);
  }, [client, refreshKey]);

  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setHits(null);
      return;
    }
    const ctl = new AbortController();
    const t = setTimeout(() => {
      void search(client, query, ctl.signal).then((h) => !ctl.signal.aborted && setHits(h));
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
      ctl.abort();
    };
  }, [client, q]);

  const needle = q.trim().toLowerCase();
  const titled = hits?.filter((h) => h.title.toLowerCase().includes(needle)) ?? [];
  const content = hits?.filter((h) => !h.title.toLowerCase().includes(needle)) ?? [];
  const group = (label: string, list: Hit[]) =>
    list.length ? (
      <section aria-label={label}>
        <h3 className={s.groupLabel}>{label}</h3>
        <ul className={s.results}>
          {list.map((h) => (
            <li key={h.key}>
              <button type="button" className={s.result} onClick={() => open(h.key)}>
                <span className={s.resultTitle}>{h.title}</span>
                <span className={s.resultPath}>{h.path}</span>
                <Snippet html={h.snippet} />
              </button>
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  return (
    <aside className={s.sidebar} aria-label="Pages">
      <Input
        label="Search notes"
        hideLabel
        variant="search"
        placeholder="Search the KB"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {hits === null ? (
        <Tree nodes={nodes} current={current} open={open} />
      ) : hits.length === 0 ? (
        <p className={s.muted}>No pages match “{q.trim()}”.</p>
      ) : (
        <div data-testid="search-results">
          {group("Titles", titled)}
          {group("Content", content)}
        </div>
      )}
    </aside>
  );
}
