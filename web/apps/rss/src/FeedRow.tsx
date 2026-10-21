// SPDX-License-Identifier: AGPL-3.0-or-later
// One feed on the manage screen: its folder (moved, filed into a new folder, or the folder
// renamed, inline), when it last synced and last posted, its health, how long it keeps posts,
// Sync now and Remove.
import { Button, Input, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { causeThen, moveFeed, refreshFeed, renameFolder, setRetention, type Feed, type Retention } from "./api";
import { RetentionPicker } from "./RetentionPicker";
import s from "./rss.module.css";

export type FolderRef = { id: number; name: string };

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86_400],
  ["month", 30 * 86_400],
  ["week", 7 * 86_400],
  ["day", 86_400],
  ["hour", 3600],
  ["minute", 60],
];
const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "always" });

/** "just now", "5 minutes ago", "3 weeks ago": how long ago `iso` was, in its largest unit. */
export function since(iso: string, now: number = Date.now()): string {
  const secs = (now - new Date(iso).getTime()) / 1000;
  for (const [unit, size] of UNITS) if (secs >= size) return RELATIVE.format(-Math.floor(secs / size), unit);
  return "just now";
}

function When({ at, never }: { at: string | null; never: string }) {
  if (!at) return <span className={s.muted}>{never}</span>;
  return (
    <time dateTime={at} title={new Date(at).toLocaleString()}>
      {since(at)}
    </time>
  );
}

/**
 * A name typed inline in the row (a new folder, or the folder's new name). `save` answers
 * with why it failed, or null; the field stays until it succeeds. Esc and Cancel leave it.
 */
function InlineName(props: {
  label: string;
  initial: string;
  submit: string;
  save: (name: string) => Promise<string | null>;
  onDone: () => void;
}) {
  const [name, setName] = useState(props.initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const wanted = name.trim();
    if (!wanted || busy) return;
    setBusy(true);
    setError(null);
    const failed = await props.save(wanted);
    setBusy(false);
    if (failed === null) props.onDone();
    else setError(failed);
  };
  return (
    <form
      className={s.inlineForm}
      onSubmit={(e) => void submit(e)}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return;
        e.preventDefault();
        props.onDone();
      }}
    >
      <Input
        label={props.label}
        hideLabel
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        error={error ?? undefined}
      />
      <Button type="submit" size="sm" variant="primary" loading={busy} disabled={!name.trim()}>
        {props.submit}
      </Button>
      <Button size="sm" variant="ghost" onClick={props.onDone}>
        Cancel
      </Button>
    </form>
  );
}

/** What to do after a failed move or folder name (the cause comes first). */
const MOVE_NEXT = "Try again, or choose another folder.";
const NAME_NEXT = "Try again, or choose another folder name.";

/** The folder select's choices that open an inline field rather than move the feed. */
const NEW_FOLDER = "\u0000new";
const RENAME_FOLDER = "\u0000rename";

export type FeedRowProps = {
  feed: Feed;
  folders: FolderRef[];
  /** Sync all is running: every row is syncing. */
  syncingAll: boolean;
  /** Feeds or folders changed: reload them. */
  onChanged: () => void;
  /** Remove was pressed: the screen hides the row and offers Undo. */
  onRemove: (feed: Feed) => void;
};

