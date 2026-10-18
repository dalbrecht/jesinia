// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Review (design pass, spec §2, §5.2 and §6.1): the pre-review setting and the agent,
// the gates (fixed, shown for reference), the standards folders, the watched repos with their
// per-repo overrides (automated reviewer, pre-review, metrics), and "What Jesinia thinks you
// know", where the author corrects the knowledge model. Every write sends a fresh
// Idempotency-Key. Folder names in the knowledge model are anyone's to write: they render as
// text, with hidden and direction-changing characters as visible tokens (R62).
import { errorText, withIdempotency, type components } from "@jesinia/client";
import { Badge, Button, Input, Panel, useToast } from "@jesinia/design";
import { RUNNER_NAMES, useClient, VisibleText, visibleText } from "@jesinia/shell";
import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import s from "../settings.module.css";
import { sentence, unreachable } from "./unreachable";

type Settings = components["schemas"]["ReviewSettingsBody"];
type RepoSetting = components["schemas"]["ReviewRepoSettingBody"];
type PreReview = Settings["prereview"];
type Unit = components["schemas"]["UnitOut"];
type Knowledge = components["schemas"]["KnowledgeOut"];
type Correction = "known" | "unknown" | "";

const PREREVIEW: { value: PreReview; label: string; help: string }[] = [
  {
    value: "triage",
    label: "Triage",
    help: "The recommendation, correctness pass and findings run in the background; the rest when you open the PR.",
  },
  { value: "everything", label: "Everything", help: "The whole review is ready before you open the PR." },
  { value: "on_open", label: "On open only", help: "Nothing runs until you open the PR." },
];
const prereviewLabel = (mode: PreReview) => PREREVIEW.find((p) => p.value === mode)?.label ?? mode;
/** The agents a review can run on; one set elsewhere is offered too. */
const RUNNERS = ["codex", "claude"] as const;

type Load = { cause: string; answered: boolean };

/** The words for a request that failed: the server's reason, or why it got no answer. */
async function attempt<T>(
  request: () => Promise<{ data?: T; error?: unknown }>,
): Promise<{ data: T } | { error: string }> {
  try {
    const r = await request();
    if (r.data !== undefined) return { data: r.data };
    return { error: errorText(r.error) };
  } catch (e) {
    return { error: unreachable(e) };
  }
}

function LoadError({ what, load, retry }: { what: string; load: Load; retry: () => void }) {
  return (
    <div className={s.form}>
      <p className={s.muted}>
        Couldn't load {what}: {sentence(load.cause)}
        {load.answered ? (
          <>
            {" "}
            Restart Jesinia; <code>jesinia status</code> shows why.
          </>
        ) : null}
      </p>
      <div className={s.actions}>
        <Button size="sm" onClick={retry}>
          Try again
        </Button>
      </div>
    </div>
  );
}

/** One repo's overrides, saved together; a metrics change applies from its next round. */
function RepoForm({
  config,
  defaultMode,
  save,
}: {
  config: RepoSetting;
  defaultMode: PreReview;
  save: (repo: RepoSetting) => Promise<boolean>;
}) {
  const modeId = useId();
  const [watched, setWatched] = useState(config.watched);
  const [mode, setMode] = useState<PreReview | "">(config.prereview ?? "");
  const [reviewer, setReviewer] = useState(config.automated_reviewer ?? "");
  const [metrics, setMetrics] = useState((config.metrics ?? []).join("\n"));
  const [saving, setSaving] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    await save({
      repo: config.repo,
      watched,
      // Watching a suggested repo is the author's consent, as Confirm in the queue is.
      suggested: config.suggested && !watched,
      prereview: mode === "" ? null : mode,
      automated_reviewer: reviewer.trim() || null,
      metrics: metrics
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    });
    setSaving(false);
  };

  return (
    <form onSubmit={(e) => void submit(e)}>
      <fieldset className={s.repo}>
        <legend className={s.repoName}>{config.repo}</legend>
        {config.suggested ? (
          <p>
            <Badge>Suggested</Badge>
          </p>
        ) : null}
        <label className={s.check}>
          <input type="checkbox" checked={watched} onChange={(e) => setWatched(e.target.checked)} />
          Watched
        </label>
        <div className={s.inline}>
          <label htmlFor={modeId} className={s.settingTitle}>
            Pre-review
          </label>
          <select
            id={modeId}
            aria-label={`Pre-review for ${config.repo}`}
            className={s.select}
            value={mode}
            onChange={(e) => setMode(e.target.value as PreReview | "")}
          >
            <option value="">{`${prereviewLabel(defaultMode)} (default)`}</option>
            {PREREVIEW.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        <Input
          label="Automated reviewer"
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="copilot"
          help="Its GitHub login. Jesinia waits up to 30 minutes for its review on a new head."
        />
        <Input
          label="Metrics"
          multiline
          value={metrics}
          onChange={(e) => setMetrics(e.target.value)}
          placeholder={"Security\nTests"}
          help="One criterion per line. Empty: the standards decide what is scored."
        />
        <p className={s.muted}>Changes apply from the repo's next review round.</p>
        <div className={s.actions}>
          <Button type="submit" size="sm" loading={saving}>
            {`Save ${config.repo}`}
          </Button>
        </div>
      </fieldset>
    </form>
  );
}

