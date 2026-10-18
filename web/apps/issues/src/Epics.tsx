// SPDX-License-Identifier: AGPL-3.0-or-later
// The Epics tab (spec §4, §6, §6.1): the epics and initiatives with progress (children done of
// all) and readiness, each a drop target and itself draggable; beside them the open issues
// without an epic, to drag onto one, and the agent's group proposals. A drop on an epic stages
// one parent link per issue (ruling IR39), never a write; a drop the epic can't take (into
// itself or its own child, an initiative into an epic, another owner's issue: IR17) is refused
// with the reason before anything is staged, and an issue from another repo is warned "won't fit
// one PR". "Move to epic…" is the menu equivalent, with the same refusals. An epic opens in its own
// workspace tab (`EpicTab`, PR 4 final review I1), with the same layout wherever it is opened from.
import { Badge, Button, Draggable, DropTarget, useToast, type DragItem, type Refusal } from "@jesinia/design";
import { useClient, VisibleText, visibleText } from "@jesinia/shell";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActionMenu, type MenuOption } from "./ActionMenu";
import {
  askGroups,
  containmentRefusal,
  errorCause,
  getEpics,
  getView,
  groupsScope,
  readinessText,
  refOf,
  setParentChange,
  type Epic,
  type IssueRow,
  type Repo,
} from "./api";
import { useDrafts, useProposalApproval } from "./drafts";
import type { DropAction } from "./drops";
import { Drops } from "./drops";
import { EpicView } from "./EpicView";
import s from "./issues.module.css";
import { ProposalCard } from "./Plans";
import { AddToQueueMenu } from "./Queues";
import { useTray, type Staging } from "./Tray";

/** Anything that can go under an epic: an issue row, an epic, an epic's child. */
export type Linkable = { repo: string; number: number; parent: string | null; is_initiative?: boolean; title?: string | null }; // prettier-ignore
type Holder = { repo: string; number: number; is_initiative: boolean; title: string };

const lower = (ref: string) => ref.toLowerCase();

/** The cached parent of each ref (lowercased), from every epic and issue in view. */
export function parentsOf(...lists: readonly (readonly Linkable[])[]): (ref: string) => string | null {
  const parents = new Map<string, string>();
  for (const list of lists)
    for (const item of list) if (item.parent) parents.set(lower(refOf(item)), lower(item.parent));
  return (ref) => parents.get(ref) ?? null;
}

const UNCHANGED: Refusal = { code: "unchanged", reason: "Already in this epic" };

/** Why `epic` refuses `items`, or null: the first refusal of any of them (IR17's shape). */
export function epicRefusal(
  epic: Holder,
  items: readonly Linkable[],
  parentOf: (ref: string) => string | null,
): Refusal | null {
  for (const item of items) {
    const refused = containmentRefusal(epic, item, parentOf);
    if (refused) return refused;
  }
  const own = lower(refOf(epic));
  if (items.length > 0 && items.every((item) => item.parent && lower(item.parent) === own)) return UNCHANGED;
  return null;
}

/** One parent link per issue, each against the parent it has now (IR39). */
export function linksUnder(epic: { repo: string; number: number }, items: readonly Linkable[], source: Staging["source"]): Staging[] {
  const own = lower(refOf(epic));
  return items
    .filter((item) => item.parent === null || lower(item.parent) !== own)
    .map((item) => ({ repo: item.repo, n: item.number, change: setParentChange({ repo: epic.repo, n: epic.number }, item.parent), source })); // prettier-ignore
} // prettier-ignore

/** Stage `items` under `epic`, warning when one sits in another repo ("won't fit one PR"). */
export function useLinkUnder() {
  const tray = useTray();
  const toast = useToast();
  return useCallback(
    async (epic: { repo: string; number: number }, items: readonly Linkable[], source: Staging["source"]) => {
      const elsewhere = items.filter((item) => lower(item.repo) !== lower(epic.repo)).map(refOf);
      await tray.stageAll(linksUnder(epic, items, source));
      if (elsewhere.length > 0) {
        toast.show({ tone: "warn", message: `Won't fit one PR: ${elsewhere.join(", ")} ${elsewhere.length === 1 ? "is" : "are"} in another repo than ${refOf(epic)}.` }); // prettier-ignore
      }
    },
    [tray, toast],
  );
}

const linkablesOf = (items: readonly DragItem[]) => items.map((item) => item.data as Linkable);

/** "Move to epic…": the menu equivalent of a drop on an epic, listing refused epics with why. */
export function MoveToEpicMenu({ targets, name }: { targets: readonly Linkable[]; name: string }) {
  const client = useClient();
  const link = useLinkUnder();
  const [epics, setEpics] = useState<Epic[]>([]);
  const load = useCallback(async (): Promise<MenuOption[]> => {
    const found = await getEpics(client);
    setEpics(found);
    const parentOf = parentsOf(found, targets);
    return found.map((epic) => ({
      id: refOf(epic),
      label: `${refOf(epic)} ${epic.title}`,
      refusal: epicRefusal(epic, targets, parentOf)?.reason ?? null,
      hint: targets.some((t) => lower(t.repo) !== lower(epic.repo)) ? "won't fit one PR" : null,
    }));
  }, [client, targets]);
  return (
    <ActionMenu
      label="Move to epic…"
      name={name}
      options={load}
      emptyText="No open epics."
      onPick={(id) => {
        const epic = epics.find((e) => refOf(e) === id);
        if (epic) void link(epic, targets, "menu");
      }}
      disabled={targets.length === 0}
    />
  );
}