export function FeedRow({ feed: given, folders, syncingAll, onChanged, onRemove }: FeedRowProps) {
  const client = useClient();
  const toast = useToast();
  const [editing, setEditing] = useState<"new" | "rename" | null>(null);
  const [syncing, setSyncing] = useState(false);
  // An answer (Sync now's feed, a retention just chosen) shows until the feeds reload.
  const [fresh, setFresh] = useState<{ of: Feed; feed: Feed } | null>(null);
  const [keeping, setKeeping] = useState<{ of: Feed; retention: Retention | null } | null>(null);
  const feed = fresh?.of === given ? fresh.feed : given;
  const retention = keeping?.of === given ? keeping.retention : feed.retention;
  const pick = useRef<HTMLSelectElement>(null);
  const [refocus, setRefocus] = useState(0);
  useEffect(() => {
    if (refocus) pick.current?.focus();
  }, [refocus]);
  const title = feed.title;

  const doneEditing = () => {
    setEditing(null);
    setRefocus((n) => n + 1);
  };

  const move = async (folder: string) => {
    if (folder === NEW_FOLDER || folder === RENAME_FOLDER) {
      setEditing(folder === NEW_FOLDER ? "new" : "rename");
      return;
    }
    try {
      await moveFeed(client, feed.id, folder || null);
      onChanged();
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't move ${title}: ${causeThen(e, MOVE_NEXT)}` });
    }
  };
  const intoNewFolder = async (name: string) => {
    try {
      await moveFeed(client, feed.id, name);
    } catch (e) {
      return `Couldn't move ${title}: ${causeThen(e, NAME_NEXT)}`;
    }
    toast.show({ tone: "success", message: `Moved ${title} to ${name}.` });
    onChanged();
    return null;
  };
  const rename = async (name: string) => {
    try {
      await renameFolder(client, feed.folder_id as number, name);
    } catch (e) {
      return `Couldn't rename the folder: ${causeThen(e, NAME_NEXT)}`;
    }
    toast.show({ tone: "success", message: `Renamed ${feed.folder} to ${name}.` });
    onChanged();
    return null;
  };
  const sync = async () => {
    setSyncing(true);
    try {
      setFresh({ of: given, feed: await refreshFeed(client, feed.id) });
      onChanged();
    } catch (e) {
      toast.show({
        tone: "danger",
        message: `Couldn't sync ${title}: ${causeThen(e, "Try again later; Jesinia keeps retrying it on its own.")}`,
      });
    } finally {
      setSyncing(false);
    }
  };
  const keepFor = async (next: Retention | null) => {
    setKeeping({ of: given, retention: next });
    try {
      await setRetention(client, feed.id, next);
      onChanged();
    } catch (e) {
      setKeeping(null);
      toast.show({
        tone: "danger",
        message: `Couldn't change how long ${title} keeps posts: ${causeThen(e, "Choose 1–365 days or 10–10 000 posts.")}`,
      });
    }
  };

  const failing = feed.warning !== null;
  const tries = `${feed.fail_count} ${feed.fail_count === 1 ? "try" : "tries"}`;
  return (
    <tr className={s.feedRow} aria-label={title} data-failing={failing || undefined}>
      <td className={s.cellFeed}>
        <span className={s.manageTitle}>{title}</span>
        <span className={s.manageUrl}>{feed.url}</span>
      </td>
      <td className={s.cellFolder} data-label="Folder">
        <select
          ref={pick}
          className={s.select}
          aria-label={`Folder for ${title}`}
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
          {feed.folder_id !== null ? <option value={RENAME_FOLDER}>Rename folder…</option> : null}
        </select>
        {editing === "new" ? (
          <InlineName
            label={`New folder for ${title}`}
            initial=""
            submit="Move"
            save={intoNewFolder}
            onDone={doneEditing}
          />
        ) : editing === "rename" ? (
          <InlineName
            label={`New name for ${feed.folder ?? ""}`}
            initial={feed.folder ?? ""}
            submit="Save name"
            save={rename}
            onDone={doneEditing}
          />
        ) : null}
      </td>
      <td className={s.cellSynced} data-label="Last synced">
        <When at={feed.last_ok_at} never="Never" />
      </td>
      <td className={s.cellNewest} data-label="Newest post">
        <When at={feed.newest_post_at} never="—" />
      </td>
      <td className={s.cellStatus} data-label="Status">
        {syncing || syncingAll ? (
          <span className={s.syncing}>Syncing</span>
        ) : failing ? (
          <>
            <span className={s.failing}>Failing ({tries})</span>
            <span className={s.cause}>{feed.last_error ?? feed.warning}</span>
          </>
        ) : (
          <span className={s.ok}>OK</span>
        )}
      </td>
      <td className={s.cellKeep} data-label="Keep posts">
        <RetentionPicker
          value={retention}
          onChange={(r) => void keepFor(r)}
          label={`Keep posts for ${title}`}
          subject={title}
        />
      </td>
      <td className={s.cellActions}>
        <Button size="sm" variant="ghost" disabled={syncing || syncingAll} onClick={() => void sync()}>
          Sync now
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onRemove(feed)}>
          Remove
        </Button>
      </td>
    </tr>
  );
}
