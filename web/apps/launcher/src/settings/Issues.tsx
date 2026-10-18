// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Issues (Issues spec §2 and §5, plan Task 14): the runner that answers suggestion
// asks, the watched repos, and per repo its label schema (where it comes from and its aliases)
// with the label setup and migration batches, each previewed first and applied as an approval.
// Every save sends only the fields it changes, with a fresh Idempotency-Key, so a save never puts
// back what another tab changed. Label names are anyone's to write: they render as text, with
// hidden and direction-changing characters as visible tokens (R62).
import { errorText, withIdempotency, type components } from "@jesinia/client";
import { Badge, Button, Input, Panel, useToast } from "@jesinia/design";
import { RUNNER_NAMES, useClient, VisibleText, type ServerStatus } from "@jesinia/shell";
import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import s from "../settings.module.css";
import { sentence, unreachable } from "./unreachable";

type Repo = components["schemas"]["IssuesRepoOut"];
type RepoPatch = components["schemas"]["IssuesRepoPatch"];
type Schema = components["schemas"]["LabelSchemaBody"];
type Canonical = components["schemas"]["LabelCanonicalBody"];
type Plan = components["schemas"]["LabelPlanOut"];
type Op = Plan["ops"][number];
type Applied = components["schemas"]["LabelApplyOut"];
type Kind = Plan["kind"];

type Outcome<T> = { data: T } | { error: string };

