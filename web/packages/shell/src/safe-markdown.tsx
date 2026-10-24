// SPDX-License-Identifier: AGPL-3.0-or-later
// Untrusted Markdown on screen (ruling R17): feed articles, agent replies and KB pages. It goes
// through react-markdown with raw HTML skipped, never innerHTML, and every href and src must be
// http(s); anything else is dropped. Agent output never loads a resource (R24): images are off
// there. KB pages (R-REN2) may load their own local assets, served by `/api/kb/assets`, and link
// to other pages through the app; nothing else.
// Shared so every chat renders agent replies the same safe way.
import { memo, useMemo, type MouseEvent, type ReactNode } from "react";
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

const ZOTERO_ITEM_PATH = /^zotero:\/\/(?:select|open-pdf)\/library\/items\/[A-Z0-9]{8}$/;
const ZOTERO_QUERY = /^[\w=&.-]*$/;

/** `raw` when it is exactly a Zotero item link (select or open-pdf, optional query), else null. */
export function safeZoteroUrl(raw: string): string | null {
  const queryAt = raw.indexOf("?");
  const path = queryAt === -1 ? raw : raw.slice(0, queryAt);
  const query = queryAt === -1 ? "" : raw.slice(queryAt + 1);
  return ZOTERO_ITEM_PATH.test(path) && ZOTERO_QUERY.test(query) ? raw : null;
}

/** A KB page's local image, as the server serves it: `/api/kb/assets/<page-id>/<file>`. */
const KB_ASSET =
  /^\/api\/kb\/assets\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/[A-Za-z0-9][A-Za-z0-9_-]*(?:$|\.[A-Za-z0-9]+$)/;

/** Whether `url` is exactly a KB asset path (the only image a KB page may load). */
export function isKbAssetUrl(url: string): boolean {
  return KB_ASSET.test(url);
}

/** The app-internal scheme a transform gives links to other KB pages: `jesinia-page:<id>`. */
export const PAGE_LINK_SCHEME = "jesinia-page:";
const PAGE_LINK = /^jesinia-page:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

/** How page links (`jesinia-page:<id>`) become in-app links. */
export type PageLinks = { href: (id: string) => string; open: (id: string) => void };

/**
 * A Markdown (mdast) node, as far as an app's transform needs it. `data.hName` and
 * `data.hProperties` choose the element it renders as.
 */
export type MdNode = {
  type: string;
  value?: string;
  url?: string;
  alt?: string | null;
  depth?: number;
  children?: MdNode[];
  data?: { hName?: string; hProperties?: Record<string, string | string[] | undefined> };
};

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

/**
 * Feed articles: https images load (lazily, no referrer). An http: image is its alt text: the
 * reader's CSP allows https: images only, so it would only show as broken.
 */
type LinkProps = { href?: string | undefined; children?: ReactNode };

/** A link that leaves the app in a new tab (feed articles, KB pages). */
function NewTabLink({ href, children }: LinkProps) {
  return href ? (
    <a href={href} {...LINK_PROPS}>
      {children}
    </a>
  ) : (
    <>{children}</>
  );
}

/** A link in text that must not load anything: its host shows, and an absurdly long one is text. */
function HostLink({ href, children }: LinkProps) {
  return href && href.length <= MAX_LINK_CHARS ? (
    <>
      <a href={href} title={href} {...LINK_PROPS}>
        {children}
      </a>{" "}
      ({hostOf(href)})
    </>
  ) : (
    <>{children}</>
  );
}

const ARTICLE: Components = {
  a: NewTabLink,
  img: ({ src, alt }) => {
    if (typeof src !== "string" || !src) return null;
    return src.startsWith("https:") ? <img src={src} alt={alt ?? ""} {...IMG_PROPS} /> : <>{alt ?? ""}</>;
  },
};

