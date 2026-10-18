// SPDX-License-Identifier: AGPL-3.0-or-later
// The Inbox and Backlog (spec §3, §6): a compact list with multi-select and a bulk bar ("Suggest
// with Claude", "Approve all"), or the board as a view toggle. Served from the cache at once and
// read again in the background, with "Updated N min ago · refreshing…" (spec §6.3); when GitHub
// can't be read the cached list stays under a banner. The themes sit beside the list and the board
// as drop targets and a filter; the bulk bar holds every drop's menu equivalent ("Add theme…",
// "Move to epic…", "Add to queue…") for the selected issues.
import { Badge, Banner, Button, Draggable, toneFor, useToast, VisuallyHidden } from "@jesinia/design";
import { useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionKeys,
  IDLE_POLL_MS,
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
  getTheme,
} from "./api";
import { Board, itemOf, type Grouping } from "./Board";
import { Drops } from "./drops";
import { MoveToEpicMenu } from "./Epics";
import s from "./issues.module.css";
import { AddToQueueMenu } from "./Queues";
import { AddThemeMenu, ThemesColumn, useAddTheme, useThemes } from "./Themes";
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
  /** Shown now: a hidden list reads the cache at the idle pace only. */
  active: boolean;
};

export function IssueList({ view, repos, onOpen, active }: IssueListProps) {
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
  const [themeFilter, setThemeFilter] = useState<{ tag: string; refs: ReadonlySet<string> } | null>(null);
  const { themes, reload: reloadThemes } = useThemes();
  // A theme added or undone while the list is filtered by it changes what the filter shows.
  const filterTag = useRef<string | null>(null);
  filterTag.current = themeFilter?.tag ?? null;
  const refreshFilter = useCallback(async () => {
    const tag = filterTag.current;
    if (tag === null) return;
    try {
      const found = await getTheme(client, tag);
      const refs = new Set(found.issues.map((i) => refOf({ repo: i.repo, number: i.n }).toLowerCase()));
      setThemeFilter((current) => (current?.tag === tag ? { tag, refs } : current));
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't refresh the filter by #${tag}: ${errorCause(e)}` });
    }
  }, [client, toast]);
  const addTheme = useAddTheme(() => {
    void reloadThemes();
    void refreshFilter();
  });

  // A read started earlier (another filter, a poll) may resolve later: only the newest shows.
  const reads = useRef({ issued: 0, shown: 0 });
  const read = useCallback(async () => {
    const id = ++reads.current.issued;
    try {
      const found = await getView(client, view, { repo: repo || undefined, filter: filter || undefined });
      if (id < reads.current.shown) return;
      reads.current.shown = id;
      setData(found);
      setReadError(null);
      setFailedReads(0);
    } catch (e) {
      // An older read failing after a newer one answered changes nothing (re-review N7).
      if (id < reads.current.shown) return;
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

  const pollMs = active ? nextPollMs(data, failedReads) : Math.max(IDLE_POLL_MS, nextPollMs(data, failedReads));
  useEffect(() => {
    const timer = setTimeout(() => void read(), pollMs);
    return () => clearTimeout(timer);
  }, [read, pollMs, attempts]);

  const all = useMemo(() => data?.rows ?? [], [data]);
  const rows = useMemo(
    () => (themeFilter === null ? all : all.filter((row) => themeFilter.refs.has(refOf(row).toLowerCase()))),
    [all, themeFilter],
  );
  const chosen = useMemo(() => rows.filter((row) => selected.has(refOf(row))), [rows, selected]);
  const targets = chosen.map((row) => ({ repo: row.repo, n: row.number }));
  const group = useMemo(() => chosen.map(itemOf), [chosen]);
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

  // Approving applies at once (spec §6.2), one batch per suggestion, followed in the tray. Each
  // selected issue's suggestions are read on their own (never a capped repo page), and every
  // outcome is reported: nothing is skipped without saying so.
  const approveAll = async () => {
    setBusy("approve");
    const unread: string[] = [];
    let open = 0;
    let started = 0;
    for (const row of chosen) {
      let found;
      try {
        found = await getSuggestions(client, { repo: row.repo, n: row.number });
      } catch (e) {
        unread.push(refOf(row));
        toast.show({ tone: "danger", message: `Couldn't read the suggestions for ${refOf(row)}: ${errorCause(e)}` });
        continue;
      }
      for (const suggestion of found.suggestions) {
        open += 1;
        if (await tray.approve(suggestion)) started += 1;
      }
    }
    setBusy(null);
    const parts = [];
    if (open === 0 && unread.length === 0) parts.push("No open suggestions to approve for the selected issues.");
    if (open > 0)
      parts.push(
        `Approved ${started} of ${plural(open, "suggestion", "suggestions")}; results are in Pending changes.`,
      );
    if (started < open) parts.push(`${open - started} couldn't be approved (see why above).`);
    if (unread.length > 0) parts.push(`Suggestions of ${unread.join(", ")} couldn't be read.`);
    toast.show({ tone: started === open && unread.length === 0 ? "info" : "warn", message: parts.join(" ") });
  };

  const syncError = data?.sync_error ?? null;
  const signedOut = syncError !== null && /gh auth login/.test(syncError);

  return (
    <Drops>
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
            <legend>
              <VisuallyHidden>View</VisuallyHidden>
            </legend>
            {(["list", "board"] as const).map((m) => (
              <label key={m} className={s.segment} data-on={mode === m || undefined}>
                <input type="radio" name={`${view}-mode`} checked={mode === m} onChange={() => setMode(m)} />
                {m === "list" ? "List" : "Board"}
              </label>
            ))}
          </fieldset>
          {mode === "board" ? (
            <fieldset className={s.segmented}>
              <legend>
                <VisuallyHidden>Columns by</VisuallyHidden>
              </legend>
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
            <AddThemeMenu
              targets={targets}
              themes={themes}
              name="Add a theme to the selected issues"
              onAdd={(tag, issues) => void addTheme(tag, issues)}
            />
            <MoveToEpicMenu targets={chosen} name="Move the selected issues to an epic" />
            <AddToQueueMenu targets={chosen} name="Add the selected issues to a queue" />
            <span className={s.spacer} />
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
              Clear selection
            </Button>
          </div>
        ) : null}
        {data === null && readError !== null ? (
          <div className={s.problem} role="alert">{`Couldn't read the ${NAMES[view]}: ${readError}`}</div>
        ) : null}
        {themeFilter !== null ? (
          <div className={s.filterChip}>
            <span>{`Showing the issues with #${themeFilter.tag}`}</span>
            <Button size="sm" variant="ghost" onClick={() => setThemeFilter(null)}>
              Show all
            </Button>
          </div>
        ) : null}
        <div className={s.withThemes}>
          <div className={s.listMain}>
            {data !== null && mode === "board" ? (
              <Board rows={rows} repos={repos} grouping={grouping} selected={selected} onOpen={onOpen} />
            ) : null}
            {data !== null && mode === "list" ? (
              rows.length === 0 ? (
                <p className={s.empty}>
                  {repos.length === 0 ? "Add a repo in Settings → Issues to start." : EMPTY[view]}
                </p>
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
                    {rows.map((row) => {
                      const item = itemOf(row);
                      return (
                        <li key={refOf(row)} data-selected={selected.has(refOf(row)) || undefined}>
                          <Draggable id={item.id} label={item.label} data={row} group={group} className={s.row}>
                            {(handle) => (
                              <>
                                <button type="button" {...handle} className={s.grip} aria-label={`Move ${item.label}`}>
                                  <span aria-hidden="true">⠿</span>
                                </button>
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
                              </>
                            )}
                          </Draggable>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )
            ) : null}
          </div>
          <ThemesColumn
            themes={themes}
            filter={themeFilter?.tag ?? null}
            onFilter={(tag, refs) => setThemeFilter(tag === null || refs === null ? null : { tag, refs })}
            selected={targets}
            onAdd={(tag, issues) => void addTheme(tag, issues)}
          />
        </div>
      </div>
    </Drops>
  );
}