/** A request's answer, or the words for why it failed (the server's, or why it got none). */
async function attempt<T>(
  request: () => Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<Outcome<T>> {
  try {
    const r = await request();
    if (r.data !== undefined) return { data: r.data };
    return { error: errorText(r.error, `HTTP ${r.response.status}`) };
  } catch (e) {
    return { error: unreachable(e) };
  }
}

const REPO_NAME = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const SOURCE: Record<Repo["schema_source"], string> = {
  repo: "From the repo's .jesinia/labels.yml (read-only here)",
  settings: "Made here in Settings",
  default: "The default label convention",
};
const DIMENSIONS = [
  ["readiness", "Readiness"],
  ["priority", "Priority"],
  ["type", "Type"],
] as const;
const KIND: Record<Kind, string> = { setup: "Setup", migrate: "Migration" };
const STEP: Record<Applied["steps"][number]["state"], string> = { done: "Done", failed: "Failed", skipped: "Skipped" };

const pathOf = (repo: string) => {
  const [owner = "", name = ""] = repo.split("/");
  return { owner, repo: name };
};
const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
const issuesCount = (n: number) => `${n} ${n === 1 ? "issue" : "issues"}`;

/** One step of a plan in words; every label name rendered with R62 tokens. */
function OpText({ op }: { op: Op }) {
  if (op.op === "create")
    return (
      <>
        <Badge tone="success" icon="+">
          Create
        </Badge>{" "}
        <VisibleText text={op.name} />
      </>
    );
  if (op.replacement === null)
    return (
      <>
        <Badge tone="warn" icon="−">
          Delete
        </Badge>{" "}
        <VisibleText text={op.name} />
      </>
    );
  return (
    <>
      {`Move ${issuesCount(op.members.length)} from `}
      <VisibleText text={op.name} />
      {" to "}
      <VisibleText text={op.replacement} />
      {", then delete "}
      <VisibleText text={op.name} />
    </>
  );
}

/** A previewed batch: its steps, then Apply (an approval) and each step's outcome. */
function PlanView({ repo, plan }: { repo: string; plan: Plan }) {
  const client = useClient();
  // One key per plan: a retry after no answer is the same approval, never a second batch.
  const [key] = useState(() => withIdempotency());
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<Applied | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const kind = KIND[plan.kind];

  const apply = async () => {
    setApplying(true);
    setProblem(null);
    try {
      const r = await client.POST("/api/issues/labels/{owner}/{repo}/apply", {
        params: { path: pathOf(repo), header: key },
        body: { kind: plan.kind, plan_id: plan.plan_id },
      });
      // A 503 still carries the steps so far (GitHub stopped the batch).
      const body = (r.data ?? r.error) as Applied | { detail?: unknown } | undefined;
      if (body && "steps" in body) setResult(body);
      else setProblem(errorText(r.error, `HTTP ${r.response.status}`));
    } catch (e) {
      setProblem(unreachable(e));
    } finally {
      setApplying(false);
    }
  };

  const outcomes = new Map((result?.steps ?? []).map((step, i) => [i, step]));
  return (
    <section className={s.form} role="region" aria-label={`${kind} for ${repo}`}>
      {plan.kind === "migrate" ? (
        <p className={s.muted}>
          Each old label's issues are listed again, live from GitHub, when you apply, and each moves to its canonical
          label. An old label is deleted only if every move succeeded.
        </p>
      ) : null}
      {plan.ops.length === 0 ? (
        <p className={s.muted}>
          {plan.kind === "setup"
            ? "Nothing to do: every canonical label exists."
            : "Nothing to do: no alias exists on GitHub."}
        </p>
      ) : (
        <ul className={s.list}>
          {plan.ops.map((op, i) => {
            const done = outcomes.get(i);
            return (
              <li key={`${op.op}:${op.name}`} className={s.item}>
                <span>
                  <OpText op={op} />
                </span>
                {op.skip_cause && !done ? (
                  <span className={s.error}>{`Skipped: ${sentence(op.skip_cause)}`}</span>
                ) : null}
                {done ? (
                  <span className={done.state === "done" ? s.muted : s.error}>
                    {done.cause ? `${STEP[done.state]}: ${done.cause}` : STEP[done.state]}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {result ? (
        <p className={s.outcome} role="status">
          {result.complete
            ? `${kind} applied: every step done.`
            : `${kind} stopped. ${result.stopped ? sentence(result.stopped) : ""} ${result.note ?? ""} Preview again for what is left.`}
        </p>
      ) : null}
      {problem ? (
        <p className={s.error} role="alert">
          {problem}
        </p>
      ) : null}
      {plan.ops.length > 0 && result === null ? (
        <div className={s.actions}>
          <Button size="sm" variant="primary" loading={applying} onClick={() => void apply()}>
            {`Apply ${kind.toLowerCase()}`}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

/** The editable copy of a schema: each value's aliases as lines of text. */
type Draft = { dimensions: Record<(typeof DIMENSIONS)[number][0], Canonical[]>; areas: Canonical[] };
const draftOf = (schema: Schema): Draft => ({
  dimensions: { readiness: schema.readiness, priority: schema.priority, type: schema.type },
  areas: schema.areas ?? [],
});

function CanonicalRow({
  value,
  editable,
  onAliases,
  onRemove,
}: {
  value: Canonical;
  editable: boolean;
  onAliases: (aliases: string[]) => void;
  onRemove?: () => void;
}) {
  const aliases = value.aliases ?? [];
  const [text, setText] = useState(aliases.join("\n"));
  return (
    <li className={s.item}>
      <strong>
        <VisibleText text={value.name} />
      </strong>
      {editable ? (
        <Input
          label={`Aliases of ${value.name}`}
          hideLabel
          multiline
          value={text}
          placeholder="Existing labels that mean the same, one per line"
          onChange={(e) => {
            setText(e.target.value);
            onAliases(lines(e.target.value));
          }}
        />
      ) : (
        <span className={s.muted}>
          {aliases.length === 0
            ? "no aliases"
            : aliases.map((alias, i) => (
                <span key={alias}>
                  {i > 0 ? ", " : ""}
                  <VisibleText text={alias} />
                </span>
              ))}
        </span>
      )}
      {editable && onRemove ? (
        <Button size="sm" variant="ghost" aria-label={`Remove ${value.name}`} onClick={onRemove}>
          Remove
        </Button>
      ) : null}
    </li>
  );
}

/** One repo: watched, its schema and aliases, and the label batches. */
function RepoForm({ config, save }: { config: Repo; save: (change: RepoPatch) => Promise<boolean> }) {
  const client = useClient();
  const editable = config.schema_source !== "repo";
  const base = config.settings_schema ?? config.label_schema;
  const [draft, setDraft] = useState<Draft>(() => draftOf(base));
  const [newArea, setNewArea] = useState("");
  const [saving, setSaving] = useState(false);
  // Each preview is a fresh plan view, even when the server answers the same plan again.
  const [plan, setPlan] = useState<{ plan: Plan; n: number } | null>(null);
  const [previewing, setPreviewing] = useState<Kind | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const setAliases = (dimension: keyof Draft["dimensions"], index: number, aliases: string[]) =>
    setDraft((d) => ({
      ...d,
      dimensions: {
        ...d.dimensions,
        [dimension]: d.dimensions[dimension].map((v, i) => (i === index ? { ...v, aliases } : v)),
      },
    }));
  const setAreaAliases = (index: number, aliases: string[]) =>
    setDraft((d) => ({ ...d, areas: d.areas.map((v, i) => (i === index ? { ...v, aliases } : v)) }));

  const addArea = (e: FormEvent) => {
    e.preventDefault();
    const name = newArea.trim();
    if (!name) return;
    setDraft((d) => ({ ...d, areas: [...d.areas, { name, aliases: [] }] }));
    setNewArea("");
  };

  const saveSchema = async () => {
    setSaving(true);
    const schema: Schema = { ...base, ...draft.dimensions, areas: draft.areas };
    await save({ settings_schema: schema });
    setSaving(false);
  };

  const preview = async (kind: Kind) => {
    setPreviewing(kind);
    setPlan(null);
    setProblem(null);
    const outcome = await attempt(() =>
      client.POST("/api/issues/labels/{owner}/{repo}/preview", {
        params: { path: pathOf(config.repo) },
        body: { kind },
      }),
    );
    setPreviewing(null);
    if ("data" in outcome) setPlan((prev) => ({ plan: outcome.data, n: (prev?.n ?? 0) + 1 }));
    else setProblem(outcome.error);
  };

  return (
    <fieldset className={s.repo}>
      <legend className={s.repoName}>{config.repo}</legend>
      <label className={s.check}>
        <input type="checkbox" checked={config.watched} onChange={(e) => void save({ watched: e.target.checked })} />
        Watched
      </label>
      {config.sync_error ? <p className={s.error}>{`Last sync failed: ${config.sync_error}`}</p> : null}
      <div>
        <h3 className={s.settingTitle}>Label schema</h3>
        <p className={s.muted}>{SOURCE[config.schema_source]}</p>
        {config.schema_errors.length > 0 ? (
          <ul className={s.plainList}>
            {config.schema_errors.map((error) => (
              <li key={error} className={s.error}>
                {error}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      {DIMENSIONS.map(([dimension, title]) => (
        <div key={dimension}>
          <h4 className={s.settingTitle}>{title}</h4>
          <ul className={s.list}>
            {draft.dimensions[dimension].map((v, i) => (
              <CanonicalRow
                key={v.name}
                value={v}
                editable={editable}
                onAliases={(aliases) => setAliases(dimension, i, aliases)}
              />
            ))}
          </ul>
        </div>
      ))}
      <div>
        <h4 className={s.settingTitle}>Areas</h4>
        <ul className={s.list}>
          {draft.areas.map((v, i) => (
            <CanonicalRow
              key={v.name}
              value={v}
              editable={editable}
              onAliases={(aliases) => setAreaAliases(i, aliases)}
              onRemove={() => setDraft((d) => ({ ...d, areas: d.areas.filter((_, j) => j !== i) }))}
            />
          ))}
        </ul>
        {editable ? (
          <form className={s.actions} onSubmit={addArea}>
            <Input
              label="New area"
              hideLabel
              placeholder="area:name"
              value={newArea}
              onChange={(e) => setNewArea(e.target.value)}
            />
            <Button size="sm" type="submit">
              Add area
            </Button>
          </form>
        ) : null}
      </div>
      {editable ? (
        <div className={s.actions}>
          <Button size="sm" loading={saving} onClick={() => void saveSchema()}>
            {`Save labels for ${config.repo}`}
          </Button>
          {config.settings_schema !== null ? (
            <Button size="sm" variant="ghost" onClick={() => void save({ settings_schema: null })}>
              {`Use the default for ${config.repo}`}
            </Button>
          ) : null}
        </div>
      ) : null}
      <div>
        <h3 className={s.settingTitle}>Labels on GitHub</h3>
        <p className={s.muted}>
          Jesinia never creates a label on its own. Setup creates the canonical labels the repo lacks; migration moves
          issues off their aliases and deletes the old labels. Both are previewed first.
          {config.migrated ? " This repo has been migrated." : ""}
        </p>
      </div>
      <div className={s.actions}>
        <Button size="sm" loading={previewing === "setup"} onClick={() => void preview("setup")}>
          {`Preview setup for ${config.repo}`}
        </Button>
        <Button size="sm" loading={previewing === "migrate"} onClick={() => void preview("migrate")}>
          {`Preview migration for ${config.repo}`}
        </Button>
      </div>
      {problem ? (
        <p className={s.error} role="alert">
          {problem}
        </p>
      ) : null}
      {plan ? <PlanView key={plan.n} repo={config.repo} plan={plan.plan} /> : null}
    </fieldset>
  );
}

type Load = { cause: string; answered: boolean };

/** The section: the runner, adding a repo, and each repo's form. */
export function IssuesSection({ status }: { status: ServerStatus }) {
  const client = useClient();
  const toast = useToast();
  const runnerId = useId();
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [runner, setRunner] = useState<string | null | undefined>(undefined);
  const [loadError, setLoadError] = useState<Load | null>(null);
  const [attemptNo, setAttemptNo] = useState(0);
  const [adding, setAdding] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setLoadError(null);
    void (async () => {
      try {
        const [r, settings] = await Promise.all([client.GET("/api/issues/repos"), client.GET("/api/issues/settings")]);
        if (!r.data) {
          setLoadError({ cause: errorText(r.error, `HTTP ${r.response.status}`), answered: true });
          return;
        }
        setRepos(r.data.repos);
        setRunner(settings.data ? settings.data.runner : null);
      } catch (e) {
        setLoadError({ cause: unreachable(e), answered: false });
      }
    })();
  }, [client, attemptNo]);

  const saveRepo = useCallback(
    async (name: string, change: RepoPatch): Promise<Outcome<Repo>> => {
      const outcome = await attempt(() =>
        client.PATCH("/api/issues/repos/{owner}/{repo}", {
          params: { path: pathOf(name), header: withIdempotency() },
          body: change,
        }),
      );
      if ("data" in outcome) {
        const saved = outcome.data;
        setRepos((list) => {
          const others = (list ?? []).filter((r) => r.repo.toLowerCase() !== saved.repo.toLowerCase());
          const at = (list ?? []).findIndex((r) => r.repo.toLowerCase() === saved.repo.toLowerCase());
          if (at < 0) return [...others, saved];
          return [...others.slice(0, at), saved, ...others.slice(at)];
        });
      }
      return outcome;
    },
    [client],
  );

  const save = async (name: string, change: RepoPatch) => {
    const outcome = await saveRepo(name, change);
    if ("data" in outcome) {
      toast.show({ tone: "success", message: `${outcome.data.repo} saved.` });
      return true;
    }
    toast.show({ tone: "danger", message: `Couldn't save ${name}: ${outcome.error}` });
    return false;
  };

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const name = adding.trim();
    if (!REPO_NAME.test(name)) {
      setAddError("Write the repo as owner/name.");
      return;
    }
    setAddError(null);
    setBusy(true);
    const outcome = await saveRepo(name, { watched: true });
    setBusy(false);
    if ("error" in outcome) {
      setAddError(`Couldn't watch ${name}: ${outcome.error}`);
      return;
    }
    setAdding("");
    toast.show({ tone: "success", message: `Watching ${outcome.data.repo}: its open issues land in the Backlog.` });
  };

  const chooseRunner = async (picked: string) => {
    const outcome = await attempt(() =>
      client.PATCH("/api/issues/settings", {
        params: { header: withIdempotency() },
        body: { runner: picked === "" ? null : picked },
      }),
    );
    if ("data" in outcome) {
      setRunner(outcome.data.runner);
      const name = outcome.data.runner;
      toast.show({ tone: "success", message: `Suggestions: ${name ? (RUNNER_NAMES[name] ?? name) : "automatic"}.` });
    } else toast.show({ tone: "danger", message: `Couldn't save the runner: ${outcome.error}` });
  };

  const runners = [...new Set([...Object.keys(status.runners ?? {}), ...(runner ? [runner] : [])])];
  const watched = (repos ?? []).filter((r) => r.watched).length;

  return (
    <Panel
      title="Issues"
      id="issues"
      actions={
        repos ? <span className={s.muted}>{`${watched} watched ${watched === 1 ? "repo" : "repos"}`}</span> : undefined
      }
    >
      {loadError ? (
        <div className={s.form}>
          <p className={s.muted}>
            {`Couldn't load the Issues settings: ${sentence(loadError.cause)}`}
            {loadError.answered ? " Restart Jesinia; jesinia status shows why." : null}
          </p>
          <div className={s.actions}>
            <Button size="sm" onClick={() => setAttemptNo((n) => n + 1)}>
              Try again
            </Button>
          </div>
        </div>
      ) : repos === null ? (
        <p className={s.muted}>Loading…</p>
      ) : (
        <div className={s.form}>
          <div className={s.setting}>
            <div>
              <label htmlFor={runnerId} className={s.settingTitle}>
                Suggestions runner
              </label>
              <p className={s.muted}>
                The agent that suggests labels, priorities, epics and queue orders. It never changes GitHub: you approve
                each suggestion.
              </p>
            </div>
            <select
              id={runnerId}
              className={s.select}
              value={runner ?? ""}
              onChange={(e) => void chooseRunner(e.target.value)}
            >
              <option value="">Automatic (the first available)</option>
              {runners.map((id) => (
                <option key={id} value={id}>
                  {RUNNER_NAMES[id] ?? id}
                </option>
              ))}
            </select>
          </div>
          <form className={s.form} onSubmit={(e) => void add(e)}>
            <Input
              label="Add a repo"
              value={adding}
              placeholder="owner/name"
              onChange={(e) => setAdding(e.target.value)}
              error={addError ?? undefined}
              help="Its open issues land in the Backlog; issues opened after this land in the Inbox."
            />
            <div className={s.actions}>
              <Button type="submit" size="sm" loading={busy}>
                Watch
              </Button>
            </div>
          </form>
          {repos.length === 0 ? (
            <p className={s.muted}>No repos yet: add one above.</p>
          ) : (
            repos.map((r) => (
              // A saved schema starts the form again from what the server kept; a watched toggle
              // keeps the unsaved alias edits.
              <RepoForm
                key={`${r.repo}|${JSON.stringify(r.settings_schema ?? r.label_schema)}`}
                config={r}
                save={(change) => save(r.repo, change)}
              />
            ))
          )}
        </div>
      )}
    </Panel>
  );
}
