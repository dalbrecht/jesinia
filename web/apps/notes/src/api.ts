// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes and calls the Notes PWA uses (the KB routes are shared, spec ADR 0008 §5).
import { errorText, type JesiniaClient } from "@jesinia/client";

export type Page = {
  id: string;
  title: string;
  path: string;
  body: string;
  jesinia: Record<string, unknown>;
  foreign: Record<string, unknown>;
  rev: string;
};
export type TreeNode = {
  path: string;
  kind: "folder" | "page";
  title?: string | null;
  id?: string | null;
  pages: number;
  children: TreeNode[];
};
export type Hit = { source: string; key: string; title: string; snippet: string; path: string | null };

export type SaveResult =
  | { ok: true; page: Page }
  | { ok: false; conflict: { rev: string; body: string } }
  | { ok: false; error: string };

export async function getPage(client: JesiniaClient, id: string): Promise<Page | null> {
  const r = await client.GET("/api/kb/pages/{id}", { params: { path: { id } } });
  return (r.data as Page | undefined) ?? null;
}

export async function saveBody(client: JesiniaClient, id: string, body: string, rev: string): Promise<SaveResult> {
  const r = await client.PATCH("/api/kb/pages/{id}/body", {
    params: { path: { id } },
    body: { body, expected_rev: rev },
  });
  if (r.response.status === 409) {
    const c = (r as { error?: unknown }).error as { rev: string; body: string };
    return { ok: false, conflict: c };
  }
  if (!r.response.ok) return { ok: false, error: `Couldn't save (${r.response.status}).` };
  return { ok: true, page: r.data as Page };
}

export async function search(client: JesiniaClient, q: string, signal?: AbortSignal): Promise<Hit[]> {
  const r = await client.GET("/api/search", {
    params: { query: { q, source: ["kb"], limit: 30 } },
    ...(signal ? { signal } : {}),
  });
  // A failed search must not read as "no matches"; the caller shows the message.
  if (!r.response.ok) throw new Error(errorText(r.error, `HTTP ${r.response.status}`));
  return (r.data as Hit[] | undefined) ?? [];
}

export async function tree(client: JesiniaClient): Promise<TreeNode[]> {
  const r = await client.GET("/api/kb/tree");
  return (r.data as TreeNode[] | undefined) ?? [];
}

/** `/notes/page/<id>` → id. */
export function pageFromPath(path: string): string | null {
  const m = /^\/notes\/page\/([^/]+)/.exec(path);
  return m?.[1] ? decodeURIComponent(m[1]) : null;
}
