// SPDX-License-Identifier: AGPL-3.0-or-later
// The Read view (ruling R-REN2): a KB page rendered through the shell's safe Markdown renderer
// (raw HTML skipped, never innerHTML). Before it renders, the kb-format extras are rewritten in
// the Markdown tree: `[[id|Title]]` links open pages in Notes, `../_assets/<id>/<file>` images
// load from `/api/kb/assets`, `<!-- jesinia:ai … -->` blocks become a labelled AI callout (the
// renderer would drop the comments), and `#tags` become chips.
import { PAGE_LINK_SCHEME, SafeMarkdown, type MdNode, type PageLinks } from "@jesinia/shell";
import { useMemo } from "react";
import s from "./notes.module.css";

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const PAGE_ID = new RegExp(`^${UUID}$`);
const ASSET_FILE = /^[A-Za-z0-9][A-Za-z0-9_-]*(?:$|\.[A-Za-z0-9]+$)/;
const PAGE_LINK = new RegExp(`\\[\\[(${UUID})(?:\\|([^\\]]*))?\\]\\]`, "g");
// kb-format's tag grammar: `#` after no word character, `/`, `#`, `?`, `=`, `&` or `\`, and a
// tag that doesn't run on into `_` or another word character.
const TAG =
  /(?<![\p{L}\p{N}\p{M}_/#?=&\\])#[\p{L}\p{N}](?:[\p{L}\p{N}\p{M}]|-(?=[\p{L}\p{N}]))*(?!-?[\p{L}\p{N}\p{M}_])/gu;
// A front-matter block: `---`, then a `key:` line, up to the closing `---`.
const FRONT_MATTER = /^---\r?\n(?=["']?[A-Za-z_][^\n]*:)[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/;
const AI_OPEN = /^<!-- jesinia:ai\b(.*?)-->\s*$/;
const AI_CLOSE = /^<!-- \/jesinia:ai -->\s*$/;
// An escaped `\#` is not a tag (kb-format), but the Markdown parser drops the backslash. It is
// swapped for this private-use character first, then restored: `#` in text, `\#` in code.
const ESCAPED_HASH = "";
const ESCAPED_HASH_ALL = new RegExp(ESCAPED_HASH, "g");

/** The page body as the Read view parses it: no front matter, escaped `\#` marked. */
export function prepareMarkdown(body: string): string {
  return body
    .replace(FRONT_MATTER, "")
    .replace(/\\([\\#])/g, (escape, c: string) => (c === "#" ? ESCAPED_HASH : escape));
}

/** The open page, as the Read view needs it. */
export type ReadPage = { id: string; path: string; title: string };

/**
 * `src` (an image in `page`) as its `/api/kb/assets/<id>/<file>` URL when, resolved against the
 * page's path, it is exactly one file in that page's own asset folder; else null.
 */
export function kbAssetUrl(src: string, page: Pick<ReadPage, "id" | "path">): string | null {
  if (/^[a-z][a-z0-9+.-]*:|^\/|[%?#\\]/i.test(src)) return null;
  const parts = page.path.split("/").slice(0, -1);
  for (const segment of src.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (parts.length === 0) return null;
      parts.pop();
    } else parts.push(segment);
  }
  const [folder, id, file, ...rest] = parts;
  if (
    folder !== "_assets" ||
    rest.length > 0 ||
    id !== page.id ||
    !file ||
    !PAGE_ID.test(id) ||
    !ASSET_FILE.test(file)
  ) {
    return null;
  }
  return `/api/kb/assets/${id}/${file}`;
}

type Classes = { tag: string | undefined; ai: string | undefined; aiLabel: string | undefined };

const text = (value: string): MdNode => ({ type: "text", value });
const restore = (value: string, as = "#") => value.replace(ESCAPED_HASH_ALL, as);

/** Text with its tags as chips (`#` restored where it was escaped). */
function tagged(value: string, classes: Classes): MdNode[] {
  const out: MdNode[] = [];
  let at = 0;
  for (const m of value.matchAll(TAG)) {
    if (m.index > at) out.push(text(restore(value.slice(at, m.index))));
    out.push({
      type: "text",
      value: m[0],
      data: { hName: "span", hProperties: { className: classes.tag, dataTag: m[0].slice(1).toLowerCase() } },
    });
    at = m.index + m[0].length;
  }
  if (at < value.length) out.push(text(restore(value.slice(at))));
  return out;
}

/** Text with `[[id|Title]]` and `[[id]]` as page links, and (outside headings) tags as chips. */
function linked(value: string, classes: Classes, tags: boolean): MdNode[] {
  const plain = (v: string) => (tags ? tagged(v, classes) : [text(restore(v))]);
  const out: MdNode[] = [];
  let at = 0;
  for (const m of value.matchAll(PAGE_LINK)) {
    if (m.index > at) out.push(...plain(value.slice(at, m.index)));
    const id = m[0].slice(2, 2 + 36); // `[[` and a UUID
    out.push({ type: "link", url: `${PAGE_LINK_SCHEME}${id}`, children: [text(restore(m[2]?.trim() || id))] });
    at = m.index + m[0].length;
  }
  if (at < value.length) out.push(...plain(value.slice(at)));
  return out;
}

/** The AI callout for a `<!-- jesinia:ai … -->` marker and the nodes it wraps. */
function aiBlock(marker: string, children: MdNode[], classes: Classes): MdNode {
  const attrs: Record<string, string | undefined> = Object.fromEntries(
    [...marker.matchAll(/(\w+)=(\S+)/g)].map(([, key, value]) => [key, value]),
  );
  const label = ["AI", attrs.agent ?? "agent", ...(attrs.model ? [attrs.model] : [])].join(" · ");
  return {
    type: "aiBlock",
    data: { hName: "aside", hProperties: { className: classes.ai, role: "note", ariaLabel: "Written by an agent" } },
    children: [
      { type: "paragraph", data: { hProperties: { className: classes.aiLabel } }, children: [text(label)] },
      ...children,
    ],
  };
}

/** `children` with each provenance block (open marker … close marker) grouped as a callout. */
function groupAi(children: MdNode[], classes: Classes): MdNode[] {
  const out: MdNode[] = [];
  let open: { marker: string; nodes: MdNode[] } | null = null;
  for (const node of children) {
    const html = node.type === "html" ? String(node.value) : null;
    if (html !== null && AI_OPEN.test(html)) {
      if (open) out.push(aiBlock(open.marker, open.nodes, classes));
      open = { marker: html, nodes: [] };
    } else if (html !== null && AI_CLOSE.test(html)) {
      if (open) out.push(aiBlock(open.marker, open.nodes, classes));
      open = null; // a close marker with no block open is dropped
    } else if (open) open.nodes.push(node);
    else out.push(node);
  }
  if (open) out.push(aiBlock(open.marker, open.nodes, classes)); // left open: to the end
  return out;
}

/** A heading's text, lowercased with whitespace collapsed (for comparing with a title). */
function headingKey(node: MdNode): string {
  const words = (n: MdNode): string => (n.children ? n.children.map(words).join("") : restore(n.value ?? ""));
  return words(node).replace(/\s+/g, " ").trim().toLowerCase();
}

const ARTICLE_HEADING = "article";

/**
 * `children` without the h1 that only repeats the page title: the body's first heading, or the
 * first heading after `## Article` (a kept post's article starts with its own title).
 */
function withoutRepeatedTitle(children: MdNode[], title: string): MdNode[] {
  const key = title.replace(/\s+/g, " ").trim().toLowerCase();
  const headings = children.filter((n) => n.type === "heading");
  const article = headings.findIndex((h) => h.depth === 2 && headingKey(h) === ARTICLE_HEADING);
  const candidates = [headings[0], article === -1 ? undefined : headings[article + 1]];
  const repeated = candidates.find((h) => h?.depth === 1 && headingKey(h) === key);
  return repeated ? children.filter((n) => n !== repeated) : children;
}

/** The Read view's tree rewrite for `page`. */
export function readTransform(page: ReadPage, classes: Classes): (tree: MdNode) => void {
  const visit = (node: MdNode, tags: boolean): void => {
    if (node.type === "code" || node.type === "inlineCode" || node.type === "html") {
      node.value = restore(String(node.value), "\\#");
      return;
    }
    if (node.url !== undefined) {
      const url = restore(node.url);
      node.url = node.type === "link" ? url : (kbAssetUrl(url, page) ?? url);
    }
    if (typeof node.alt === "string") node.alt = restore(node.alt);
    if (!node.children) return;
    // No tags in headings (kb-format), and no page links or chips inside a link.
    const inLink = node.type === "link" || node.type === "linkReference";
    const childTags = tags && node.type !== "heading";
    node.children = groupAi(node.children, classes).flatMap((child) => {
      if (child.type !== "text" || child.data) {
        visit(child, childTags);
        return [child];
      }
      const value = String(child.value);
      return inLink ? [text(restore(value))] : linked(value, classes, childTags);
    });
  };
  return (tree) => {
    tree.children = withoutRepeatedTitle(tree.children ?? [], page.title);
    visit(tree, true);
  };
}

/** A KB page body, rendered for reading. */
export function ReadView({
  markdown,
  page,
  onOpenPage,
}: {
  markdown: string;
  page: ReadPage;
  onOpenPage: (id: string) => void;
}) {
  const prepared = useMemo(() => prepareMarkdown(markdown), [markdown]);
  const { id, path, title } = page;
  const transform = useMemo(
    () => readTransform({ id, path, title }, { tag: s.tag, ai: s.ai, aiLabel: s.aiLabel }),
    [id, path, title],
  );
  const pageLinks = useMemo<PageLinks>(
    () => ({ href: (id) => `/notes/page/${encodeURIComponent(id)}`, open: onOpenPage }),
    [onOpenPage],
  );
  return (
    <article className={s.read} aria-label="Read view">
      <SafeMarkdown markdown={prepared} images="local" transform={transform} pageLinks={pageLinks} />
    </article>
  );
}

export type ViewMode = "read" | "edit";
/** localStorage key prefix; the page kind follows (`…note`, `…source/rss`). */
export const VIEW_KEY_PREFIX = "jesinia.notes.view:";

/** A page's kind (`jesinia.kind`), `note` when it has none. */
export function pageKind(jesinia: Record<string, unknown>): string {
  return typeof jesinia.kind === "string" && jesinia.kind ? jesinia.kind : "note";
}

/** Sources (kept posts and the like) open for reading; everything else opens for editing. */
export function defaultView(kind: string): ViewMode {
  return kind.startsWith("source/") ? "read" : "edit";
}

/** The view last chosen for pages of `kind`, else its default (storage may be unavailable). */
export function loadView(kind: string): ViewMode {
  try {
    const saved = localStorage.getItem(`${VIEW_KEY_PREFIX}${kind}`);
    if (saved === "read" || saved === "edit") return saved;
  } catch {
    // Private mode or blocked storage: the default is fine.
  }
  return defaultView(kind);
}

export function saveView(kind: string, mode: ViewMode): void {
  try {
    localStorage.setItem(`${VIEW_KEY_PREFIX}${kind}`, mode);
  } catch {
    // Not remembered this time; the toggle still works.
  }
}

const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform);

/** ⌘E on macOS, Ctrl+E elsewhere. */
export function isViewShortcut(e: KeyboardEvent): boolean {
  if (e.key.toLowerCase() !== "e" || e.shiftKey || e.altKey) return false;
  return isMac() ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

/** The view shortcut as this platform writes it. */
export function viewShortcutLabel(): string {
  return isMac() ? "⌘E" : "Ctrl+E";
}
