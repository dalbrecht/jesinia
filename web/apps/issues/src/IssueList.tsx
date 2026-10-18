// SPDX-License-Identifier: AGPL-3.0-or-later
// The Inbox and Backlog (spec §3, §6): a compact list with multi-select and a bulk bar ("Suggest
// with Claude", "Approve all"), or the board as a view toggle. Served from the cache at once and
// read again in the background, with "Updated N min ago · refreshing…" (spec §6.3); when GitHub
// can't be read the cached list stays under a banner.
import { Badge, Banner, Button, toneFor, useToast } from "@jesinia/design";
import { useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActionKeys,
  errorCause,
  freshness,
  getSuggestions,
  getView,
  nextPollMs,
  refOf,
  runSuggestions,
  SAVED_FILTERS,
  splitCode,
  type IssueRow,
  type IssueView,
  type Repo,
  type SavedFilter,
  type ViewName,
} from "./api";
import { Board, type Grouping } from "./Board";
import s from "./issues.module.css";
import { useTray } from "./Tray";

const NAMES: Record<ViewName, string> = { inbox: "Inbox", backlog: "Backlog" };
const EMPTY: Record<ViewName, string> = {
  inbox: "Nothing new to triage. New issues in the repos you watch land here.",
  backlog: "No issues match.",
};
const GROUPINGS: Record<Grouping, string> = { readiness: "Readiness", priority: "Priority", status: "Status" };
const plural = (n: number, one: string, other: string) => `${n} ${n === 1 ? one : other}`;

/** A sync failure's cause, its commands shown as code (text only, never markup). */
function Cause({ text }: { text: string }) {
  return (
    <>
      {splitCode(text).map((part, i) =>
        part.code ? <code key={i}>{part.text}</code> : <span key={i}>{part.text}</span>,
      )}
    </>
  );
}

export type IssueListProps = {
  view: ViewName;
  repos: Repo[];
  onOpen: (row: IssueRow) => void;
};

