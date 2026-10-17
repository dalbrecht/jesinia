// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → RSS (design pass, spec §4.1): OPML import and export, how long unread posts are
// kept, and the KB folder kept posts go to. Changes save as you make them; every write sends a
// fresh Idempotency-Key.
import { errorText, withIdempotency, type components } from "@jesinia/client";
import { Button, Input, Panel, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import s from "../settings.module.css";
import { sentence, unreachable } from "./unreachable";

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
const SPLIT = "Export again with fewer feeds, or split the file.";

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
  /** Why the settings didn't load: the server's refusal, or no answer at all. */
  const [loadError, setLoadError] = useState<{ cause: string; answered: boolean } | null>(null);
  const [folder, setFolder] = useState("");
  const [folderError, setFolderError] = useState<string | null>(null);
  const [savingFolder, setSavingFolder] = useState(false);
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<Imported | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadFeeds = useCallback(async () => {
    try {
      const r = await client.GET("/api/rss/feeds");
      if (r.data) setFeeds(r.data);
    } catch {
      // Only the "N feeds in M folders" summary: the settings' own load says why it failed.
    }
  }, [client]);

  /** Bumped by Try again: the load runs again. */
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setLoadError(null);
    client.GET("/api/rss/settings").then(
      (r) => {
        if (r.data) {
          setSettings(r.data);
          setFolder(r.data.source_folder);
        } else {
          setLoadError({ cause: errorText(r.error, `HTTP ${r.response.status}`), answered: true });
        }
      },
      // No answer (a restart, say): not stuck on Loading…, the cause and the fix instead.
      (e: unknown) => setLoadError({ cause: unreachable(e), answered: false }),
    );
    void loadFeeds();
  }, [client, loadFeeds, attempt]);

  // Both settings share one document, PUT whole. Writes go one at a time, each carrying the
  // latest intended values (a retention change during a folder save keeps the new folder); a
  // refused change is taken back out so it never rides along with the next one.
  const intended = useRef<RssSettings | null>(null);
  const confirmed = useRef<RssSettings | null>(null);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    if (settings && confirmed.current === null) confirmed.current = intended.current = settings;
  }, [settings]);

  const write = (patch: Partial<RssSettings>) => {
    const base = intended.current ?? (settings as RssSettings);
    intended.current = { ...base, ...patch };
    setSettings(intended.current); // shown at once
    let sent: RssSettings | null = null;
    const run = () => {
      sent = intended.current as RssSettings;
      return client.PUT("/api/rss/settings", { params: { header: withIdempotency() }, body: sent });
    };
    const result = queue.current.then(run, run);
    queue.current = result.catch(() => undefined);
    return result.then(
      (r) => {
        if (r.data) {
          confirmed.current = r.data;
          // The server's values, except those changed since this write left (a later write
          // carries them).
          intended.current = { ...r.data, ...changedSince(sent as RssSettings) };
        } else {
          takeBack(patch);
        }
        setSettings(intended.current);
        return r;
      },
      (e: unknown) => {
        takeBack(patch);
        setSettings(intended.current);
        throw e;
      },
    );
  };
  const changedSince = (sent: RssSettings): Partial<RssSettings> => {
    const want = intended.current as RssSettings;
    return Object.fromEntries(
      (Object.keys(want) as (keyof RssSettings)[]).filter((k) => want[k] !== sent[k]).map((k) => [k, want[k]]),
    );
  };
  const takeBack = (patch: Partial<RssSettings>) => {
    const back = confirmed.current as RssSettings;
    const undo = Object.fromEntries((Object.keys(patch) as (keyof RssSettings)[]).map((k) => [k, back[k]]));
    intended.current = { ...(intended.current as RssSettings), ...undo };
  };

  const changeRetention = async (days: number) => {
    let message: string;
    try {
      const r = await write({ retention: days });
      if (r.data) {
        toast.show({ tone: "success", message: `Unread posts are kept for ${count(r.data.retention, "day")}.` });
        return;
      }
      message = errorText(r.error);
    } catch (e) {
      message = unreachable(e);
    }
    toast.show({ tone: "danger", message: `Couldn't save the retention: ${message}` });
  };

  const saveFolder = async (e: FormEvent) => {
    e.preventDefault();
    setSavingFolder(true);
    setFolderError(null);
    try {
      const r = await write({ source_folder: folder.trim() });
      if (r.data) {
        setFolder(r.data.source_folder);
        toast.show({ tone: "success", message: `Kept posts now go to ${r.data.source_folder}/.` });
      } else {
        setFolderError(
          errorText(r.error, "Couldn't use that folder. Choose a folder inside your KB, such as Sources."),
        );
      }
    } catch (err) {
      setFolderError(`Couldn't save the folder: ${unreachable(err)}`);
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
        setImported({
          error: `Couldn't read ${file.name}: ${sentence(err instanceof Error ? err.message : String(err))} Choose the file again.`,
        });
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
      setImported({ error: `Couldn't import ${file.name}: ${unreachable(err)}` });
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
      toast.show({ tone: "danger", message: `Couldn't refresh: ${unreachable(e)}` });
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
        <div className={s.form}>
          <p className={s.muted}>
            Couldn't load the RSS settings: {sentence(loadError.cause)}
            {loadError.answered ? (
              <>
                {" "}
                Restart Jesinia; <code>jesinia status</code> shows why.
              </>
            ) : null}
          </p>
          <div className={s.actions}>
            <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </Button>
          </div>
        </div>
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
              onChange={(e) => void changeRetention(Number(e.target.value))}
            >
              {retentionChoices.map((days) => (
                <option key={days} value={days}>
                  {count(days, "day")}
                </option>
              ))}
            </select>
          </div>
          <form className={s.setting} onSubmit={(e) => void saveFolder(e)}>
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
