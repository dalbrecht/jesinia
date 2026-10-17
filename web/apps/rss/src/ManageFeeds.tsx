// SPDX-License-Identifier: AGPL-3.0-or-later
// Feed management (spec §4.1, §7), opened by the feeds pane's + Add feed: add a feed or a site
// (its advertised feed is found), file feeds into folders (one level) and rename folders,
// unsubscribe, and a failing feed's cause with Retry. OPML lives in Settings → RSS.
import { Button, Input, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { addFeed, moveFeed, refreshFeed, removeFeed, renameFolder, type Feed } from "./api";
import { sentence } from "./Reader";
import s from "./rss.module.css";

/** The datalist of existing folder names the folder field offers. */
export const FOLDER_NAMES_ID = "rss-folder-names";
const FOCUSABLE = "button:not([disabled]), input:not([disabled]), select:not([disabled]), [href]";

function causeOf(e: unknown): string {
  return sentence(e instanceof Error ? e.message : String(e));
}

type FolderRef = { id: number; name: string };

function foldersOf(feeds: Feed[]): FolderRef[] {
  const byId = new Map<number, string>();
  for (const feed of feeds) if (feed.folder_id !== null) byId.set(feed.folder_id, feed.folder ?? "");
  return [...byId].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * A failing feed's cause and its Retry (spec §7), which polls that feed now. The warning stays
 * until a poll succeeds, so an answer without one means the feed is back.
 */
export function FeedProblem({ feed, onChanged }: { feed: Feed; onChanged: () => void }) {
  const client = useClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!feed.warning) return null;
  const retry = async () => {
    setBusy(true);
    try {
      const after = await refreshFeed(client, feed.id);
      if (after.warning) {
        toast.show({ tone: "danger", message: `${feed.title} still fails: ${sentence(after.warning)}` });
      } else {
        toast.show({ tone: "success", message: `${feed.title} is back.` });
      }
      onChanged();
    } catch (e) {
      toast.show({
        tone: "danger",
        message: `Couldn't retry ${feed.title}: ${causeOf(e)} Try again later; Jesinia keeps retrying it on its own.`,
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={s.problem} role="group" aria-label={`${feed.title} is failing`}>
      <span className={s.problemText}>{feed.warning}</span>
      <Button size="sm" variant="ghost" loading={busy} onClick={() => void retry()}>
        Retry
      </Button>
    </div>
  );
}

/** Esc in an inline edit cancels that edit only, not the whole dialog (M-1). */
function cancelOnEscape(cancel: () => void) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    cancel();
  };
}

/** A new folder's name, inline in the feed's row (R28): the feed moves into it. */
function NewFolder({ feed, onDone }: { feed: Feed; onDone: (moved: boolean) => void }) {
  const client = useClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const wanted = name.trim();
    if (!wanted || busy) return;
    setBusy(true);
    try {
      await moveFeed(client, feed.id, wanted);
      toast.show({ tone: "success", message: `Moved ${feed.title} to ${wanted}.` });
      onDone(true);
    } catch (err) {
      toast.show({ tone: "danger", message: `Couldn't move ${feed.title}: ${causeOf(err)}` });
      setBusy(false);
    }
  };
  return (
    <form className={s.renameFolder} onSubmit={(e) => void save(e)} onKeyDown={cancelOnEscape(() => onDone(false))}>
      <Input
        label={`New folder for ${feed.title}`}
        hideLabel
        autoFocus
        placeholder="New folder"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <Button type="submit" size="sm" variant="primary" loading={busy} disabled={!name.trim()}>
        Move
      </Button>
      <Button size="sm" variant="ghost" onClick={() => onDone(false)}>
        Cancel
      </Button>
    </form>
  );
}

function RenameFolder({ folder, onDone }: { folder: FolderRef; onDone: (renamed: boolean) => void }) {
  const client = useClient();
  const toast = useToast();
  const [name, setName] = useState(folder.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const wanted = name.trim();
    if (!wanted || busy) return;
    setBusy(true);
    setError(null);
    try {
      await renameFolder(client, folder.id, wanted);
      toast.show({ tone: "success", message: `Renamed ${folder.name} to ${wanted}.` });
      onDone(true);
    } catch (err) {
      setError(causeOf(err));
      setBusy(false);
    }
  };
  return (
    <form className={s.renameFolder} onSubmit={(e) => void save(e)} onKeyDown={cancelOnEscape(() => onDone(false))}>
      <Input
        label={`New name for ${folder.name}`}
        hideLabel
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        error={error ? `Couldn't rename the folder: ${error}` : undefined}
      />
      <Button type="submit" size="sm" variant="primary" loading={busy} disabled={!name.trim()}>
        Save name
      </Button>
      <Button size="sm" variant="ghost" onClick={() => onDone(false)}>
        Cancel
      </Button>
    </form>
  );
}

/** The folder select's choice that opens the new-folder field. */
const NEW_FOLDER = "\u0000new";

type FeedRowProps = {
  feed: Feed;
  folders: FolderRef[];
  onChanged: () => void;
  /** Unsubscribed: the row is about to go, so the dialog moves focus before it does. */
  onRemoved: (row: HTMLElement) => void;
};
type RowFocus = "unsubscribe" | "confirm" | "pick" | null;

function FeedRow({ feed, folders, onChanged, onRemoved }: FeedRowProps) {
  const client = useClient();
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [naming, setNaming] = useState(false);
  const [busy, setBusy] = useState(false);
  // A control that replaces the focused one takes focus after it renders (I-2).
  const [focusTo, setFocusTo] = useState<RowFocus>(null);
  const row = useRef<HTMLLIElement>(null);
  const pick = useRef<HTMLSelectElement>(null);
  const unsubscribeButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focusTo === null) return;
    const target = { unsubscribe: unsubscribeButton, confirm: confirmButton, pick }[focusTo];
    target.current?.focus();
    setFocusTo(null);
  }, [focusTo]);

  const move = async (folder: string) => {
    if (folder === NEW_FOLDER) {
      setNaming(true);
      return;
    }
    try {
      await moveFeed(client, feed.id, folder || null);
      onChanged();
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't move ${feed.title}: ${causeOf(e)}` });
    }
  };
  const unsubscribe = async () => {
    setBusy(true);
    try {
      await removeFeed(client, feed.id);
      toast.show({ tone: "success", message: `Unsubscribed from ${feed.title}.` });
      if (row.current) onRemoved(row.current);
      onChanged();
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't unsubscribe from ${feed.title}: ${causeOf(e)}` });
      setBusy(false);
      setConfirming(false);
      setFocusTo("unsubscribe");
    }
  };
  return (
    <li ref={row} className={s.manageFeed} aria-label={feed.title} data-feed-row="">
      <div className={s.manageFeedHead}>
        <span className={s.manageTitle}>{feed.title}</span>
        <span className={s.manageUrl}>{feed.url}</span>
      </div>
      <FeedProblem feed={feed} onChanged={onChanged} />
      <div className={s.manageActions}>
        <label className={s.folderPick}>
          <span className={s.muted}>Folder</span>
          <select
            ref={pick}
            aria-label={`Folder for ${feed.title}`}
            value={feed.folder ?? ""}
            onChange={(e) => void move(e.target.value)}
          >
            <option value="">No folder</option>
            {folders.map((f) => (
              <option key={f.id} value={f.name}>
                {f.name}
              </option>
            ))}
            <option value={NEW_FOLDER}>New folder…</option>
          </select>
        </label>
        {naming ? (
          <NewFolder
            feed={feed}
            onDone={(moved) => {
              setNaming(false);
              setFocusTo("pick");
              if (moved) onChanged();
            }}
          />
        ) : null}
        {confirming ? (
          <>
            <span className={s.muted}>Its unkept posts go; kept posts stay in the KB.</span>
            <Button ref={confirmButton} size="sm" variant="danger" loading={busy} onClick={() => void unsubscribe()}>
              Unsubscribe from {feed.title}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setConfirming(false);
                setFocusTo("unsubscribe");
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <Button
            ref={unsubscribeButton}
            size="sm"
            variant="ghost"
            data-unsubscribe=""
            onClick={() => {
              setConfirming(true);
              setFocusTo("confirm");
            }}
          >
            Unsubscribe
          </Button>
        )}
      </div>
    </li>
  );
}

