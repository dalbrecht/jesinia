// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → RSS (design pass, spec §4.1): OPML import and export, how long unread posts are
// kept, and the KB folder kept posts go to. Changes save as you make them; every write sends a
// fresh Idempotency-Key.
import { errorText, withIdempotency, type components } from "@jesinia/client";
import { Button, Input, Panel, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useId, useState, type ChangeEvent, type FormEvent } from "react";
import s from "../settings.module.css";

type Feed = components["schemas"]["FeedOut"];
type RssSettings = components["schemas"]["SettingsBody"];
type ImportResult = components["schemas"]["ImportResultOut"];
type PollSummary = components["schemas"]["PollSummaryOut"];
type Imported = { file: string; result: ImportResult } | { file: string; error: string };

/** The usual retention choices (days); a value set elsewhere (1–365) is offered too. */
export const RETENTION_CHOICES = [7, 14, 30, 90, 180, 365] as const;

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function feedSummary(feeds: Feed[]): string {
  const folders = new Set(feeds.flatMap((f) => (f.folder_id === null ? [] : [f.folder_id])));
  return `${count(feeds.length, "feed")} in ${count(folders.size, "folder")}`;
}

function pollSummary({ polled, new_items, failed }: PollSummary): string {
  const news = new_items ? count(new_items, "new post") : "no new posts";
  const failures = failed ? `, ${count(failed, "feed")} failed` : "";
  return `Polled ${count(polled, "feed")}: ${news}${failures}.`;
}