export function IssueList({ view, repos, onOpen }: IssueListProps) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const keys = useRef(new ActionKeys());
  const [data, setData] = useState<IssueView | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [failedReads, setFailedReads] = useState(0);
  const [attempts, setAttempts] = useState(0);
  const [repo, setRepo] = useState("");
  const [filter, setFilter] = useState<SavedFilter | "">("");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [mode, setMode] = useState<"list" | "board">("list");
  const [grouping, setGrouping] = useState<Grouping>("readiness");
  const [busy, setBusy] = useState<string | null>(null);

  const read = useCallback(async () => {
    try {
      setData(await getView(client, view, { repo: repo || undefined, filter: filter || undefined }));
      setReadError(null);
      setFailedReads(0);
    } catch (e) {
      // Whatever was shown stays; say why.
      setReadError(errorCause(e));
      setFailedReads((n) => n + 1);
    } finally {
      setAttempts((n) => n + 1);
    }
  }, [client, view, repo, filter]);

  useEffect(() => {
    void read();
  }, [read, tray.revision]);

  const pollMs = nextPollMs(data, failedReads);
  useEffect(() => {
    const timer = setTimeout(() => void read(), pollMs);
    return () => clearTimeout(timer);
  }, [read, pollMs, attempts]);

  const rows = data?.rows ?? [];
  const chosen = rows.filter((row) => selected.has(refOf(row)));
  const allSelected = rows.length > 0 && chosen.length === rows.length;

  const toggle = (row: IssueRow) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(refOf(row))) next.delete(refOf(row));
      else next.add(refOf(row));
      return next;
    });

  const suggest = async () => {
    const refs = chosen.map((row) => ({ repo: row.repo, n: row.number }));
    const action = `suggest:${refs.map((r) => `${r.repo}#${r.n}`).join(",")}`;
    setBusy("suggest");
    try {
      await runSuggestions(client, refs, keys.current.keyFor(action));
      keys.current.settle(action);
      toast.show({
        tone: "info",
        message: `Asked for suggestions on ${plural(refs.length, "issue", "issues")}: they appear as each run finishes.`,
      });
    } catch (e) {
      keys.current.settle(action, e);
      toast.show({ tone: "danger", message: `Couldn't ask for suggestions: ${errorCause(e)}` });
    } finally {
      setBusy(null);
    }
  };

  // Approving applies at once (spec §6.2), one batch per suggestion, followed in the tray.
  const approveAll = async () => {
    setBusy("approve");
    try {
      const wanted = new Set(chosen.map(refOf));
      const byRepo = [...new Set(chosen.map((row) => row.repo))];
      const open = [];
      for (const name of byRepo) {
        const found = await getSuggestions(client, { repo: name });
        open.push(...found.suggestions.filter((sug) => wanted.has(`${sug.repo ?? ""}#${sug.n ?? 0}`)));
      }
      if (open.length === 0) {
        toast.show({ tone: "info", message: "No open suggestions to approve for the selected issues." });
        return;
      }
      for (const suggestion of open) await tray.approve(suggestion);
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't read the suggestions: ${errorCause(e)}` });
    } finally {
      setBusy(null);
    }
  };

  const syncError = data?.sync_error ?? null;
  const signedOut = syncError !== null && /gh auth login/.test(syncError);

  return (
    <div className={s.listPane}>
      <div className={s.listBar}>
        <label className={s.field}>
          <span>Repo</span>
          <select aria-label="Repo" value={repo} onChange={(e) => setRepo(e.target.value)}>
            <option value="">All repos</option>
            {repos.map((r) => (
              <option key={r.repo} value={r.repo}>
                {r.repo}
              </option>
            ))}
          </select>
        </label>
        <label className={s.field}>
          <span>Filter</span>
          <select aria-label="Filter" value={filter} onChange={(e) => setFilter(e.target.value as SavedFilter | "")}>
            <option value="">Everything</option>
            {SAVED_FILTERS.map((f) => (
              <option key={f.id} value={f.id}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        <fieldset className={s.segmented}>
          <legend className={s.visuallyHidden}>View</legend>
          {(["list", "board"] as const).map((m) => (
            <label key={m} className={s.segment} data-on={mode === m || undefined}>
              <input type="radio" name={`${view}-mode`} checked={mode === m} onChange={() => setMode(m)} />
              {m === "list" ? "List" : "Board"}
            </label>
          ))}
        </fieldset>
        {mode === "board" ? (
          <fieldset className={s.segmented}>
            <legend className={s.visuallyHidden}>Columns by</legend>
            {(["readiness", "priority", "status"] as const).map((g) => (
              <label key={g} className={s.segment} data-on={grouping === g || undefined}>
                <input
                  type="radio"
                  name={`${view}-grouping`}
                  checked={grouping === g}
                  onChange={() => setGrouping(g)}
                />
                {GROUPINGS[g]}
              </label>
            ))}
          </fieldset>
        ) : null}
        <span className={s.spacer} />
        {data ? <span className={s.muted}>{freshness(data)}</span> : null}
      </div>
      {syncError !== null ? (
        <div className={s.banners}>
          <Banner tone="danger" cause={signedOut ? "Signed out of GitHub:" : "Couldn't refresh from GitHub:"}>
            <Cause text={syncError} />
            {rows.length ? " What you see is from the cache." : null}
          </Banner>
        </div>
      ) : null}
      {chosen.length > 0 ? (
        <div className={s.bulk} role="toolbar" aria-label="Selected issues">
          <strong>{chosen.length} selected</strong>
          <Button size="sm" loading={busy === "suggest"} onClick={() => void suggest()}>
            Suggest with Claude
          </Button>
          <Button size="sm" variant="primary" loading={busy === "approve"} onClick={() => void approveAll()}>
            Approve all
          </Button>
          <span className={s.spacer} />
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
            Clear selection
          </Button>
        </div>
      ) : null}
      {data === null && readError !== null ? (
        <div className={s.problem} role="alert">{`Couldn't read the ${NAMES[view]}: ${readError}`}</div>
      ) : null}
      {data !== null && mode === "board" ? (
        <Board rows={rows} repos={repos} grouping={grouping} selected={selected} onOpen={onOpen} />
      ) : null}
      {data !== null && mode === "list" ? (
        rows.length === 0 ? (
          <p className={s.empty}>{repos.length === 0 ? "Add a repo in Settings → Issues to start." : EMPTY[view]}</p>
        ) : (
          <div className={s.listScroll}>
            <div className={s.listHead}>
              <input
                type="checkbox"
                aria-label="Select all"
                checked={allSelected}
                onChange={() => setSelected(allSelected ? new Set() : new Set(rows.map(refOf)))}
              />
              <span>{plural(rows.length, "issue", "issues")}</span>
            </div>
            <ul className={s.list} aria-label={NAMES[view]}>
              {rows.map((row) => (
                <li key={refOf(row)} className={s.row} data-selected={selected.has(refOf(row)) || undefined}>
                  <input
                    type="checkbox"
                    aria-label={`Select ${refOf(row)}`}
                    checked={selected.has(refOf(row))}
                    onChange={() => toggle(row)}
                  />
                  <span className={s.ref}>{refOf(row)}</span>
                  <button type="button" className={s.titleButton} onClick={() => onOpen(row)}>
                    <VisibleText text={row.title} />
                  </button>
                  <span className={s.rowLabels}>
                    {row.missing.map((dimension) => (
                      <Badge key={`missing-${dimension}`} tone="warn" icon="!">
                        needs {dimension}
                      </Badge>
                    ))}
                    {row.labels.map((label) => (
                      <Badge key={label} tone={toneFor(label)}>
                        <VisibleText text={label} />
                      </Badge>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )
      ) : null}
    </div>
  );
}
