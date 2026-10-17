// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed content on screen (ruling R17). The server sanitises `content_html`, but it is still
// feed-supplied, so it never reaches the DOM as HTML: it is parsed off-document and rebuilt as
// React elements through an allowlist. Markdown goes through react-markdown with raw HTML
// skipped. Every href and src must be http(s); anything else is dropped.
import { createElement, useMemo, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import { safeHttpUrl } from "./api";

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

export const LINK_PROPS = { target: "_blank", rel: "noopener noreferrer nofollow" } as const;
const IMG_PROPS = { loading: "lazy", referrerPolicy: "no-referrer" } as const;

/** Only absolute http(s) URLs pass when there is no http(s) page to resolve against. */
function resolve(raw: string | null, base: string): string | null {
  if (raw === null) return null;
  const page = safeHttpUrl(base);
  return page === null ? safeHttpUrl(raw.trim()) : safeHttpUrl(raw.trim(), page);
}

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
    return createElement("img", { key, src, alt: node.getAttribute("alt") ?? "", ...IMG_PROPS });
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

/** react-markdown's hook: resolve against the post, and anything that isn't http(s) becomes ''
 * (then dropped below). */
export function httpOnly(url: string, base = ""): string {
  return resolve(url, base) ?? "";
}

const COMPONENTS: Components = {
  a: ({ href, children }) =>
    href ? (
      <a href={href} {...LINK_PROPS}>
        {children}
      </a>
    ) : (
      <>{children}</>
    ),
  img: ({ src, alt }) => (typeof src === "string" && src ? <img src={src} alt={alt ?? ""} {...IMG_PROPS} /> : null),
};

/** Markdown (a full article, an Engage reply) with raw HTML skipped and http(s) URLs only;
 * `base` (the post's URL) resolves relative links and images. */
export function SafeMarkdown({ markdown, base = "" }: { markdown: string; base?: string }) {
  return (
    <Markdown skipHtml urlTransform={(url) => httpOnly(url, base)} components={COMPONENTS}>
      {markdown}
    </Markdown>
  );
}

/** For `ChatTranscript`'s `renderMarkdown`. */
export function renderMarkdown(markdown: string): ReactNode {
  return <SafeMarkdown markdown={markdown} />;
}