function ImportOutcome({ imported }: { imported: Imported }) {
  if ("error" in imported) {
    return (
      <p className={s.error} role="alert">
        Couldn't import {imported.file}: {imported.error}
      </p>
    );
  }
  const { added, existing, failed } = imported.result;
  return (
    <div role="status" className={s.outcome}>
      Added {count(added, "feed")}.{existing ? ` ${count(existing, "was", "were")} already followed.` : ""}
      {failed.length ? ` ${failed.length} couldn't be added:` : ""}
      {failed.length ? (
        <ul className={s.plainList}>
          {failed.map((f) => (
            <li key={f.url}>
              <span className={s.mono}>{f.url}</span> <span className={s.muted}>{f.cause}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function RssSection() {
  const client = useClient();
  const toast = useToast();
  const retentionId = useId();
  const [feeds, setFeeds] = useState<Feed[] | null>(null);
  const [settings, setSettings] = useState<RssSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [folder, setFolder] = useState("");
  const [folderError, setFolderError] = useState<string | null>(null);
  const [savingFolder, setSavingFolder] = useState(false);
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<Imported | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadFeeds = useCallback(async () => {
    const r = await client.GET("/api/rss/feeds");
    if (r.data) setFeeds(r.data);
  }, [client]);

  useEffect(() => {
    void client.GET("/api/rss/settings").then((r) => {
      if (r.data) {
        setSettings(r.data);
        setFolder(r.data.source_folder);
      } else {
        setLoadError(errorText(r.error, `HTTP ${r.response.status}`));
      }
    });
    void loadFeeds();
  }, [client, loadFeeds]);

  const save = (next: RssSettings) =>
    client.PUT("/api/rss/settings", { params: { header: withIdempotency() }, body: next });

  const changeRetention = async (current: RssSettings, days: number) => {
    setSettings({ ...current, retention: days }); // shown at once; put back if it fails
    const r = await save({ ...current, retention: days });
    if (r.data) {
      setSettings(r.data);
      toast.show({ tone: "success", message: `Unread posts are kept for ${count(r.data.retention, "day")}.` });
    } else {
      setSettings(current);
      toast.show({ tone: "danger", message: `Couldn't save the retention: ${errorText(r.error)}` });
    }
  };

  const saveFolder = async (e: FormEvent, current: RssSettings) => {
    e.preventDefault();
    setSavingFolder(true);
    setFolderError(null);
    const r = await save({ ...current, source_folder: folder.trim() });
    setSavingFolder(false);
    if (r.data) {
      setSettings(r.data);
      setFolder(r.data.source_folder);
      toast.show({ tone: "success", message: `Kept posts now go to ${r.data.source_folder}/.` });
    } else {
      setFolderError(errorText(r.error, "Couldn't use that folder."));
    }
  };

  const importOpml = async (e: ChangeEvent<HTMLInputElement>) => {
    const input = e.target;
    const file = input.files?.[0];
    if (!file) return; // the chooser was cancelled
    setImporting(true);
    setImported(null);
    const r = await client.POST("/api/rss/opml", {
      params: { header: withIdempotency() },
      body: { opml: await file.text() },
    });
    setImporting(false);
    // The same file can be chosen again (after fixing it, say).
    input.value = "";
    if (r.data) {
      setImported({ file: file.name, result: r.data });
      await loadFeeds();
    } else {
      setImported({ file: file.name, error: errorText(r.error) });
    }
  };

  const refresh = async () => {
    setRefreshing(true);
    const r = await client.POST("/api/rss/refresh", { params: { header: withIdempotency() } });
    setRefreshing(false);
    if (r.data) toast.show({ message: pollSummary(r.data) });
    else toast.show({ tone: "danger", message: `Couldn't refresh: ${errorText(r.error)}` });
  };

  const retentionChoices = settings
    ? [...new Set<number>([...RETENTION_CHOICES, settings.retention])].sort((a, b) => a - b)
    : [];

  return (
    <Panel
      title="RSS"
      id="rss"
      actions={feeds ? <span className={s.muted}>{feedSummary(feeds)}</span> : undefined}
      footer={
        settings ? (
          <div className={s.actions}>
            <Button loading={refreshing} onClick={() => void refresh()}>
              Refresh all feeds now
            </Button>
          </div>
        ) : undefined
      }
    >
      {loadError ? (
        <p className={s.muted}>Couldn't load the RSS settings: {loadError}</p>
      ) : !settings ? (
        <p className={s.muted}>Loading…</p>
      ) : (
        <div className={s.form}>
          <div className={s.setting}>
            <div>
              <h3 className={s.settingTitle}>Import OPML</h3>
              <p className={s.muted}>
                From another reader. Folders are kept (one level); feeds you already follow are skipped.
              </p>
            </div>
            <label className={s.fileButton} aria-busy={importing || undefined}>
              {importing ? "Importing…" : "Choose file…"}
              <input
                type="file"
                accept=".opml,.xml,text/x-opml,text/xml,application/xml"
                aria-label="Import OPML"
                disabled={importing}
                onChange={(e) => void importOpml(e)}
              />
            </label>
          </div>
          {imported ? <ImportOutcome imported={imported} /> : null}
          <div className={s.setting}>
            <div>
              <h3 className={s.settingTitle}>Export OPML</h3>
              <p className={s.muted}>Every feed and folder.</p>
            </div>
            <Button href="/api/rss/opml" download="jesinia-feeds.opml" variant="secondary" size="sm">
              Download OPML
            </Button>
          </div>
          <div className={s.setting}>
            <div>
              <label htmlFor={retentionId} className={s.settingTitle}>
                Keep unread posts for
              </label>
              <p className={s.muted}>Kept posts stay forever.</p>
            </div>
            <select
              id={retentionId}
              className={s.select}
              value={settings.retention}
              onChange={(e) => void changeRetention(settings, Number(e.target.value))}
            >
              {retentionChoices.map((days) => (
                <option key={days} value={days}>
                  {count(days, "day")}
                </option>
              ))}
            </select>
          </div>
          <form className={s.setting} onSubmit={(e) => void saveFolder(e, settings)}>
            <Input
              label="Folder for kept posts"
              variant="path"
              value={folder}
              onChange={(e) => {
                setFolder(e.target.value);
                setFolderError(null);
              }}
              error={folderError ?? undefined}
              help="Each kept post becomes a page here, with its archived article."
            />
            <Button type="submit" size="sm" loading={savingFolder} disabled={!folder.trim()}>
              Save folder
            </Button>
          </form>
        </div>
      )}
    </Panel>
  );
}
