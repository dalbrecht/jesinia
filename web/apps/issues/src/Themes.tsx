// SPDX-License-Identifier: AGPL-3.0-or-later
// Themes (spec §6.1, rulings IR15, IR22, IR54, IR56, IR59): tags from the KB-wide catalog, kept
// locally per issue. They show as a column of drop targets beside the list and the board, and as
// a filter. A drop (or "Add theme…") is local and immediate, with Undo, which takes the theme off
// the issues only (the catalog keeps the tag). Publish stages the `theme:<tag>` label in the tray;
// To epic previews one batch (the epic, then each issue linked under it) to approve.
import { Button, DropTarget, Input, useToast, type DragItem } from "@jesinia/design";
import { useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActionMenu, type MenuOption } from "./ActionMenu";
import {
  ActionKeys,
  addTheme,
  approveThemeEpic,
  errorCause,
  getTheme,
  getThemeEpic,
  getThemes,
  isRefusal,
  publishTheme,
  epicOfRefusal,
  refOf,
  removeTheme,
  type IssueTarget,
  type Theme,
  type ThemeEpicPlan,
} from "./api";
import type { DropAction } from "./drops";
import s from "./issues.module.css";
import { PlanDiff } from "./Plans";
import { useTray } from "./Tray";

const plural = (n: number, one: string, other: string) => `${n} ${n === 1 ? one : other}`;
const targetsOf = (items: readonly DragItem[]): IssueTarget[] =>
  items.map((item) => {
    const row = item.data as { repo: string; number: number };
    return { repo: row.repo, n: row.number };
  });

/** Every theme (the column's targets), read again after each add. */
export function useThemes() {
  const client = useClient();
  const toast = useToast();
  const [themes, setThemes] = useState<Theme[]>([]);
  const reload = useCallback(async () => {
    try {
      setThemes(await getThemes(client));
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't read the themes: ${errorCause(e)}` });
    }
  }, [client, toast]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { themes, reload };
}

/** Give issues a theme at once, with an Undo that takes it off those issues again (IR22). */
export function useAddTheme(onChanged: () => void) {
  const client = useClient();
  const toast = useToast();
  const keys = useRef(new ActionKeys());

  const keyed = useCallback(async <T,>(action: string, work: (key: string) => Promise<T>): Promise<T> => {
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    }
  }, []);

  return useCallback(
    async (tag: string, issues: IssueTarget[]) => {
      const names = issues.map((i) => `${i.repo}#${i.n}`).join(", ");
      try {
        const added = await keyed(`theme:${tag}:${names}`, (key) => addTheme(client, tag, issues, key));
        onChanged();
        if (added.added.length === 0) {
          toast.show({ tone: "info", message: `${names} had #${added.tag} already.` });
          return;
        }
        const which = added.added.map((i) => `${i.repo}#${i.n}`).join(", ");
        const undo = async () => {
          try {
            await keyed(`untheme:${added.tag}:${which}`, (key) => removeTheme(client, added.tag, added.added, key));
            toast.show({ tone: "info", message: `Took #${added.tag} off ${which} again.` });
          } catch (e) {
            toast.show({ tone: "danger", message: `Couldn't undo the theme: ${errorCause(e)}` });
          }
          onChanged();
        };
        toast.show({
          tone: "success",
          message: `Added #${added.tag} to ${which}. It stays in Jesinia until you publish it.`,
          action: { label: "Undo", onAction: () => void undo() },
        });
      } catch (e) {
        toast.show({ tone: "danger", message: `Couldn't add #${tag}: ${errorCause(e)}` });
      }
    },
    [client, keyed, onChanged, toast],
  );
}