function FoldersForm({
  settings,
  save,
}: {
  settings: Settings;
  save: (patch: Partial<Settings>) => Promise<string | null>;
}) {
  const [checkout, setCheckout] = useState(settings.standards_dir ?? "");
  const [mine, setMine] = useState(settings.standards_user_dir ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(await save({ standards_dir: checkout.trim() || null, standards_user_dir: mine.trim() || null }));
    setSaving(false);
  };
  return (
    <form className={s.form} onSubmit={(e) => void submit(e)}>
      <div>
        <h3 className={s.settingTitle}>Standards</h3>
        <p className={s.muted}>
          Base, then the repo's own <code>.jesinia/review.md</code>, then yours: later layers win.
        </p>
      </div>
      <Input
        label="Coding-standards checkout"
        variant="path"
        value={checkout}
        onChange={(e) => setCheckout(e.target.value)}
        placeholder="~/projects/coding-standards"
        help="Your one checkout of the standards repo (the base layer)."
      />
      <Input
        label="Your own standards folder"
        variant="path"
        value={mine}
        onChange={(e) => setMine(e.target.value)}
        placeholder="~/standards"
        help="Markdown documents of your own rules (the last layer), read from the folder itself: subfolders and symlinks are skipped. Leave empty for none."
      />
      {error ? (
        <p className={s.error} role="alert">
          {error}
        </p>
      ) : null}
      <div className={s.actions}>
        <Button type="submit" size="sm" loading={saving}>
          Save folders
        </Button>
      </div>
    </form>
  );
}

