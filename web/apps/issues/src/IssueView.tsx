// SPDX-License-Identifier: AGPL-3.0-or-later
// One issue in the book layout (spec §6): the outline on the left (summary, timeline,
// suggestions, related and duplicates, the epic), a toolbar on top (readiness, priority, the repo,
// Open on GitHub), suggestions with Accept, Edit and Dismiss, and Approve and Dismiss all along
// the bottom. Accepting or approving applies at once (spec §6.2); a picker change stages in the
// tray. The issue's text and the agent's reasons are untrusted: Markdown goes through the shared
// safe renderer (no raw HTML, no images), everything else is plain text, and hidden characters
// show as visible tokens (R62).
import { Badge, Button, toneFor, useToast } from "@jesinia/design";
import { safeHttpUrl, SafeMarkdown, useClient, VisibleText, visibleText, type MdNode } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActionKeys,
  describeSuggestion,
  dismissSuggestion,
  editSuggestion,
  errorCause,
  getItem,
  getSuggestions,
  parseRef,
  refreshItem,
  runSuggestions,
  syncedAgo,
  type Dimension,
  type IssueDetail,
  type Repo,
  type Suggestion,
  type SuggestionList,
  type SuggestionRun,
} from "./api";
import s from "./issues.module.css";
import { useTray } from "./Tray";

/** Every text node of the issue's Markdown with its hidden characters as visible tokens. */
function revealTree(node: MdNode): void {
  if (typeof node.value === "string") node.value = visibleText(node.value);
  for (const child of node.children ?? []) revealTree(child);
}

const PICKERS: { dimension: Dimension; label: string }[] = [
  { dimension: "readiness", label: "Readiness" },
  { dimension: "priority", label: "Priority" },
];

/** How often an issue's suggestions are read while a run is queued or running. */
export const RUN_POLL_MS = 2000;

const DIMENSIONS: readonly Dimension[] = ["readiness", "priority", "type"];

const OUTLINE = [
  { id: "summary", label: "Summary" },
  { id: "timeline", label: "Timeline" },
  { id: "suggestions", label: "Suggestions" },
  { id: "related", label: "Related" },
  { id: "epic", label: "Epic" },
] as const;

function runText(run: SuggestionRun | null, hasOpen: boolean): string {
  if (run === null) return hasOpen ? "" : "No suggestions yet.";
  if (run.status === "waiting") return "Suggestions are waiting for an agent runner; manual triage works meanwhile.";
  if (run.status === "queued" || run.status === "running") return "Claude is suggesting…";
  if (run.status === "failed") return `The last suggestion run failed: ${run.cause ?? "no cause given"}`;
  return hasOpen ? "" : "No open suggestions.";
}

function eventText(e: IssueDetail["timeline"][number]): string {
  const detail = e.label ?? e.source ?? e.state_reason ?? "";
  return detail ? `${e.event} ${detail}` : e.event;
}

type Props = {
  repo: string;
  number: number;
  repos: Repo[];
  onTitle: (title: string) => void;
  onOpenRef: (repo: string, number: number) => void;
};