/**
 * Untrusted text that must never load anything by itself (agent replies, ruling R24): an
 * image is its alt text and host as plain text; a link shows its host and its full href in
 * a title, and an absurdly long one is plain text.
 */
const NO_LOADS: Components = {
  a: HostLink,
  img: ({ src, alt }) => <>{withHost(alt ?? "", typeof src === "string" ? src : "")}</>,
};

/**
 * A KB page: its own assets load (lazily); a remote image is its alt text and host (Notes'
 * CSP is `default-src 'self'`); links open in a new tab.
 */
const LOCAL: Components = {
  a: NewTabLink,
  img: ({ src, alt }) => {
    const url = typeof src === "string" ? src : "";
    return isKbAssetUrl(url) ? <img src={url} alt={alt ?? ""} loading="lazy" /> : <>{withHost(alt ?? "", url)}</>;
  },
};

/** A plain click opens the page in the app; a modified one is left to the browser. */
function inApp(open: () => void) {
  return (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    open();
  };
}

/** `components` with page links (`jesinia-page:<id>`) as in-app links; `Other` renders the rest. */
function withPageLinks(base: Components, Other: (props: LinkProps) => ReactNode, links: PageLinks): Components {
  return {
    ...base,
    a: ({ href, children }) => {
      const id = PAGE_LINK.exec(href ?? "")?.[1];
      if (id === undefined) return <Other href={href}>{children}</Other>;
      return (
        <a href={links.href(id)} onClick={inApp(() => links.open(id))}>
          {children}
        </a>
      );
    },
  };
}

/**
 * `components` with Zotero item links as plain anchors: no new tab (it would break the protocol
 * handoff to the desktop app) and no host suffix; `Other` renders every other link.
 */
function withZoteroLinks(base: Components): Components {
  const Other = base.a as (props: LinkProps) => ReactNode;
  return {
    ...base,
    a: ({ href, children }) =>
      href !== undefined && safeZoteroUrl(href) !== null ? (
        <a href={href} rel="noopener noreferrer">
          {children}
        </a>
      ) : (
        Other({ href, children })
      ),
  };
}

export type SafeMarkdownProps = {
  markdown: string;
  /** A post's URL: relative links and images resolve against it. */
  base?: string;
  /**
   * `true`: http(s) images load (feed articles only). `"local"`: only KB assets
   * (`/api/kb/assets/…`) load (KB pages). Off by default, and always for agent output.
   */
  images?: boolean | "local";
  /** Rewrites the Markdown tree before it renders (e.g. Notes' page links, tags, AI blocks). */
  transform?: (tree: MdNode) => void;
  /** With this, `jesinia-page:<id>` links open that page in the app; without it they drop. */
  pageLinks?: PageLinks;
};

/**
 * Markdown with raw HTML skipped and http(s) URLs only; `base` (a post's URL) resolves
 * relative links and images. Images are off unless `images` is set: only feed article views
 * and KB pages (their local assets) may load them, never agent output.
 */
export function SafeMarkdown({ markdown, base = "", images = false, transform, pageLinks }: SafeMarkdownProps) {
  const components = useMemo(() => {
    const chosen = images === "local" ? LOCAL : images ? ARTICLE : NO_LOADS;
    const withPages = pageLinks ? withPageLinks(chosen, images ? NewTabLink : HostLink, pageLinks) : chosen;
    return withZoteroLinks(withPages);
  }, [images, pageLinks]);
  const plugins = useMemo(() => (transform ? [() => transform] : []), [transform]);
  const urlTransform = (url: string, key: string) => {
    if (images === "local" && key === "src" && isKbAssetUrl(url)) return url;
    if (pageLinks && key === "href" && PAGE_LINK.test(url)) return url;
    if (key === "href") {
      const zotero = safeZoteroUrl(url);
      if (zotero !== null) return zotero;
    }
    return httpOnly(url, base);
  };
  return (
    <Markdown skipHtml remarkPlugins={plugins} urlTransform={urlTransform} components={components}>
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
