// SPDX-License-Identifier: AGPL-3.0-or-later
// Untrusted Markdown on screen (ruling R17): feed articles and agent replies. It goes through
// react-markdown with raw HTML skipped, never innerHTML, and every href and src must be http(s);
// anything else is dropped. Agent output never loads a resource (R24): images are off there.
// Shared so every chat renders agent replies the same safe way.
import { memo, type ReactNode } from "react";
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

/** An href longer than this is not a link anyone means to follow (it may carry data out). */
export const MAX_LINK_CHARS = 2048;

/** The URL's host, for showing where a link or image points ('' when there is none). */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

const withHost = (text: string, url: string) => {
  const host = hostOf(url);
  return host ? `${text}${text ? " " : ""}(${host})` : text;
};

/** Feed articles: http(s) images load (lazily, no referrer). */
const ARTICLE: Components = {
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

/**
 * Untrusted text that must never load anything by itself (agent replies, ruling R24): an
 * image is its alt text and host as plain text; a link shows its host and its full href in
 * a title, and an absurdly long one is plain text.
 */
const NO_LOADS: Components = {
  a: ({ href, children }) =>
    href && href.length <= MAX_LINK_CHARS ? (
      <>
        <a href={href} title={href} {...LINK_PROPS}>
          {children}
        </a>{" "}
        ({hostOf(href)})
      </>
    ) : (
      <>{children}</>
    ),
  img: ({ src, alt }) => <>{withHost(alt ?? "", typeof src === "string" ? src : "")}</>,
};

/**
 * Markdown with raw HTML skipped and http(s) URLs only; `base` (a post's URL) resolves
 * relative links and images. Images are off unless `images` is set: only feed article views
 * may load them, never agent output.
 */
export function SafeMarkdown({
  markdown,
  base = "",
  images = false,
}: {
  markdown: string;
  base?: string;
  images?: boolean;
}) {
  return (
    <Markdown skipHtml urlTransform={(url) => httpOnly(url, base)} components={images ? ARTICLE : NO_LOADS}>
      {markdown}
    </Markdown>
  );
}

/** Streaming re-renders the transcript on every delta: unchanged messages keep their tree. */
const ChatMarkdown = memo(function ChatMarkdown({ markdown }: { markdown: string }) {
  return <SafeMarkdown markdown={markdown} />;
});

/** For `ChatTranscript`'s `renderMarkdown`: agent replies, images off (R24). */
export function renderMarkdown(markdown: string): ReactNode {
  return <ChatMarkdown markdown={markdown} />;
}