export type ManageFeedsProps = {
  feeds: Feed[];
  /** A feed was added: the reader shows it. */
  onAdded: (feed: Feed) => void;
  /** Feeds or folders changed: reload them. */
  onChanged: () => void;
  /** A feed was unsubscribed (the reader leaves it if it is in view). */
  onRemoved: (feedId: number) => void;
  onClose: () => void;
};

export function ManageFeeds({ feeds, onAdded, onChanged, onRemoved, onClose }: ManageFeedsProps) {
  const client = useClient();
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [folder, setFolder] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<number | null>(null);
  /** After a rename closes, its folder's Rename button takes focus back (I-2). */
  const [renamed, setRenamed] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  /** Pressed on the backdrop itself: only then does a click there close the dialog (M-2). */
  const pressedBackdrop = useRef(false);
  const urlBox = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const titleId = useId();
  const folders = foldersOf(feeds);
  const loose = feeds.filter((f) => f.folder_id === null);

  useEffect(() => {
    // Back to + Add feed when it closes.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    urlBox.current?.focus();
    return () => opener?.focus();
  }, []);

  useEffect(() => {
    if (renamed === null || renaming !== null) return;
    root.current?.querySelector<HTMLElement>(`[data-rename="${renamed}"]`)?.focus();
    setRenamed(null);
  }, [renamed, renaming]);

  /** The unsubscribed row is about to go: focus the next row, else the one before, else the title. */
  const removed = (feedId: number, row: HTMLElement) => {
    const rows = [...(root.current?.querySelectorAll<HTMLElement>("[data-feed-row]") ?? [])];
    const at = rows.indexOf(row);
    const next = rows[at + 1] ?? rows[at - 1];
    const target = next?.querySelector<HTMLElement>("[data-unsubscribe]") ?? heading.current;
    target?.focus();
    onRemoved(feedId);
  };

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const wanted = url.trim();
    if (!wanted || adding) return;
    setAdding(true);
    setError(null);
    try {
      const feed = await addFeed(client, wanted, folder.trim() || null);
      toast.show({ tone: "success", message: `Added ${feed.title}.` });
      onAdded(feed);
    } catch (err) {
      setError(causeOf(err));
      setAdding(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Tab") {
      // Modal: Tab and Shift+Tab cycle through the dialog's own controls.
      const controls = [...(root.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      }
    }
  };

  const feedRows = (list: Feed[], where: string) => (
    <ul className={s.manageList} aria-label={`Feeds in ${where}`}>
      {list.map((feed) => (
        <FeedRow
          key={feed.id}
          feed={feed}
          folders={folders}
          onChanged={onChanged}
          onRemoved={(row) => removed(feed.id, row)}
        />
      ))}
    </ul>
  );

  return (
    <div
      className={s.scrim}
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        // A drag from a field that ends out here (selecting text) is not a click on the backdrop.
        if (pressedBackdrop.current && e.target === e.currentTarget) onClose();
        pressedBackdrop.current = false;
      }}
    >
      <div
        ref={root}
        className={s.manage}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId} ref={heading} tabIndex={-1}>
          Feeds
        </h2>
        <form className={s.addFeed} onSubmit={(e) => void add(e)}>
          <Input
            ref={urlBox}
            label="Feed or site URL"
            variant="path"
            placeholder="https://example.com"
            help="Paste a feed, or a site: its advertised feed is found."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            error={error ? `Couldn't add the feed: ${error}` : undefined}
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
            Add feed
          </Button>
        </form>
        <div className={s.manageBody}>
          {folders.map((f) => (
            <section key={f.id} className={s.manageFolder} aria-label={f.name}>
              <div className={s.manageFolderHead}>
                {renaming === f.id ? (
                  <RenameFolder
                    folder={f}
                    onDone={(changed) => {
                      setRenaming(null);
                      setRenamed(f.id);
                      if (changed) onChanged();
                    }}
                  />
                ) : (
                  <>
                    <h3>{f.name}</h3>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Rename ${f.name}`}
                      data-rename={f.id}
                      onClick={() => setRenaming(f.id)}
                    >
                      Rename
                    </Button>
                  </>
                )}
              </div>
              {feedRows(
                feeds.filter((feed) => feed.folder_id === f.id),
                f.name,
              )}
            </section>
          ))}
          {loose.length ? (
            <section className={s.manageFolder} aria-label="No folder">
              <div className={s.manageFolderHead}>
                <h3>No folder</h3>
              </div>
              {feedRows(loose, "no folder")}
            </section>
          ) : null}
        </div>
        <div className={s.manageFoot}>
          <span className={s.muted}>
            OPML import and export are in <a href="/settings/#rss">Settings → RSS</a>.
          </span>
          <Button variant="secondary" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </div>
  );
}
