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
type Imported = { result: ImportResult } | { error: string };

/** The usual retention choices (days); a value set elsewhere (1–365) is offered too. */
export const RETENTION_CHOICES = [7, 14, 30, 90, 180, 365] as const;
/** The server's cap on an OPML document (`OPML_MAX`, 5 MB): checked before reading the file. */
export const OPML_MAX_BYTES = 5_000_000;
const OPML_MAX_MB = OPML_MAX_BYTES / 1_000_000;
/** The next step when a request never got an answer (§9: the cause, then the fix). */
const UNREACHABLE = "Check that Jesinia is running, then try again.";
const SPLIT = "Export again with fewer feeds, or split the file.";

const failure = (e: unknown) => (e instanceof Error ? e.message : String(e));
/** "cause." with a sentence's full stop, so the next step can follow it. */
const sentence = (text: string) => (/[.!?]$/.test(text.trim()) ? text.trim() : `${text.trim()}.`);

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

/** What an import did, in a live region that always exists so its change is announced. */
function ImportOutcome({ imported }: { imported: Imported | null }) {
  const done = imported && "result" in imported ? imported.result : null;
  return (
    <>
      <div role="status" className={s.outcome}>
        {done ? (
          <>
            Added {count(done.added, "feed")}.
            {done.existing ? ` ${count(done.existing, "was", "were")} already followed.` : ""}
            {done.failed.length ? ` ${done.failed.length} couldn't be added:` : ""}
            {done.failed.length ? (
              <ul className={s.plainList}>
                {done.failed.map((f, i) => (
                  <li key={`${i}:${f.url}`}>
                    <span className={s.mono}>{f.url}</span> <span className={s.muted}>{f.cause}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </div>
      {imported && "error" in imported ? (
        <p className={s.error} role="alert">
          {imported.error}
        </p>
      ) : null}
    </>
  );
}

/** Why the server refused an import, in plain words with the next step. */
function refusal(file: string, status: number, error: unknown): string {
  // 413 from a proxy, 422 from the server's size cap: the file is too big (it isn't empty).
  if (status === 413 || status === 422) {
    return `Couldn't import ${file}: it is larger than the ${OPML_MAX_MB} MB Jesinia imports. ${SPLIT}`;
  }
  const cause = sentence(errorText(error, `HTTP ${status}`));
  return `Couldn't import ${file}: ${cause} Export the list from your reader as OPML, then choose that file.`;
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
    let message: string;
    try {
      const r = await save({ ...current, retention: days });
      if (r.data) {
        setSettings(r.data);
        toast.show({ tone: "success", message: `Unread posts are kept for ${count(r.data.retention, "day")}.` });
        return;
      }
      message = errorText(r.error);
    } catch (e) {
      message = `${sentence(failure(e))} ${UNREACHABLE}`;
    }
    setSettings(current);
    toast.show({ tone: "danger", message: `Couldn't save the retention: ${message}` });
  };

  const saveFolder = async (e: FormEvent, current: RssSettings) => {
    e.preventDefault();
    setSavingFolder(true);
    setFolderError(null);
    try {
      const r = await save({ ...current, source_folder: folder.trim() });
      if (r.data) {
        setSettings(r.data);
        setFolder(r.data.source_folder);
        toast.show({ tone: "success", message: `Kept posts now go to ${r.data.source_folder}/.` });
      } else {
        setFolderError(
          errorText(r.error, "Couldn't use that folder. Choose a folder inside your KB, such as Sources."),
        );
      }
    } catch (err) {
      setFolderError(`Couldn't save the folder: ${sentence(failure(err))} ${UNREACHABLE}`);
    } finally {
      setSavingFolder(false);
    }
  };

  const importOpml = async (e: ChangeEvent<HTMLInputElement>) => {
    const input = e.target;
    const file = input.files?.[0];
    if (!file) return; // the chooser was cancelled
    // The same file can be chosen again (after fixing it, say).
    input.value = "";
    setImported(null);
    if (file.size > OPML_MAX_BYTES) {
      const mb = Math.ceil(file.size / 1_000_000);
      setImported({ error: `${file.name} is ${mb} MB; Jesinia imports OPML files up to ${OPML_MAX_MB} MB. ${SPLIT}` });
      return;
    }
    if (file.size === 0) {
      setImported({ error: `${file.name} is empty. Choose the OPML file your reader exported.` });
      return;
    }
    setImporting(true);
    try {
      let opml: string;
      try {
        opml = await file.text();
      } catch (err) {
        setImported({ error: `Couldn't read ${file.name}: ${sentence(failure(err))} Choose the file again.` });
        return;
      }
      const r = await client.POST("/api/rss/opml", { params: { header: withIdempotency() }, body: { opml } });
      if (r.data) {
        setImported({ result: r.data });
        await loadFeeds();
      } else {
        setImported({ error: refusal(file.name, r.response.status, r.error) });
      }
    } catch (err) {
      setImported({ error: `Couldn't import ${file.name}: ${sentence(failure(err))} ${UNREACHABLE}` });
    } finally {
      setImporting(false);
    }
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await client.POST("/api/rss/refresh", { params: { header: withIdempotency() } });
      if (r.data) toast.show({ message: pollSummary(r.data) });
      else toast.show({ tone: "danger", message: `Couldn't refresh: ${errorText(r.error)}` });
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't refresh: ${sentence(failure(e))} ${UNREACHABLE}` });
    } finally {
      setRefreshing(false);
    }
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
        <p className={s.muted}>
          Couldn't load the RSS settings: {sentence(loadError)} Restart Jesinia; <code>jesinia status</code> shows why.
        </p>
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
            <label className={s.fileButton}>
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
          <ImportOutcome imported={imported} />
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