/** "Add theme…": the menu equivalent of a drop on a theme, with a new tag allowed. */
export function AddThemeMenu({
  targets,
  themes,
  name,
  onAdd,
}: {
  targets: IssueTarget[];
  themes: Theme[];
  name: string;
  onAdd: (tag: string, issues: IssueTarget[]) => void;
}) {
  return (
    <ActionMenu
      label="Add theme…"
      name={name}
      options={themes.map((t) => ({ id: t.tag, label: `#${t.tag}` }))}
      onPick={(tag) => onAdd(tag, targets)}
      create={{ label: "Name a new theme", onCreate: (tag) => onAdd(tag.replace(/^#/, ""), targets) }}
      emptyText="No themes yet: name a new one."
    />
  );
}

type EpicDraft = {
  tag: string;
  repo: string;
  newEpic: boolean;
  plan: ThemeEpicPlan | null;
  problem: string | null;
  /** The theme's epic is closed: reopen it on GitHub, or ask for a new epic (IR59). */
  closed: boolean;
  error: unknown;
};

/** The closed epic a ThemeEpicClosedError names. */
export function closedEpicOf(e: unknown): { repo: string; number: number } | null {
  return epicOfRefusal(e, "ThemeEpicClosedError");
}

function ReopenLink({ problem }: { problem: unknown }) {
  const closed = closedEpicOf(problem);
  if (closed === null) return null;
  return (
    <a
      className={s.linkButton}
      href={`https://github.com/${closed.repo}/issues/${closed.number}`}
      target="_blank"
      rel="noopener noreferrer"
    >
      {`Reopen ${closed.repo}#${closed.number} on GitHub`}
    </a>
  );
}

export type ThemesColumnProps = {
  themes: Theme[];
  /** The theme the list is filtered by, or null. */
  filter: string | null;
  onFilter: (tag: string | null, refs: ReadonlySet<string> | null) => void;
  /** The list's selected issues, for the new-theme field. */
  selected: IssueTarget[];
  onAdd: (tag: string, issues: IssueTarget[]) => void;
};

export function ThemesColumn({ themes, filter, onFilter, selected, onAdd }: ThemesColumnProps) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const keys = useRef(new ActionKeys());
  const [draft, setDraft] = useState("");
  const [epic, setEpic] = useState<EpicDraft | null>(null);
  const [busy, setBusy] = useState(false);

  const repoOptions = useCallback(
    (tag: string) => async (): Promise<MenuOption[]> => {
      const found = await getTheme(client, tag);
      const repos = [...new Set(found.issues.map((i) => i.repo))];
      return repos.map((repo) => ({ id: repo, label: repo }));
    },
    [client],
  );

  const toggleFilter = async (tag: string) => {
    if (filter === tag) {
      onFilter(null, null);
      return;
    }
    try {
      const found = await getTheme(client, tag);
      onFilter(found.tag, new Set(found.issues.map((i) => refOf({ repo: i.repo, number: i.n }).toLowerCase())));
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't filter by #${tag}: ${errorCause(e)}` });
    }
  };

  const publish = async (tag: string, repo: string) => {
    const action = `publish:${tag}:${repo}`;
    try {
      const published = await publishTheme(client, tag, repo, keys.current.keyFor(action));
      keys.current.settle(action);
      await tray.refresh();
      toast.show({
        tone: "info",
        message: `Staged the label ${published.label} for ${plural(published.added.length, "issue", "issues")} in ${repo}. Nothing is sent until you apply.`, // prettier-ignore
      });
    } catch (e) {
      keys.current.settle(action, e);
      toast.show({ tone: "danger", message: `Couldn't publish #${tag}: ${errorCause(e)}` });
    }
  };

  const preview = async (tag: string, repo: string, newEpic: boolean) => {
    const base: EpicDraft = { tag, repo, newEpic, plan: null, problem: null, closed: false, error: null };
    setEpic(base);
    try {
      setEpic({ ...base, plan: await getThemeEpic(client, tag, repo, newEpic) });
    } catch (e) {
      setEpic({ ...base, problem: errorCause(e), closed: isRefusal(e, "ThemeEpicClosedError"), error: e });
    }
  };

  const approve = async (current: EpicDraft & { plan: ThemeEpicPlan }) => {
    const action = `theme-epic:${current.tag}:${current.repo}:${current.plan.digest}`;
    setBusy(true);
    try {
      const batch = await approveThemeEpic(
        client,
        current.tag,
        { repo: current.repo, digest: current.plan.digest, new_epic: current.newEpic },
        keys.current.keyFor(action),
      );
      keys.current.settle(action);
      tray.follow(batch, `Theme #${current.tag} to an epic in ${current.repo}`);
      setEpic(null);
    } catch (e) {
      keys.current.settle(action, e);
      tray.followEarlier(e);
      setEpic({ ...current, problem: errorCause(e), closed: isRefusal(e, "ThemeEpicClosedError"), error: e });
    } finally {
      setBusy(false);
    }
  };

  const submitNew = () => {
    const tag = draft.trim().replace(/^#/, "");
    if (!tag || selected.length === 0) return;
    setDraft("");
    onAdd(tag, selected);
  };

  return (
    <aside className={s.themes} aria-label="Themes">
      <h2 className={s.kicker}>Themes</h2>
      {themes.length === 0 ? <p className={s.muted}>Drop issues on a theme to group them. Name one below.</p> : null}
      <ul className={s.plain}>
        {themes.map((t) => {
          const data: DropAction = { drop: (items) => onAdd(t.tag, targetsOf(items)) };
          return (
            <li key={t.tag}>
              <DropTarget
                id={`theme:${t.tag}`}
                label={`#${t.tag}`}
                data={data}
                className={s.themeTarget}
                role="group"
                aria-label={`#${t.tag}`}
              >
                <button
                  type="button"
                  className={s.linkButton}
                  aria-pressed={filter === t.tag}
                  aria-label={`Filter by #${t.tag}`}
                  onClick={() => void toggleFilter(t.tag)}
                >
                  #<VisibleText text={t.tag} />
                </button>
                <span className={s.muted}>{t.count}</span>
                <span className={s.spacer} />
                <ActionMenu
                  label="Publish…"
                  name={`Publish #${t.tag} as a label`}
                  options={repoOptions(t.tag)}
                  onPick={(repo) => void publish(t.tag, repo)}
                  disabled={!t.publishable}
                />
                <ActionMenu
                  label="To epic…"
                  name={`Turn #${t.tag} into an epic`}
                  options={repoOptions(t.tag)}
                  onPick={(repo) => void preview(t.tag, repo, false)}
                />
              </DropTarget>
              {!t.publishable ? <p className={s.muted}>Too long for a GitHub label: it stays a local theme.</p> : null}
            </li>
          );
        })}
      </ul>
      <form
        className={s.menuCreate}
        onSubmit={(e) => {
          e.preventDefault();
          submitNew();
        }}
      >
        <Input
          label="New theme"
          hideLabel
          placeholder="New theme"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <Button size="sm" type="submit" disabled={draft.trim() === "" || selected.length === 0}>
          Add to selected
        </Button>
      </form>
      {epic ? (
        <div className={s.themeEpic}>
          {epic.plan === null && epic.problem === null ? <p className={s.muted}>Planning the epic…</p> : null}
          {epic.plan === null && epic.problem !== null ? (
            <div className={s.plan} role="region" aria-label={`#${epic.tag} to an epic`}>
              <p className={s.planProblem} role="alert">
                {epic.problem}
              </p>
              <div className={s.planActions}>
                {epic.closed ? (
                  <Button size="sm" onClick={() => void preview(epic.tag, epic.repo, true)}>
                    Ask for a new epic
                  </Button>
                ) : null}
                <ReopenLink problem={epic.error} />
                <Button size="sm" variant="ghost" onClick={() => setEpic(null)}>
                  Close
                </Button>
              </div>
            </div>
          ) : null}
          {epic.plan !== null ? (
            <PlanDiff
              title={`#${epic.tag} to an epic in ${epic.repo}`}
              epic={epic.plan.epic}
              children={epic.plan.children}
              skipped={epic.plan.skipped}
              busy={busy}
              problem={epic.problem}
              onApprove={() => void approve({ ...epic, plan: epic.plan as ThemeEpicPlan })}
              onCancel={() => setEpic(null)}
              actions={
                epic.closed ? (
                  <>
                    <Button size="sm" onClick={() => void preview(epic.tag, epic.repo, true)}>
                      Ask for a new epic
                    </Button>
                    <ReopenLink problem={epic.error} />
                  </>
                ) : null
              }
            />
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}