function SettingsPanel() {
  const client = useClient();
  const toast = useToast();
  const prereviewId = useId();
  const runnerId = useId();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loadError, setLoadError] = useState<Load | null>(null);
  const [attemptNo, setAttemptNo] = useState(0);

  useEffect(() => {
    setLoadError(null);
    client.GET("/api/review/settings").then(
      (r) => {
        if (r.data) setSettings(r.data);
        else setLoadError({ cause: errorText(r.error, `HTTP ${r.response.status}`), answered: true });
      },
      (e: unknown) => setLoadError({ cause: unreachable(e), answered: false }),
    );
  }, [client, attemptNo]);

  // Writes go one at a time, and each sends only what it changes (I1): the server keeps every
  // repo not listed and each folder left out, so a repo confirmed in the queue or edited in
  // another tab since this page loaded is never put back. `prereview` and `runner` are
  // required: they come from the server's latest answer.
  const saved = useRef<Settings | null>(null);
  useEffect(() => {
    if (settings) saved.current = settings;
  }, [settings]);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const write = useCallback(
    (patch: Partial<Settings>) => {
      const run = () => {
        const latest = saved.current as Settings;
        const body: Settings = { prereview: latest.prereview, runner: latest.runner, repos: [], ...patch };
        return attempt(() => client.PUT("/api/review/settings", { params: { header: withIdempotency() }, body }));
      };
      const result = queue.current.then(run, run);
      queue.current = result;
      return result.then((outcome) => {
        if ("data" in outcome) {
          saved.current = outcome.data;
          setSettings(outcome.data);
        }
        return outcome;
      });
    },
    [client],
  );

  const change = async (patch: Partial<Settings>, done: (s: Settings) => string) => {
    const outcome = await write(patch);
    if ("data" in outcome) toast.show({ tone: "success", message: done(outcome.data) });
    else toast.show({ tone: "danger", message: `Couldn't save: ${outcome.error}` });
  };

  const saveFolders = async (patch: Partial<Settings>) => {
    const outcome = await write(patch);
    if ("error" in outcome) return outcome.error;
    toast.show({ tone: "success", message: "Standards folders saved." });
    return null;
  };

  const saveRepo = async (repo: RepoSetting) => {
    const outcome = await write({ repos: [repo] });
    if ("data" in outcome) {
      toast.show({ tone: "success", message: `${repo.repo} saved.` });
      return true;
    }
    toast.show({ tone: "danger", message: `Couldn't save ${repo.repo}: ${outcome.error}` });
    return false;
  };

  const watched = (settings?.repos ?? []).filter((r) => r.watched).length;
  const runners = settings ? [...new Set<string>([...RUNNERS, settings.runner])] : [];
  const mode = settings ? PREREVIEW.find((p) => p.value === settings.prereview) : undefined;

  return (
    <Panel
      title="Review"
      id="review"
      actions={
        settings ? (
          <span className={s.muted}>{`${watched} watched ${watched === 1 ? "repo" : "repos"}`}</span>
        ) : undefined
      }
    >
      {loadError ? (
        <LoadError what="the review settings" load={loadError} retry={() => setAttemptNo((n) => n + 1)} />
      ) : !settings ? (
        <p className={s.muted}>Loading…</p>
      ) : (
        <div className={s.form}>
          <div className={s.setting}>
            <div>
              <label htmlFor={prereviewId} className={s.settingTitle}>
                Pre-review
              </label>
              <p className={s.muted}>{mode?.help}</p>
            </div>
            <select
              id={prereviewId}
              className={s.select}
              value={settings.prereview}
              onChange={(e) =>
                void change(
                  { prereview: e.target.value as PreReview },
                  (saved) => `Pre-review: ${prereviewLabel(saved.prereview).toLowerCase()}.`,
                )
              }
            >
              {PREREVIEW.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
          <div className={s.setting}>
            <div>
              <label htmlFor={runnerId} className={s.settingTitle}>
                Agent
              </label>
              <p className={s.muted}>Runs the reviews, reading code only through Jesinia's read-only repo tools.</p>
            </div>
            <select
              id={runnerId}
              className={s.select}
              value={settings.runner}
              onChange={(e) =>
                void change(
                  { runner: e.target.value },
                  (saved) => `Agent: ${RUNNER_NAMES[saved.runner] ?? saved.runner}.`,
                )
              }
            >
              {runners.map((id) => (
                <option key={id} value={id}>
                  {RUNNER_NAMES[id] ?? id}
                </option>
              ))}
            </select>
          </div>
          <div className={s.setting}>
            <div>
              <h3 className={s.settingTitle}>Gates</h3>
              <p className={s.muted}>
                Before any review starts: CI must pass (or you choose Review anyway); a repo's automated reviewer gets
                up to 30 minutes; a new push waits for 10 quiet minutes.
              </p>
            </div>
          </div>
          {/* Starts again from what the server kept (a ~/ expanded, a change made elsewhere). */}
          <FoldersForm
            key={`${settings.standards_dir ?? ""}|${settings.standards_user_dir ?? ""}`}
            settings={settings}
            save={saveFolders}
          />
          <div className={s.form}>
            <div>
              <h3 className={s.settingTitle}>Repos</h3>
              <p className={s.muted}>
                Watched repos are pre-reviewed. A suggested repo is someone else's, listed because they asked for your
                review: nothing runs there until you watch it.
              </p>
            </div>
            {(settings.repos ?? []).length === 0 ? (
              <p className={s.muted}>No repos yet: they appear after the first sync with GitHub.</p>
            ) : (
              (settings.repos ?? []).map((repo) => (
                // A saved repo's form starts again from what the server kept; the others keep
                // their unsaved edits.
                <RepoForm key={JSON.stringify(repo)} config={repo} defaultMode={settings.prereview} save={saveRepo} />
              ))
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// --- What Jesinia thinks you know ----------------------------------------------------------------

/**
 * The folder an inherited mark is set on, in words: `repo/<owner>/<name>[/<path>]` as
 * `owner/name / path`. Only folders pass a mark down (a topic's mark is its own).
 */
function folderLabel(id: string): string {
  const [, owner = "", name = "", ...path] = id.split("/");
  const repo = `${owner}/${name}`;
  return path.length ? `${repo} / ${path.join("/")}` : repo;
}

/** A unit in words, from its own fields (a folder name may hold a slash-like character). */
const labelOf = (unit: Unit): string =>
  unit.kind === "tag" ? `#${unit.tag ?? ""}` : unit.path ? `${unit.repo ?? ""} / ${unit.path}` : (unit.repo ?? "");

const EVIDENCE: [keyof Unit["exposures"], string, string][] = [
  ["review_deep", "deep review", "deep reviews"],
  ["review_quick", "quick review", "quick reviews"],
  ["commit", "commit", "commits"],
  ["quiz_pass", "quiz passed", "quizzes passed"],
  ["quiz_fail", "quiz failed", "quizzes failed"],
];
const day = (iso: string) => new Date(iso).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });

/** Why the unit has the familiarity it has: a correction (its own or a folder's), or its evidence. */
function Source({ unit }: { unit: Unit }) {
  if (unit.override !== null) {
    const mark = unit.override === "known" ? "Known" : "Unknown";
    const at = unit.override_unit ?? unit.unit;
    if (at === unit.unit) return <>{`${mark}: you marked it`}</>;
    return (
      <>
        {`${mark}: marked on `}
        <VisibleText text={folderLabel(at)} />
      </>
    );
  }
  const counts = EVIDENCE.flatMap(([kind, one, many]) => {
    const n = unit.exposures[kind] ?? 0;
    return n ? [`${n} ${n === 1 ? one : many}`] : [];
  });
  if (counts.length === 0) return <>No record of its own: inferred from nearby areas</>;
  const last = unit.last_exposure ? ` · last ${day(unit.last_exposure)}` : "";
  const since = unit.changes_since_last ? ` · ${unit.changes_since_last} lines changed since` : "";
  return <>{`${counts.join(", ")}${last}${since}`}</>;
}

function KnowledgeRow({ unit, correct }: { unit: Unit; correct: (unit: Unit, to: Correction) => void }) {
  const label = labelOf(unit);
  const spoken = visibleText(label);
  const percent = Math.round(unit.familiarity * 100);
  const own: Correction =
    unit.override !== null && (unit.override_unit ?? unit.unit) === unit.unit ? unit.override : "";
  return (
    <li className={s.unit} aria-label={spoken}>
      <span className={s.unitName}>
        <VisibleText text={label} />
        {unit.kind === "tag" ? <span className={s.muted}> topic</span> : null}
      </span>
      <span
        className={s.familiarity}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-label={`${spoken}: ${percent}% familiar`}
      >
        <span style={{ width: `${percent}%` }} />
      </span>
      <select
        className={s.select}
        aria-label={`Correct ${spoken}`}
        value={own}
        onChange={(e) => correct(unit, e.target.value as Correction)}
      >
        <option value="">As Jesinia sees it</option>
        <option value="known">I know this</option>
        <option value="unknown">I don't know this</option>
      </select>
      <span className={s.unitSource}>
        <Source unit={unit} />
      </span>
    </li>
  );
}

/** Units read per page: every unit stays reachable through "Show more" (I4). */
const KNOWLEDGE_PAGE = 200;

function KnowledgePanel() {
  const client = useClient();
  const toast = useToast();
  const [knowledge, setKnowledge] = useState<Knowledge | null>(null);
  const [loadError, setLoadError] = useState<Load | null>(null);
  const [attemptNo, setAttemptNo] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const shown = useRef(0);
  shown.current = knowledge?.units.length ?? 0;

  /** Pages from `from` until `count` units are read or none follow; a failure as a Load. */
  const readPages = useCallback(
    async (from: number, count: number): Promise<Knowledge | Load> => {
      let units: Unit[] = [];
      let page: Knowledge | null = null;
      while (units.length < count) {
        const offset = from + units.length;
        const limit = Math.min(KNOWLEDGE_PAGE, count - units.length);
        try {
          const r = await client.GET("/api/review/knowledge", { params: { query: { offset, limit } } });
          if (!r.data) return { cause: errorText(r.error, `HTTP ${r.response.status}`), answered: true };
          page = r.data;
        } catch (e) {
          return { cause: unreachable(e), answered: false };
        }
        units = [...units, ...page.units];
        if (!page.truncated || page.units.length < limit) break; // the last page, or a short one
      }
      return { ...(page as Knowledge), units, offset: from, truncated: from + units.length < (page?.total ?? 0) };
    },
    [client],
  );

  // Only the newest read lands: a reload that answers late never overwrites a newer list.
  const latestRead = useRef(0);
  /** Read the model; `again`: after a correction (the pages shown so far, read again), when
   * a failure keeps the list shown. */
  const load = useCallback(
    async (again = false) => {
      const read = ++latestRead.current;
      const result = await readPages(0, again ? Math.max(shown.current, KNOWLEDGE_PAGE) : KNOWLEDGE_PAGE);
      if (read !== latestRead.current) return;
      if ("units" in result) setKnowledge(result);
      else if (again)
        toast.show({ tone: "danger", message: `Couldn't read the knowledge model again: ${result.cause}` });
      else setLoadError(result);
    },
    [readPages, toast],
  );

  const showMore = async () => {
    if (!knowledge) return;
    setLoadingMore(true);
    const read = latestRead.current;
    const result = await readPages(knowledge.units.length, KNOWLEDGE_PAGE);
    setLoadingMore(false);
    if (read !== latestRead.current) return; // a newer full read replaced the list
    if ("units" in result) setKnowledge((k) => k && { ...result, units: [...k.units, ...result.units], offset: 0 });
    else toast.show({ tone: "danger", message: `Couldn't read more of the knowledge model: ${result.cause}` });
  };
  useEffect(() => {
    setLoadError(null);
    void load();
  }, [load, attemptNo]);

  const correct = async (unit: Unit, to: Correction) => {
    const outcome = await attempt(() =>
      client.PUT("/api/review/knowledge/{unit}", {
        params: { path: { unit: unit.unit }, header: withIdempotency() },
        // A unit id is a path (`repo/<owner>/<name>/<folder>`): its slashes stay slashes, each
        // segment is encoded on its own.
        pathSerializer: (pathname) =>
          pathname.replace("{unit}", unit.unit.split("/").map(encodeURIComponent).join("/")),
        body: { override: to === "" ? null : to },
      }),
    );
    if ("error" in outcome) {
      toast.show({ tone: "danger", message: `Couldn't correct ${visibleText(labelOf(unit))}: ${outcome.error}` });
      return;
    }
    setKnowledge((k) => k && { ...k, units: k.units.map((u) => (u.unit === outcome.data.unit ? outcome.data : u)) });
    // A folder's mark reaches everything beneath it: read the model again.
    void load(true);
  };

  return (
    <Panel title="What Jesinia thinks you know" id="review-knowledge">
      {loadError ? (
        <LoadError what="what Jesinia thinks you know" load={loadError} retry={() => setAttemptNo((n) => n + 1)} />
      ) : !knowledge ? (
        <p className={s.muted}>Loading…</p>
      ) : (
        <div className={s.form}>
          <p className={s.muted}>
            It sets how deep a review goes and how much background it shows. Correct it any time: a mark on a folder
            covers everything beneath it.
          </p>
          <p className={s.muted}>KB pages aren't counted yet.</p>
          {knowledge.units.length === 0 ? (
            <p className={s.muted}>
              Nothing yet: Jesinia learns from your commits, the reviews you finish and your quiz results.
            </p>
          ) : (
            <ul className={s.units}>
              {knowledge.units.map((unit) => (
                <KnowledgeRow key={unit.unit} unit={unit} correct={(u, to) => void correct(u, to)} />
              ))}
            </ul>
          )}
          {knowledge.truncated ? (
            <div className={s.inline}>
              <p className={s.muted}>{`Showing ${knowledge.units.length} of ${knowledge.total} areas.`}</p>
              {loadingMore ? (
                <span className={s.muted}>Loading more…</span>
              ) : (
                <Button size="sm" variant="secondary" onClick={() => void showMore()}>
                  Show more
                </Button>
              )}
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function ReviewSection() {
  return (
    <>
      <SettingsPanel />
      <KnowledgePanel />
    </>
  );
}