const progress = (epic: Epic) => `${epic.done} of ${epic.total} done`;

export type EpicsProps = {
  repos: Repo[];
  onOpenIssue: (repo: string, number: number, title?: string) => void;
  /** Open an epic in its own workspace tab. */
  onOpenEpic: (repo: string, number: number, title?: string) => void;
};

export function Epics({ repos, onOpenIssue, onOpenEpic }: EpicsProps) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const link = useLinkUnder();
  const [repo, setRepo] = useState("");
  const [epics, setEpics] = useState<Epic[] | null>(null);
  const [pool, setPool] = useState<IssueRow[]>([]);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const reads = useRef(0);

  const read = useCallback(async () => {
    const id = ++reads.current;
    const filters = { repo: repo || undefined };
    try {
      const [found, inbox, backlog] = await Promise.all([
        getEpics(client, repo || undefined),
        getView(client, "inbox", filters),
        getView(client, "backlog", filters),
      ]);
      if (id !== reads.current) return;
      setEpics(found);
      const seen = new Set<string>();
      setPool(
        [...inbox.rows, ...backlog.rows].filter((row) => {
          const key = lower(refOf(row));
          if (seen.has(key) || row.parent !== null || row.is_epic || row.state !== "open") return false;
          seen.add(key);
          return true;
        }),
      );
    } catch (e) {
      if (id === reads.current) toast.show({ tone: "danger", message: `Couldn't read the epics: ${errorCause(e)}` });
    }
  }, [client, repo, toast]);

  useEffect(() => {
    void read();
  }, [read, tray.revision]);

  const list = useMemo(() => epics ?? [], [epics]);
  const parentOf = useMemo(() => parentsOf(list, pool), [list, pool]);
  const chosen = pool.filter((row) => selected.has(refOf(row)));
  const group = useMemo(
    () => pool.filter((row) => selected.has(refOf(row))).map((row) => ({ id: refOf(row), label: `${refOf(row)} ${visibleText(row.title)}`, data: row })), // prettier-ignore
    [pool, selected],
  );

  const toggle = (row: IssueRow) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(refOf(row))) next.delete(refOf(row));
      else next.add(refOf(row));
      return next;
    });

  const scope = groupsScope(repo || null);
  const drafts = useDrafts({ scope }, (d) => d.kind === "group");
  const proposals = useProposalApproval((r, n) => onOpenEpic(r, n));

  return (
    <Drops>
      <div className={s.epicsPane}>
        <aside className={s.epicsList} aria-label="Epic list">
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
          </div>
          {epics !== null && list.length === 0 ? (
            <p className={s.empty}>No open epics. An issue with the epic label, or with sub-issues, shows here.</p>
          ) : null}
          {epics === null ? null : (
            <ul className={s.plain} aria-label="Epics">
              {list.map((epic) => {
                const ref = refOf(epic);
                const label = `${ref} ${visibleText(epic.title)}`;
                const data: DropAction = { drop: (items) => void link(epic, linkablesOf(items), "drag") };
                return (
                  <li key={ref}>
                    <DropTarget
                      id={`epic:${ref}`}
                      label={label}
                      data={data}
                      accepts={(items) => epicRefusal(epic, linkablesOf(items), parentOf)}
                      className={s.epicTarget}
                      role="group"
                      aria-label={label}
                    >
                      <Draggable id={`epic:${ref}`} label={label} data={epic} className={s.epicRow}>
                        {(handle) => (
                          <>
                            <div className={s.cardTop}>
                              <button type="button" {...handle} className={s.grip} aria-label={`Move ${label}`}>
                                <span aria-hidden="true">⠿</span>
                              </button>
                              <span className={s.ref}>{ref}</span>
                              {epic.is_initiative ? <Badge tone="neutral">initiative</Badge> : null}
                              <span className={s.spacer} />
                              <Badge
                                tone={epic.readiness.ready ? "success" : "warn"}
                                icon={epic.readiness.ready ? "✓" : "!"}
                              >
                                {readinessText(epic.readiness)}
                              </Badge>
                            </div>
                            <button
                              type="button"
                              className={s.cardTitle}
                              onClick={() => onOpenEpic(epic.repo, epic.number, epic.title)}
                            >
                              <VisibleText text={epic.title} />
                            </button>
                            <div className={s.progressLine}>
                              <progress
                                max={Math.max(epic.total, 1)}
                                value={epic.done}
                                aria-label={`${ref} progress`}
                              />
                              <span className={s.muted}>{progress(epic)}</span>
                            </div>
                            {epic.readiness.warning ? (
                              <Badge tone="warn" icon="⚠">
                                <VisibleText text={epic.readiness.warning} />
                              </Badge>
                            ) : null}
                            {epic.lacks_label ? (
                              <span className={s.muted}>Has sub-issues but no epic label.</span>
                            ) : null}
                            <MoveToEpicMenu targets={[epic]} name={`Move ${ref} to an epic`} />
                            <AddToQueueMenu
                              targets={[{ repo: epic.repo, number: epic.number, kind: "epic" }]}
                              name={`Add ${ref} to a queue`}
                            />
                          </>
                        )}
                      </Draggable>
                    </DropTarget>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>
        <div className={s.epicsMain}>
          <>
            <section className={s.section} aria-label="Issues without an epic">
              <h2 className={s.kicker}>Issues without an epic</h2>
              <p className={s.muted}>Drag issues onto an epic, or select them and use Move to epic…</p>
              {chosen.length > 0 ? (
                <div className={s.bulk} role="toolbar" aria-label="Selected issues">
                  <strong>{chosen.length} selected</strong>
                  <MoveToEpicMenu targets={chosen} name="Move the selected issues to an epic" />
                  <AddToQueueMenu targets={chosen} name="Add the selected issues to a queue" />
                  <span className={s.spacer} />
                  <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
                    Clear selection
                  </Button>
                </div>
              ) : null}
              {epics !== null && pool.length === 0 ? <p className={s.muted}>Every open issue is in an epic.</p> : null}
              {epics === null ? null : (
                <ul className={s.list} aria-label="Open issues without an epic">
                  {pool.map((row) => {
                    const ref = refOf(row);
                    const label = `${ref} ${visibleText(row.title)}`;
                    return (
                      <li key={ref} data-selected={selected.has(ref) || undefined}>
                        <Draggable id={ref} label={label} data={row} group={group} className={s.row}>
                          {(handle) => (
                            <>
                              <button type="button" {...handle} className={s.grip} aria-label={`Move ${label}`}>
                                <span aria-hidden="true">⠿</span>
                              </button>
                              <input
                                type="checkbox"
                                aria-label={`Select ${ref}`}
                                checked={selected.has(ref)}
                                onChange={() => toggle(row)}
                              />
                              <span className={s.ref}>{ref}</span>
                              <button
                                type="button"
                                className={s.titleButton}
                                onClick={() => onOpenIssue(row.repo, row.number, row.title)}
                              >
                                <VisibleText text={row.title} />
                              </button>
                              <span className={s.rowLabels}>
                                {row.readiness ? <Badge>{row.readiness}</Badge> : null}
                              </span>
                            </>
                          )}
                        </Draggable>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
            <section className={s.section} aria-label="Proposals">
              <div className={s.runLine}>
                <h2 className={s.kicker}>Proposals</h2>
                <span className={s.spacer} />
                <Button
                  size="sm"
                  loading={drafts.isAsking}
                  onClick={() => void drafts.ask(`groups:${scope}`, (key) => askGroups(client, repo || null, key))}
                >
                  Ask Claude for groups
                </Button>
              </div>
              {drafts.status ? <p className={s.muted}>{drafts.status}</p> : null}
              {drafts.drafts.length === 0 ? (
                <p className={s.muted}>No proposals. Claude can group related open issues into candidate epics.</p>
              ) : null}
              <ul className={s.plain}>
                {drafts.drafts.map((draft) => (
                  <ProposalCard
                    key={draft.id}
                    suggestion={draft}
                    busy={proposals.isApproving(draft.id)}
                    onApprove={() => void proposals.approve(draft)}
                    onDismiss={() => void drafts.dismiss(draft)}
                    problem={proposals.problems.get(draft.id)?.text ?? null}
                    actions={proposals.problems.get(draft.id)?.action}
                  />
                ))}
              </ul>
            </section>
          </>
        </div>
      </div>
    </Drops>
  );
}

export type EpicTabProps = {
  repo: string;
  number: number;
  onTitle: (title: string) => void;
  onClose: () => void;
  onOpenIssue: (repo: string, number: number, title?: string) => void;
};

/** An epic's own workspace tab: its page, with the parents of the open epics for drop refusals
 * (the server's own parent check refuses at staging should this read fail). */
export function EpicTab({ repo, number, onTitle, onClose, onOpenIssue }: EpicTabProps) {
  const client = useClient();
  const tray = useTray();
  const [epics, setEpics] = useState<Epic[]>([]);
  const titled = useRef(onTitle);
  titled.current = onTitle;
  useEffect(() => {
    let live = true;
    getEpics(client).then(
      (found) => {
        if (!live) return;
        setEpics(found);
        const own = found.find((e) => lower(refOf(e)) === lower(`${repo}#${number}`));
        if (own) titled.current(own.title);
      },
      () => undefined, // the tab still works: refusals then come from the server
    );
    return () => {
      live = false;
    };
  }, [client, repo, number, tray.revision]);
  const parentOf = useMemo(() => parentsOf(epics), [epics]);
  return (
    <Drops>
      <EpicView repo={repo} number={number} parentOf={parentOf} onClose={onClose} onOpenIssue={onOpenIssue} />
    </Drops>
  );
}
