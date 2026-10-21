// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed management at /rss/manage (spec §4.1, §7), in place of the list and the reader: add a
// feed or a site (its advertised feed is found), Sync all, OPML in and out, how long posts are
// kept by default, and every feed's health with its own folder, retention, Sync now and Remove.
// This is the one place sync errors show.
import { Button, Input, useToast, VisuallyHidden } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import {
  addFeed,
  causeThen,
  errorCause,
  getRssSettings,
  importOpml,
  OPML_EXPORT_URL,
  putRssSettings,
  refreshFeeds,
  removeFeed,
  RssRequestError,
  type Feed,
  type ImportResult,
  type RssSettings,
} from "./api";
import { FeedRow, type FolderRef } from "./FeedRow";
import { sentence } from "./Reader";
import s from "./rss.module.css";

/** The datalist of existing folder names the folder field offers. */
export const FOLDER_NAMES_ID = "rss-folder-names";
/** The server's cap on an OPML document (`OPML_MAX`, 5 MB): checked before reading the file. */
export const OPML_MAX_BYTES = 5_000_000;
const OPML_MAX_MB = OPML_MAX_BYTES / 1_000_000;
const SPLIT = "Export again with fewer feeds, or split the file.";
/** The usual default retentions (days); a value set elsewhere (1–365) is offered too. */
const DAY_CHOICES = [7, 14, 30, 90, 180, 365];

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function foldersOf(feeds: Feed[]): FolderRef[] {
  const byId = new Map<number, string>();
  for (const feed of feeds) if (feed.folder_id !== null) byId.set(feed.folder_id, feed.folder ?? "");
  return [...byId].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

/** Failing feeds first, then by title whatever its case. */
function sorted(feeds: Feed[]): Feed[] {
  return [...feeds].sort(
    (a, b) =>
      Number(b.warning !== null) - Number(a.warning !== null) ||
      a.title.toLowerCase().localeCompare(b.title.toLowerCase()),
  );
}

function importSummary(file: string, { added, existing, failed }: ImportResult): string {
  const parts = [`added ${plural(added, "feed")}`];
  if (existing) parts.push(`skipped ${existing} already followed`);
  if (failed.length) parts.push(`${failed.length} failed (${failed.map((f) => `${f.url}: ${f.cause}`).join("; ")})`);
  return `Imported ${file}: ${parts.join(", ")}.`;
}

/** Why an import was refused, in plain words with the next step. */
function refusal(file: string, e: unknown): string {
  // 413 from a proxy, 422 from the server's size cap: the file is too big (it isn't empty).
  if (e instanceof RssRequestError && (e.status === 413 || e.status === 422)) {
    return `Couldn't import ${file}: it is larger than the ${OPML_MAX_MB} MB Jesinia imports. ${SPLIT}`;
  }
  const next = e instanceof RssRequestError ? " Export the list from your reader as OPML, then choose that file." : "";
  return `Couldn't import ${file}: ${errorCause(e)}${next}`;
}

export type ManageFeedsProps = {
  feeds: Feed[];
  /** Feeds or folders changed: reload them. */
  onChanged(): void;
  /** A feed was removed (the reader leaves it if it is in view). */
  onRemoved(id: number): void;
  /** ‹ Back to reading. */
  onBack(): void;
};

export function ManageFeeds({ feeds, onChanged, onRemoved, onBack }: ManageFeedsProps) {
  const client = useClient();
  const toast = useToast();
  const titleId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const [url, setUrl] = useState("");
  const [folder, setFolder] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [only, setOnly] = useState<"all" | "failing">("all");
  const [syncingAll, setSyncingAll] = useState(false);
  const [importing, setImporting] = useState(false);
  const [settings, setSettings] = useState<RssSettings | null>(null);
  /** Removed but still undoable: hidden here, sent when its toast closes without Undo. */
  const [hidden, setHidden] = useState<ReadonlySet<number>>(new Set());
  /** Per feed removed but not yet sent: its toast. */
  const pending = useRef(new Map<number, { toastId: string; feed: Feed }>());
  // A remove can be sent after the screen has gone (leaving it sends them): use the latest props.
  const latest = useRef({ onChanged, onRemoved });
  latest.current = { onChanged, onRemoved };

  useEffect(() => heading.current?.focus(), []);

  useEffect(() => {
    getRssSettings(client).then(setSettings, (e: unknown) =>
      toast.show({
        tone: "danger",
        message: `Couldn't load the default retention: ${causeThen(e, "Open Manage feeds again to retry.")}`,
      }),
    );
  }, [client, toast]);

  // Leaving the screen (Back, or the browser's Back) sends any pending removes first: closing
  // their toasts reports "dismiss", and that sends them.
  useEffect(
    () => () => {
      for (const { toastId } of [...pending.current.values()]) toast.dismiss(toastId);
    },
    [toast],
  );

  const unhide = (id: number) =>
    setHidden((h) => {
      const next = new Set(h);
      next.delete(id);
      return next;
    });

  const send = (feed: Feed, opts: { keepalive?: boolean } = {}) => {
    removeFeed(client, feed.id, opts).then(
      () => {
        latest.current.onRemoved(feed.id);
        latest.current.onChanged();
      },
      (e: unknown) => {
        unhide(feed.id);
        toast.show({ tone: "danger", message: `Couldn't remove ${feed.title}: ${causeThen(e, "Try Remove again.")}` });
      },
    );
  };

  const remove = (feed: Feed) => {
    setHidden((h) => new Set(h).add(feed.id));
    const toastId = toast.show({
      message: `Removed ${feed.title}`,
      action: { label: "Undo", onAction: () => {} },
      duration: 8000,
      onClose: (reason) => {
        if (!pending.current.delete(feed.id)) return; // already sent as the page went
        if (reason === "action") unhide(feed.id);
        else send(feed);
      },
    });
    pending.current.set(feed.id, { toastId, feed });
  };

  // A tab closing or reloading runs no React cleanup: pending removes go out as the page hides,
  // as requests that outlive it, and their toasts close (no Undo left to offer).
  useEffect(() => {
    const onHide = () => {
      const now = [...pending.current.values()];
      pending.current.clear();
      for (const { toastId, feed } of now) {
        send(feed, { keepalive: true });
        toast.dismiss(toastId);
      }
    };
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
  });

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const wanted = url.trim();
    if (!wanted || adding) return;
    setAdding(true);
    setAddError(null);
    try {
      const added = await addFeed(client, wanted, folder.trim() || null);
      toast.show({ tone: "success", message: `Added ${added.title}.` });
      setUrl("");
      onChanged();
    } catch (err) {
      setAddError(causeThen(err, "Check the address and try again."));
    } finally {
      setAdding(false);
    }
  };

  const syncAll = async () => {
    setSyncingAll(true);
    try {
      await refreshFeeds(client);
      onChanged();
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't sync the feeds: ${causeThen(e, "Try again in a moment.")}` });
    } finally {
      setSyncingAll(false);
    }
  };

  const chooseOpml = async (e: ChangeEvent<HTMLInputElement>) => {
    const input = e.target;
    const file = input.files?.[0];
    if (!file) return; // the chooser was cancelled
    input.value = ""; // the same file can be chosen again (after fixing it, say)
    const fail = (message: string) => toast.show({ tone: "danger", message });
    if (file.size > OPML_MAX_BYTES) {
      const mb = Math.ceil(file.size / 1_000_000);
      fail(`${file.name} is ${mb} MB; Jesinia imports OPML files up to ${OPML_MAX_MB} MB. ${SPLIT}`);
      return;
    }
    if (file.size === 0) {
      fail(`${file.name} is empty. Choose the OPML file your reader exported.`);
      return;
    }
    setImporting(true);
    try {
      let opml: string;
      try {
        opml = await file.text();
      } catch (err) {
        const why = err instanceof Error ? err.message : String(err);
        fail(`Couldn't read ${file.name}: ${sentence(why)} Choose the file again.`);
        return;
      }
      const result = await importOpml(client, opml);
      toast.show({ tone: result.failed.length ? "warn" : "success", message: importSummary(file.name, result) });
      onChanged();
    } catch (err) {
      fail(refusal(file.name, err));
    } finally {
      setImporting(false);
    }
  };

  const changeDefault = async (days: number) => {
    if (!settings) return;
    const before = settings;
    setSettings({ ...before, retention: days });
    try {
      // The kept-posts folder goes back unchanged: the document is written whole.
      const saved = await putRssSettings(client, { retention: days, source_folder: before.source_folder });
      setSettings(saved);
      toast.show({
        tone: "success",
        message: `Feeds without their own setting keep posts for ${plural(saved.retention, "day")}.`,
      });
    } catch (e) {
      setSettings(before);
      toast.show({
        tone: "danger",
        message: `Couldn't change how long posts are kept: ${causeThen(e, "Choose 1–365 days.")}`,
      });
    }
  };

  const folders = foldersOf(feeds);
  const shown = feeds.filter((f) => !hidden.has(f.id));
  const failingCount = shown.filter((f) => f.warning !== null).length;
  const rows = sorted(only === "failing" ? shown.filter((f) => f.warning !== null) : shown);
  const dayChoices = settings ? [...new Set([...DAY_CHOICES, settings.retention])].sort((a, b) => a - b) : [];

  return (
    <section className={s.manageScreen} aria-labelledby={titleId}>
      <header className={s.manageHead}>
        <Button variant="ghost" size="sm" onClick={onBack}>
          ‹ Back to reading
        </Button>
        <h2 id={titleId} ref={heading} tabIndex={-1} className={s.manageHeading}>
          Manage feeds
        </h2>
        <div className={s.manageTools}>
          <label className={s.fileButton} data-busy={importing || undefined}>
            {importing ? "Importing…" : "Import OPML"}
            <input
              type="file"
              accept=".opml,.xml,text/x-opml,text/xml,application/xml"
              aria-label="Import OPML"
              disabled={importing}
              onChange={(e) => void chooseOpml(e)}
            />
          </label>
          <Button href={OPML_EXPORT_URL} download="jesinia-feeds.opml" variant="ghost" size="sm">
            Export OPML
          </Button>
          <Button variant="secondary" size="sm" loading={syncingAll} onClick={() => void syncAll()}>
            Sync all
          </Button>
        </div>
      </header>
      <form className={s.addFeed} onSubmit={(e) => void add(e)}>
        <Input
          label="Feed or site URL"
          variant="path"
          placeholder="https://example.com"
          help="Paste a feed, or a site: its advertised feed is found."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          error={addError ? `Couldn't add the feed: ${addError}` : undefined}
        />
        <Input
          label="Folder (optional)"
          list={FOLDER_NAMES_ID}
          placeholder="No folder"
          value={folder}
          onChange={(e) => setFolder(e.target.value)}
        />
        <datalist id={FOLDER_NAMES_ID}>
          {folders.map((f) => (
            <option key={f.id} value={f.name} />
          ))}
        </datalist>
        <Button type="submit" variant="primary" size="sm" loading={adding} disabled={!url.trim()}>
          + Add feed
        </Button>
      </form>
      <div className={s.manageBar}>
        <div className={s.filter} role="group" aria-label="Show">
          <button type="button" aria-pressed={only === "all"} onClick={() => setOnly("all")}>
            All {shown.length}
          </button>
          <span aria-hidden="true">·</span>
          <button type="button" aria-pressed={only === "failing"} onClick={() => setOnly("failing")}>
            Failing {failingCount}
          </button>
        </div>
        <label className={s.defaultKeep}>
          <span>Keep posts for</span>
          <select
            className={s.select}
            aria-label="Keep posts for, all feeds"
            disabled={!settings}
            value={settings?.retention ?? ""}
            onChange={(e) => void changeDefault(Number(e.target.value))}
          >
            {dayChoices.map((d) => (
              <option key={d} value={d}>
                {plural(d, "day")}
              </option>
            ))}
          </select>
          <span className={s.muted}>(all feeds)</span>
        </label>
      </div>
      <div className={s.manageTableWrap}>
        <table className={s.manageTable}>
          <thead>
            <tr>
              <th scope="col">Feed</th>
              <th scope="col">Folder</th>
              <th scope="col">Last synced</th>
              <th scope="col">Newest post</th>
              <th scope="col">Status</th>
              <th scope="col">Keep posts</th>
              <th scope="col">
                <VisuallyHidden>Actions</VisuallyHidden>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((feed) => (
              <FeedRow
                key={feed.id}
                feed={feed}
                folders={folders}
                syncingAll={syncingAll}
                onChanged={onChanged}
                onRemove={remove}
              />
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? (
          <p className={s.empty}>
            {only === "failing" ? "No feed is failing." : "No feeds yet. Add one above, or import OPML."}
          </p>
        ) : null}
      </div>
    </section>
  );
}