export function IssueView({ repo, number, repos, onTitle, onOpenRef }: Props) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const keys = useRef(new ActionKeys());
  const ref = `${repo}#${number}`;
  const [issue, setIssue] = useState<IssueDetail | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [refreshProblem, setRefreshProblem] = useState<string | null>(null);
  const [list, setList] = useState<SuggestionList | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());

  const keyed = useCallback(async <T,>(action: string, work: (key: string) => Promise<T>): Promise<T> => {
    setBusy((prev) => new Set(prev).add(action));
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(action);
        return next;
      });
    }
  }, []);

  const fail = useCallback(
    (what: string, e: unknown) => toast.show({ tone: "danger", message: `Couldn't ${what}: ${errorCause(e)}` }),
    [toast],
  );

  // The parent passes a fresh callback each render; reading it through a ref keeps the loads
  // below from re-running on every render.
  const titleRef = useRef(onTitle);
  titleRef.current = onTitle;
  const shown = useCallback((found: IssueDetail) => {
    setIssue(found);
    setProblem(null);
    titleRef.current(found.title);
  }, []);

  const readSuggestions = useCallback(async () => {
    try {
      setList(await getSuggestions(client, { repo, n: number }));
    } catch (e) {
      fail(`read the suggestions for ${ref}`, e);
    }
  }, [client, repo, number, ref, fail]);

  useEffect(() => {
    getItem(client, repo, number).then(shown, (e: unknown) => setProblem(errorCause(e)));
    void readSuggestions();
  }, [client, repo, number, shown, readSuggestions, tray.revision]);

  // While Claude is suggesting, its drafts are read again until the run settles.
  const runStatus = list?.run?.status ?? null;
  const isSuggesting = runStatus === "queued" || runStatus === "running";
  useEffect(() => {
    if (!isSuggesting) return;
    const timer = setTimeout(() => void readSuggestions(), RUN_POLL_MS);
    return () => clearTimeout(timer);
  }, [isSuggesting, list, readSuggestions]);

  // Opening an issue fetches its comments and timeline once (the cache shows meanwhile).
  useEffect(() => {
    keyed(`refresh:${ref}`, (key) => refreshItem(client, repo, number, key)).then(
      (found) => {
        shown(found);
        setRefreshProblem(null);
      },
      (e: unknown) => setRefreshProblem(errorCause(e)),
    );
  }, [client, repo, number, ref, keyed, shown]);

  if (issue === null) {
    return problem === null ? (
      <p className={s.empty}>Opening {ref}…</p>
    ) : (
      <div className={s.problem} role="alert">{`Couldn't open ${ref}: ${problem}`}</div>
    );
  }

  const schema = repos.find((r) => r.repo.toLowerCase() === repo.toLowerCase())?.label_schema ?? null;
  const open = list?.suggestions.filter((sug) => sug.state === "draft" || sug.state === "edited") ?? [];
  const duplicates = open.filter((sug) => sug.kind === "duplicate");
  const github = safeHttpUrl(issue.url);
  const parent = issue.parent === null ? null : parseRef(issue.parent);

  const valuesFor = (dimension: string): string[] => {
    if (schema === null) return [];
    if (dimension === "area") return (schema.areas ?? []).map((c) => c.name);
    const single = DIMENSIONS.find((d) => d === dimension);
    return single === undefined ? [] : schema[single].map((c) => c.name);
  };

  const pick = (dimension: Dimension, value: string) => {
    if (!value) return;
    void tray.stage(repo, number, { kind: "set_dimension", dimension, value }, "picker");
  };

  const accept = async (suggestion: Suggestion) => {
    await tray.approve(suggestion);
    await readSuggestions();
  };

  const dismiss = async (suggestion: Suggestion) => {
    try {
      await keyed(`dismiss:${suggestion.id}`, (key) => dismissSuggestion(client, suggestion.id, key));
    } catch (e) {
      fail(`dismiss "${describeSuggestion(suggestion)}"`, e);
    }
    await readSuggestions();
  };

  const save = async (suggestion: Suggestion) => {
    const dimension = String(suggestion.payload.dimension);
    try {
      await keyed(`edit:${suggestion.id}`, (key) =>
        editSuggestion(client, suggestion.id, { dimension, value: draft.trim() }, key),
      );
      setEditing(null);
    } catch (e) {
      fail(`edit "${describeSuggestion(suggestion)}"`, e);
    }
    await readSuggestions();
  };

  const approveAll = async () => {
    for (const suggestion of open) await tray.approve(suggestion);
    await readSuggestions();
  };

  const dismissAll = async () => {
    for (const suggestion of open) {
      try {
        await keyed(`dismiss:${suggestion.id}`, (key) => dismissSuggestion(client, suggestion.id, key));
      } catch (e) {
        fail(`dismiss "${describeSuggestion(suggestion)}"`, e);
      }
    }
    await readSuggestions();
  };

  const suggest = async () => {
    try {
      await keyed(`suggest:${ref}`, (key) => runSuggestions(client, [{ repo, n: number }], key));
      toast.show({ tone: "info", message: `Asked Claude for suggestions on ${ref}.` });
    } catch (e) {
      fail(`ask for suggestions on ${ref}`, e);
    }
    await readSuggestions();
  };

  const history = [
    ...issue.timeline.map((e) => ({ at: e.created_at, key: `e${e.id ?? e.event}`, event: e, comment: null })),
    ...issue.comments.map((c) => ({ at: c.created_at, key: `c${c.id}`, event: null, comment: c })),
  ].sort((a, b) => (a.at ?? "").localeCompare(b.at ?? ""));
  const status = runText(list?.run ?? null, open.length > 0);

  return (
    <div className={s.book}>
      <nav className={s.outline} aria-label="Outline">
        <ol>
          {OUTLINE.map((part) => (
            <li key={part.id}>
              <a href={`#${part.id}-${number}`} className={s.outlineLink}>
                {part.label}
                {part.id === "suggestions" && open.length > 0 ? <Badge>{open.length}</Badge> : null}
              </a>
            </li>
          ))}
        </ol>
      </nav>
      <article className={s.page}>
        <div className={s.toolbar} role="toolbar" aria-label="Issue">
          {PICKERS.map(({ dimension, label }) => (
            <label key={dimension} className={s.field}>
              <span>{label}</span>
              <select
                aria-label={label}
                value={issue[dimension] ?? ""}
                onChange={(e) => pick(dimension, e.target.value)}
              >
                <option value="">{`No ${dimension}`}</option>
                {valuesFor(dimension).map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <span className={s.repoName}>{repo}</span>
          <span className={s.spacer} />
          {github ? (
            <Button size="sm" variant="ghost" href={github} target="_blank" rel="noopener noreferrer">
              Open on GitHub ↗
            </Button>
          ) : null}
        </div>
        <h1 className={s.title}>
          <VisibleText text={issue.title} />
        </h1>
        <p className={s.meta}>
          <span className={s.ref}>{ref}</span> · opened by <VisibleText text={issue.author} /> · {issue.state}
          {issue.missing.map((dimension) => (
            <Badge key={dimension} tone="warn" icon="!">
              needs {dimension}
            </Badge>
          ))}
        </p>

        <section id={`summary-${number}`} className={s.section} aria-label="Summary">
          <h2 className={s.kicker}>Summary</h2>
          <div className={s.prose}>
            {issue.body.trim() ? (
              <SafeMarkdown markdown={issue.body} transform={revealTree} />
            ) : (
              <p className={s.muted}>No description.</p>
            )}
          </div>
          <div className={s.labels}>
            {issue.labels.map((label) => (
              <Badge key={label} tone={toneFor(label)}>
                <VisibleText text={label} />
              </Badge>
            ))}
          </div>
        </section>

        <section id={`timeline-${number}`} className={s.section} aria-label="Timeline">
          <h2 className={s.kicker}>Timeline</h2>
          {refreshProblem ? (
            <p className={s.muted}>{`Couldn't fetch the latest from GitHub: ${refreshProblem}`}</p>
          ) : null}
          {history.length === 0 ? <p className={s.muted}>Nothing yet.</p> : null}
          <ol className={s.timeline}>
            {history.map((item) =>
              item.comment ? (
                <li key={item.key} className={s.comment}>
                  <div className={s.commentHead}>
                    <b>
                      <VisibleText text={item.comment.author} />
                    </b>
                    <span className={s.muted}>{syncedAgo(item.comment.created_at) ?? ""}</span>
                  </div>
                  <div className={s.prose}>
                    <SafeMarkdown markdown={item.comment.body} transform={revealTree} />
                  </div>
                </li>
              ) : item.event ? (
                <li key={item.key} className={s.event}>
                  <b>
                    <VisibleText text={item.event.actor} />
                  </b>{" "}
                  {visibleText(eventText(item.event))}
                </li>
              ) : null,
            )}
          </ol>
        </section>

        <section id={`suggestions-${number}`} className={s.section} aria-label="Suggestions">
          <h2 className={s.kicker}>Suggestions</h2>
          <div className={s.runLine}>
            {status ? <span className={s.muted}>{status}</span> : null}
            <span className={s.spacer} />
            <Button size="sm" variant="ghost" loading={busy.has(`suggest:${ref}`)} onClick={() => void suggest()}>
              Suggest with Claude
            </Button>
          </div>
          <ul className={s.suggestions}>
            {open.map((sug) => {
              const isLabel = sug.kind === "label" || sug.kind === "new_area";
              const dimension = String(sug.payload.dimension ?? "");
              const isEditing = editing === sug.id;
              return (
                <li key={sug.id} className={s.suggestion}>
                  <div className={s.sugMain}>
                    <span className={s.sugWhat}>
                      <VisibleText text={describeSuggestion(sug)} />
                    </span>
                    {sug.state === "edited" ? <Badge>edited</Badge> : null}
                    <span className={s.why}>
                      <VisibleText text={sug.reason} />
                    </span>
                    {sug.apply_error ? (
                      <span className={s.sugError}>
                        <VisibleText text={`Didn't apply: ${sug.apply_error}`} />
                      </span>
                    ) : null}
                  </div>
                  {isEditing ? (
                    <div className={s.sugActions}>
                      {sug.kind === "label" ? (
                        <select
                          aria-label={`New ${dimension}`}
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                        >
                          {valuesFor(dimension).map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input aria-label="New area" value={draft} onChange={(e) => setDraft(e.target.value)} />
                      )}
                      <Button
                        size="sm"
                        variant="primary"
                        loading={busy.has(`edit:${sug.id}`)}
                        onClick={() => void save(sug)}
                      >
                        Save
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <div className={s.sugActions}>
                      <Button size="sm" variant="primary" onClick={() => void accept(sug)}>
                        Accept
                      </Button>
                      {isLabel ? (
                        <Button
                          size="sm"
                          onClick={() => {
                            setEditing(sug.id);
                            setDraft(String(sug.payload.value ?? ""));
                          }}
                        >
                          Edit
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={busy.has(`dismiss:${sug.id}`)}
                        onClick={() => void dismiss(sug)}
                      >
                        Dismiss
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>

        <section id={`related-${number}`} className={s.section} aria-label="Related">
          <h2 className={s.kicker}>Related and duplicates</h2>
          {duplicates.length === 0 && !issue.children?.length ? (
            <p className={s.muted}>Nothing related found.</p>
          ) : null}
          <ul className={s.plain}>
            {duplicates.map((sug) => (
              <li key={sug.id}>
                Possible duplicate:{" "}
                <button
                  type="button"
                  className={s.linkButton}
                  onClick={() => onOpenRef(String(sug.payload.repo), Number(sug.payload.n))}
                >
                  {`${String(sug.payload.repo)}#${String(sug.payload.n)}`}
                </button>
              </li>
            ))}
            {(issue.children ?? []).map((child) => (
              <li key={child}>
                Sub-issue: <span className={s.ref}>{child}</span>
              </li>
            ))}
          </ul>
        </section>

        <section id={`epic-${number}`} className={s.section} aria-label="Epic">
          <h2 className={s.kicker}>Epic</h2>
          {parent ? (
            <p>
              In{" "}
              <button type="button" className={s.linkButton} onClick={() => onOpenRef(parent.repo, parent.number)}>
                {issue.parent}
              </button>
            </p>
          ) : (
            <p className={s.muted}>Not in an epic.</p>
          )}
        </section>

        <div className={s.decide} role="group" aria-label="Decide">
          <Button variant="primary" disabled={open.length === 0} onClick={() => void approveAll()}>
            Approve
          </Button>
          <Button variant="ghost" disabled={open.length === 0} onClick={() => void dismissAll()}>
            Dismiss all
          </Button>
          <span className={s.muted}>Approving applies to GitHub at once; picker changes wait in Pending changes.</span>
        </div>
      </article>
    </div>
  );
}
