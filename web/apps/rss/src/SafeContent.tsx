// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed content on screen (ruling R17). The server sanitises `content_html`, but it is still
// feed-supplied, so it never reaches the DOM as HTML: it is parsed off-document and rebuilt as
// React elements through an allowlist. Every href must be http(s) and every image https
// (anything else is dropped, an http: image shown as its alt text). Markdown (a full article,
// an agent's reply) uses `SafeMarkdown` from the shell.
import { IMG_PROPS, LINK_PROPS, resolveHttpUrl as resolve } from "@jesinia/shell";
import { createElement, useMemo, type ReactNode } from "react";

const ALLOWED = new Set([
  "p", "a", "em", "strong", "b", "i", "code", "pre", "blockquote", "ul", "ol", "li",
  "h1", "h2", "h3", "h4", "h5", "h6", "img", "figure", "figcaption",
  "table", "thead", "tbody", "tr", "th", "td", "br", "hr",
]); // prettier-ignore
/** Dropped with everything inside them: their text is code or chrome, never reading. */
const DROPPED = new Set([
  "script",
  "style",
  "template",
  "iframe",
  "object",
  "embed",
  "noscript",
  "svg",
  "math",
  "head",
]);
const VOID = new Set(["br", "hr", "img"]);
/** Elements whose whitespace text is layout, not content (React rejects it inside them). */
const NO_TEXT = new Set(["table", "thead", "tbody", "tr", "ul", "ol"]);

function convert(node: Node, base: string, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (!(node instanceof Element)) return null; // comments, processing instructions
  const tag = node.localName;
  if (DROPPED.has(tag)) return null;
  const children = [...node.childNodes]
    .filter((c) => !(NO_TEXT.has(tag) && c.nodeType === Node.TEXT_NODE && !c.textContent?.trim()))
    .map((c, i) => convert(c, base, `${key}.${i}`));
  // An unknown wrapper (div, span, font, …) gives way to its content.
  if (!ALLOWED.has(tag)) return children;
  if (tag === "a") {
    const href = resolve(node.getAttribute("href"), base);
    return href === null ? children : createElement("a", { key, href, ...LINK_PROPS }, ...children);
  }
  if (tag === "img") {
    const src = resolve(node.getAttribute("src"), base);
    if (src === null) return null;
    const alt = node.getAttribute("alt") ?? "";
    // The reader's CSP loads https: images only: an http: one would only show as broken.
    if (!src.startsWith("https:")) return alt;
    return createElement("img", { key, src, alt, ...IMG_PROPS });
  }
  if (VOID.has(tag)) return createElement(tag, { key });
  return createElement(tag, { key }, ...children);
}

/** Feed HTML as React elements; `base` is the post's URL, for relative links and images. */
export function SafeHtml({ html, base }: { html: string; base: string }) {
  // DOMParser never runs scripts or loads resources in the parsed document. Parsed once per
  // post: the reader re-renders on every read toggle.
  const nodes = useMemo(() => {
    const doc = new DOMParser().parseFromString(html, "text/html");
    return [...doc.body.childNodes].map((n, i) => convert(n, base, String(i)));
  }, [html, base]);
  return <>{nodes}</>;
}
