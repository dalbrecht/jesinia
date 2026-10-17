// SPDX-License-Identifier: AGPL-3.0-or-later
// The reader (spec §2.1, design pass): a toolbar with Open original, Engage and Keep at the
// top right, the post in the serif reading style, and Previous / Next / Mark read along the
// bottom. Mount one per post (`key={post.id}`): the loaded article belongs to that post.
import { Button } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useState } from "react";
import { fullArticle, needsFullArticle, readingMinutes, retryArchive, safeHttpUrl, type Post } from "./api";
import { archiveStatus, ArchiveBadge } from "./PostList";
import s from "./rss.module.css";
import { SafeHtml, SafeMarkdown } from "./SafeContent";

/** The server's cause as a sentence, so the fix can follow it. */
export function sentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

function causeOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function byline(post: Post, words: string): string {
  const published = post.published_at ?? post.first_seen_at;
  const date = new Date(published).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  return [post.feed_title, post.author, date, `${readingMinutes(words)} min`].filter(Boolean).join(" · ");
}

/** Visible words, for the reading time only (never rendered). */
function words(html: string): string {
  return new DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
}

type Article = { state: "idle" | "loading" | "done"; markdown: string; error: string | null };

export type ReaderProps = {
  post: Post;
  hasPrevious: boolean;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
  onToggleRead: () => void;
  /** The post changed on the server (an archive retry). */
  onChanged: (post: Post) => void;
  /** Task 14 wires these; until then the buttons show but are unavailable. */
  onKeep?: () => void;
  onEngage?: () => void;
  /** Narrow layout: back to the list, labelled with where it goes. */
  back?: { label: string; onBack: () => void };
};

export function Reader(props: ReaderProps) {
  const { post } = props;
  const client = useClient();
  const [article, setArticle] = useState<Article>({ state: "idle", markdown: "", error: null });
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const original = safeHttpUrl(post.url);
  const status = archiveStatus(post);

  const load = async () => {
    setArticle((a) => ({ ...a, state: "loading", error: null }));
    try {
      setArticle({ state: "done", markdown: await fullArticle(client, post.id), error: null });
    } catch (e) {
      setArticle({ state: "idle", markdown: "", error: causeOf(e) });
    }
  };

  const retry = async () => {
    setRetrying(true);
    setRetryError(null);
    try {
      props.onChanged(await retryArchive(client, post.id));
    } catch (e) {
      setRetryError(causeOf(e));
    } finally {
      setRetrying(false);
    }
  };

  const text = article.state === "done" ? article.markdown : words(post.content_html ?? post.snippet);
  return (
    <section className={s.reader} aria-label="Reader">
      <div className={s.paneHead} role="toolbar" aria-label="Post actions">
        {props.back ? (
          <Button variant="ghost" size="sm" onClick={props.back.onBack}>
            ‹ {props.back.label}
          </Button>
        ) : null}
        <span className={s.headSource}>{post.feed_title}</span>
        <span className={s.headActions}>
          {original ? (
            <Button variant="ghost" size="sm" href={original} target="_blank" rel="noopener noreferrer">
              Open original ↗
            </Button>
          ) : null}
          <Button variant="secondary" size="sm" title="Engage (e)" disabled={!props.onEngage} onClick={props.onEngage}>
            Engage
          </Button>
          <Button variant="primary" size="sm" title="Keep (n)" disabled={!props.onKeep} onClick={props.onKeep}>
            Keep
          </Button>
        </span>
      </div>
      <div className={s.readerScroll}>
        <article className={s.read}>
          <h1>{post.title}</h1>
          <p className={s.byline}>{byline(post, text)}</p>
          {status ? (
            <div className={s.archive} role="status">
              {status === "failed" ? (
                <>
                  <span>
                    {post.archive_error ? `Archive incomplete: ${sentence(post.archive_error)}` : "Archive incomplete."}
                  </span>
                  <Button variant="ghost" size="sm" loading={retrying} onClick={() => void retry()}>
                    Retry
                  </Button>
                </>
              ) : (
                <ArchiveBadge status={status} />
              )}
              {retryError ? <span className={s.error}>Couldn't retry the archive: {sentence(retryError)}</span> : null}
            </div>
          ) : null}
          {article.state === "done" ? (
            <SafeMarkdown markdown={article.markdown} />
          ) : post.content_html ? (
            <SafeHtml html={post.content_html} base={post.url} />
          ) : (
            <p>{post.snippet}</p>
          )}
          {article.state !== "done" && needsFullArticle(post) ? (
            <div className={s.fullArticle}>
              <span>{post.is_summary ? "The feed has a summary only." : "The feed's text may be cut short."}</span>
              <Button variant="ghost" size="sm" loading={article.state === "loading"} onClick={() => void load()}>
                Load full article
              </Button>
              {article.error ? (
                <p className={s.error} role="alert">
                  Couldn't load the full article: {sentence(article.error)} Try again, or open the original.
                </p>
              ) : null}
            </div>
          ) : null}
        </article>
      </div>
      <div className={s.paneFoot} role="toolbar" aria-label="Reading">
        <Button variant="ghost" size="sm" disabled={!props.hasPrevious} onClick={props.onPrevious}>
          <span aria-hidden="true">←</span> Previous
        </Button>
        <Button variant="ghost" size="sm" disabled={!props.hasNext} onClick={props.onNext}>
          Next <span aria-hidden="true">→</span>
        </Button>
        <span className={s.footEnd}>
          <Button variant="ghost" size="sm" onClick={props.onToggleRead}>
            {post.read ? "Mark unread" : "Mark read"}
          </Button>
        </span>
      </div>
    </section>
  );
}
