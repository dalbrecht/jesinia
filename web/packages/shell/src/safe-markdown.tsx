// SPDX-License-Identifier: AGPL-3.0-or-later
// Untrusted Markdown on screen (ruling R17): feed articles and agent replies. It goes through
// react-markdown with raw HTML skipped, never innerHTML, and every href and src must be http(s);
// anything else is dropped. Shared so every chat renders agent replies the same safe way.
import type { ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";

/** Links leave the app in a new tab and tell the target nothing. */
export const LINK_PROPS = { target: "_blank", rel: "noopener noreferrer nofollow" } as const;
/** Remote images load lazily and without a referrer. */
export const IMG_PROPS = { loading: "lazy", referrerPolicy: "no-referrer" } as const;

/** `raw` resolved against `base` when it is an http(s) URL, else null (feed and agent URLs are untrusted). */
export function safeHttpUrl(raw: string, base?: string): string | null {
  let url: URL;
  try {
    url = base === undefined ? new URL(raw) : new URL(raw, base);
  } catch {
    return null;
  }
  return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
}

/**
 * `raw` (an attribute value) as an http(s) URL, resolved against `base` when that is an http(s)
 * page; only absolute http(s) URLs pass when it isn't. Null when nothing safe is left.
 */
export function resolveHttpUrl(raw: string | null, base: string): string | null {
  if (raw === null) return null;
  const page = safeHttpUrl(base);
  return page === null ? safeHttpUrl(raw.trim()) : safeHttpUrl(raw.trim(), page);
}

/** react-markdown's URL hook: anything that isn't http(s) becomes '' (then dropped below). */
export function httpOnly(url: string, base = ""): string {
  return resolveHttpUrl(url, base) ?? "";
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

/** Markdown (a full article, an agent's reply) with raw HTML skipped and http(s) URLs only;
 * `base` (a post's URL) resolves relative links and images. */
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
