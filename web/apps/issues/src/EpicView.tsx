// SPDX-License-Identifier: AGPL-3.0-or-later
// One epic in the book layout (spec §4, §6): the outline lists its children in order, then the
// delivery brief, then the proposals. Children are a sortable list: a reorder stages the sub-issue
// order, a drop from outside stages parent links (IR39), and the Remove zone stages taking one out;
// Move up, Move down and Remove are their menu equivalents. The brief editor posts through the
// brief endpoints: a brief edited on GitHub since it was posted is refused until the author
// refreshes or explicitly overwrites it. Copy brief puts the text on the clipboard. Every title,
// body and agent text is shown as text, hidden characters as visible tokens (R62).
import { Badge, Button, DropTarget, SortableList, useToast, type DragItem } from "@jesinia/design";
import { hasHidden, safeHttpUrl, useClient, VisibleText, visibleText } from "@jesinia/shell";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  ActionKeys,
  approveConversion,
  askBrief,
  askGroups,
  askSplit,
  briefScope,
  errorCause,
  getConversion,
  getEpic,
  isRefusal,
  postBrief,
  readinessText,
  refOf,
  refreshItem,
  setParentChange,
  type ConversionPlan,
  type EpicDetail,
  type Member,
  type Suggestion,
} from "./api";
import { useDrafts, useProposalApproval } from "./drafts";
import type { DropAction } from "./drops";
import { epicRefusal, MoveToEpicMenu, useLinkUnder, type Linkable } from "./Epics";
import s from "./issues.module.css";
import { PlanDiff, ProposalCard } from "./Plans";
import { AddToQueueMenu } from "./Queues";
import { useTray } from "./Tray";

type Child = DragItem & { data: Member & { parent: string } };

const lower = (ref: string) => ref.toLowerCase();

/** Copy `text`; resolves to whether the clipboard took it (a refusal is the author's to retry). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export type EpicViewProps = {
  repo: string;
  number: number;
  /** The cached parents around this epic, for the drop checks. */
  parentOf: (ref: string) => string | null;
  onClose: () => void;
  onOpenIssue: (repo: string, number: number, title?: string) => void;
};

export function EpicView({ repo, number, parentOf, onClose, onOpenIssue }: EpicViewProps) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const link = useLinkUnder();
  const ids = useId();
  const keys = useRef(new ActionKeys());
  const ref = `${repo}#${number}`;
  const [detail, setDetail] = useState<EpicDetail | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [fromDraft, setFromDraft] = useState<string | null>(null);
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [plan, setPlan] = useState<{ plan: ConversionPlan | null; problem: string | null } | null>(null);

  const read = useCallback(async () => {
    try {
      const found = await getEpic(client, repo, number);
      setDetail(found);
      setProblem(null);
      return found;
    } catch (e) {
      setProblem(errorCause(e));
      return null;
    }
  }, [client, repo, number]);

  useEffect(() => {
    void read();
  }, [read, tray.revision]);

  /** Runs a keyed write: a retry while its outcome is unknown reuses its key. */
  const keyed = async <T,>(action: string, work: (key: string) => Promise<T>): Promise<T> => {
    setBusy(action);
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    } finally {
      setBusy(null);
    }
  };

  const briefs = useDrafts({ scope: briefScope(repo, number) }, (d) => d.kind === "brief");
  const additions = useDrafts({ kind: "group" }, (d) => {
    const epic = d.payload.epic as { repo?: unknown; n?: unknown } | undefined;
    return epic !== undefined && lower(`${String(epic.repo)}#${String(epic.n)}`) === lower(ref);
  });
  const childRefs = new Set((detail?.children ?? []).map((c) => lower(refOf(c))));
  const splits = useDrafts({ kind: "split" }, (d) => {
    const whole = d.payload.issue as { repo?: unknown; n?: unknown } | undefined;
    const key = lower(`${String(whole?.repo)}#${String(whole?.n)}`);
    return key === lower(ref) || childRefs.has(key);
  });
  const proposals = useProposalApproval();

  if (detail === null) {
    return problem === null ? (
      <p className={s.empty}>Opening {ref}…</p>
    ) : (
      <div className={s.problem} role="alert">{`Couldn't open ${ref}: ${problem}`}</div>
    );
  }

  const epic = detail.epic;
  const holder = { repo: epic.repo, number: epic.number, is_initiative: epic.is_initiative, title: epic.title };
  const children: Child[] = (detail.children ?? []).map((c) => ({
    id: refOf(c),
    label: `${refOf(c)} ${visibleText(c.title ?? "")}`.trim(),
    data: { ...c, parent: ref },
  }));
  const asLinkable = (c: Member): Linkable => ({ repo: c.repo, number: c.number, parent: ref, title: c.title });
  const brief = text ?? detail.brief?.text ?? detail.default_brief;
  const githubUrl = safeHttpUrl(epic.url);

  /** Stage `moved` children in their new places: each right after the child now before it. */
  const reorder = (order: string[], moved: readonly Child[]) => {
    const changes = moved.map((child) => {
      const at = order.indexOf(child.id);
      const before = at > 0 ? children.find((c) => c.id === order[at - 1]) : undefined;
      return {
        repo: epic.repo,
        n: epic.number,
        change: {
          kind: "reorder_child" as const,
          child: { repo: child.data.repo, n: child.data.number },
          after: before ? { repo: before.data.repo, n: before.data.number } : null,
        },
        source: "drag" as const,
      };
    });
    void tray.stageAll(changes);
  };

  /** Move up or down: the menu equivalent of a reorder. */
  const step = (child: Child, by: -1 | 1) => {
    const order = children.map((c) => c.id);
    const at = order.indexOf(child.id);
    order.splice(at, 1);
    order.splice(at + by, 0, child.id);
    reorder(order, [child]);
  };

  const remove = (members: readonly Linkable[], source: "drag" | "menu") =>
    void tray.stageAll(
      members.map((m) => ({ repo: m.repo, n: m.number, change: setParentChange(null, m.parent), source })),
    );

  // A drop from outside links the issue; GitHub appends a new sub-issue, so the insertion slot
  // is ignored on purpose. Reorder it afterwards (a drag within the list, or ↑/↓) to place it.
  const outside: DropAction = {
    drop: (items) =>
      void link(
        epic,
        items.map((i) => i.data as Linkable),
        "drag",
      ),
  };
  const removeZone: DropAction = {
    drop: (items) =>
      remove(
        items.map((i) => i.data as Linkable),
        "drag",
      ),
  };

  const copy = async () => {
    const ok = await copyText(brief);
    toast.show(
      ok
        ? { tone: "success", message: `Copied the brief for ${ref}.` }
        : { tone: "danger", message: "Couldn't copy: the browser refused the clipboard. Select the text and copy it." },
    );
  };

  const post = async (overwrite: boolean) => {
    try {
      await keyed(`brief:${overwrite ? "overwrite" : "post"}:${brief}`, (key) =>
        postBrief(client, repo, number, { text: brief, suggestion_id: fromDraft, overwrite }, key),
      );
      setEdited(false);
      setText(null);
      setFromDraft(null);
      toast.show({ tone: "success", message: `Posted the brief on ${ref}.` });
      await read();
      await briefs.reload();
    } catch (e) {
      if (isRefusal(e, "BriefEditedOnGitHubError")) setEdited(true);
      toast.show({ tone: "danger", message: `Couldn't post the brief: ${errorCause(e)}` });
    }
  };

  /** Refresh: read the epic again (its comments too), and start over from the stored brief. */
  const refresh = async () => {
    try {
      await keyed(`refresh:${ref}`, (key) => refreshItem(client, repo, number, key));
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't refresh ${ref}: ${errorCause(e)}` });
    }
    const found = await read();
    if (found !== null) {
      setText(null);
      setFromDraft(null);
      setEdited(false);
    }
  };

  const takeDraft = (draft: Suggestion) => {
    setText(String(draft.payload.text ?? ""));
    setFromDraft(draft.id);
  };

  const previewConversion = async () => {
    setPlan({ plan: null, problem: null });
    try {
      setPlan({ plan: await getConversion(client, repo, number), problem: null });
    } catch (e) {
      setPlan({ plan: null, problem: errorCause(e) });
    }
  };

  const convert = async (current: ConversionPlan) => {
    try {
      const batch = await keyed(`convert:${current.digest}`, (key) =>
        approveConversion(client, repo, number, current.digest, key),
      );
      tray.follow(batch, `Checklist of ${ref} to sub-issues`);
      setPlan(null);
    } catch (e) {
      tray.followEarlier(e);
      setPlan({ plan: current, problem: errorCause(e) });
    }
  };

  const outline = [
    ...children.map((c) => ({ id: `child-${c.id}`, label: c.label ?? c.id })),
    { id: "brief", label: "Brief" },
    { id: "proposals", label: "Proposals" },
  ];
  const anchor = (id: string) => `${ids}-${id.replace(/[^A-Za-z0-9_-]/g, "_")}`;

  return (
    <div className={s.book}>
      <nav className={s.outline} aria-label="Outline">
        <ol>
          {outline.map((part) => (
            <li key={part.id}>
              <a href={`#${anchor(part.id)}`} className={s.outlineLink}>
                <VisibleText text={part.label} />
              </a>
            </li>
          ))}
        </ol>
      </nav>
      <article className={s.page}>
        <div className={s.toolbar} role="toolbar" aria-label="Epic">
          <Button size="sm" variant="ghost" onClick={onClose}>
            ← All epics
          </Button>
          <span className={s.repoName}>{repo}</span>
          <span className={s.spacer} />
          <MoveToEpicMenu targets={[{ ...holder, parent: epic.parent }]} name={`Move ${ref} to an epic`} />
          <AddToQueueMenu targets={[{ repo, number, kind: "epic" }]} name={`Add ${ref} to a queue`} />
          {githubUrl ? (
            <Button size="sm" variant="ghost" href={githubUrl} target="_blank" rel="noopener noreferrer">
              Open on GitHub
            </Button>
          ) : null}
        </div>
        <h1 className={s.title}>
          <VisibleText text={epic.title} />
        </h1>
        <p className={s.meta}>
          <span className={s.ref}>{ref}</span> · {`${epic.done} of ${epic.total} done`} ·{" "}
          <VisibleText text={readinessText(epic.readiness)} />
          {epic.readiness.warning ? (
            <>
              {" · "}
              <VisibleText text={epic.readiness.warning} />
            </>
          ) : null}
        </p>

        <section className={s.section} aria-label="Children">
          <h2 className={s.kicker}>Children, in order</h2>
          {detail.children === null ? (
            <p className={s.muted}>
              Its children haven&apos;t been fetched yet.{" "}
              <Button size="sm" variant="ghost" onClick={() => void refresh()}>
                Refresh
              </Button>
            </p>
          ) : null}
          <SortableList
            id={`children:${ref}`}
            label={`Children of ${ref}`}
            items={children}
            handle="custom"
            emptyLabel="Drop issues here to add them to this epic"
            accepts={(items) =>
              epicRefusal(
                holder,
                items.map((i) => i.data as Linkable),
                parentOf,
              )
            }
            data={outside}
            onMove={reorder}
            renderItem={(child, { index, handleProps }) => {
              const c = child.data;
              const cref = refOf(c);
              return (
                <div id={anchor(`child-${child.id}`)} className={s.childRow}>
                  <button type="button" {...handleProps} className={s.grip} aria-label={`Move ${child.label}`}>
                    <span aria-hidden="true">⠿</span>
                  </button>
                  <span className={s.ref}>{cref}</span>
                  <button
                    type="button"
                    className={s.titleButton}
                    onClick={() => onOpenIssue(c.repo, c.number, c.title ?? undefined)}
                  >
                    {c.cached ? <VisibleText text={c.title ?? ""} /> : "Not cached (its repo isn't watched)"}
                  </button>
                  {c.state === "closed" ? (
                    <Badge tone="success" icon="✓">
                      closed
                    </Badge>
                  ) : null}
                  {c.readiness ? <Badge>{c.readiness}</Badge> : null}
                  {c.blocker ? (
                    <span className={s.muted}>
                      Blocks: <VisibleText text={c.blocker} />
                    </span>
                  ) : null}
                  {c.warning ? (
                    <Badge tone="warn" icon="⚠">
                      <VisibleText text={c.warning} />
                    </Badge>
                  ) : null}
                  <span className={s.spacer} />
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={index === 0}
                    aria-label={`Move ${cref} up`}
                    onClick={() => step(child, -1)}
                  >
                    ↑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={index === children.length - 1}
                    aria-label={`Move ${cref} down`}
                    onClick={() => step(child, 1)}
                  >
                    ↓
                  </Button>
                  <MoveToEpicMenu targets={[asLinkable(c)]} name={`Move ${cref} to an epic`} />
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Remove ${cref} from this epic`}
                    onClick={() => remove([asLinkable(c)], "menu")}
                  >
                    Remove
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Propose a split of ${cref}`}
                    onClick={() => void splits.ask(`split:${cref}`, (key) => askSplit(client, c.repo, c.number, key))}
                  >
                    Split…
                  </Button>
                </div>
              );
            }}
          />
          <DropTarget
            id={`remove:${ref}`}
            label={`Remove from ${ref}`}
            data={removeZone}
            accepts={(items) =>
              items.every((i) => childRefs.has(lower(refOf(i.data as Linkable))))
                ? null
                : { code: "not_child", reason: `Only ${ref}'s own children can be removed from it` }
            }
            className={s.zone}
            role="group"
            aria-label={`Remove from ${ref}`}
          >
            Drop here to remove from this epic
          </DropTarget>
        </section>

        {detail.checklist_items > 0 ? (
          <section className={s.section} aria-label="Checklist">
            <h2 className={s.kicker}>Checklist</h2>
            <p className={s.muted}>
              {`The body has ${detail.checklist_items} checklist items. Converting makes them sub-issues, as one approved batch.`}
            </p>
            {plan === null ? (
              <Button size="sm" onClick={() => void previewConversion()}>
                Preview the conversion
              </Button>
            ) : plan.plan === null ? (
              plan.problem === null ? (
                <p className={s.muted}>Planning…</p>
              ) : (
                <p className={s.planProblem} role="alert">
                  {plan.problem}
                </p>
              )
            ) : (
              <PlanDiff
                title={`Convert the checklist of ${ref}`}
                epic={null}
                children={plan.plan.children}
                skipped={plan.plan.skipped}
                busy={busy?.startsWith("convert:") ?? false}
                problem={plan.problem}
                onApprove={() => void convert(plan.plan as ConversionPlan)}
                onCancel={() => setPlan(null)}
                actions={
                  plan.problem ? (
                    <Button size="sm" variant="ghost" onClick={() => void previewConversion()}>
                      Preview again
                    </Button>
                  ) : null
                }
              />
            )}
          </section>
        ) : null}

        <section id={anchor("brief")} className={s.section} aria-labelledby={anchor("brief-heading")}>
          <h2 id={anchor("brief-heading")} className={s.kicker}>
            Delivery brief
          </h2>
          <p className={s.muted}>
            {detail.brief
              ? detail.brief.state === "in_flight"
                ? "A post of the brief was cut off: posting again finds it on GitHub first."
                : "Posted on the epic. Edits here replace that comment when you post."
              : "Composed from the children. Edit it, then post it on the epic."}
            {detail.brief?.url && safeHttpUrl(detail.brief.url) ? (
              <>
                {" "}
                <a href={safeHttpUrl(detail.brief.url) ?? ""} target="_blank" rel="noopener noreferrer">
                  View it on GitHub
                </a>
              </>
            ) : null}
          </p>
          <textarea
            className={s.briefEditor}
            aria-label="Brief text"
            value={brief}
            rows={12}
            onChange={(e) => setText(e.target.value)}
          />
          {hasHidden(brief, { multiline: true }) ? (
            <div className={s.hiddenNote}>
              <p className={s.muted}>The brief has hidden characters, shown as tokens here:</p>
              <pre className={s.briefPreview}>{visibleText(brief, { multiline: true })}</pre>
            </div>
          ) : null}
          {edited ? (
            <div className={s.planProblem} role="alert">
              <p>
                The brief was edited on GitHub since it was posted. Refresh to start from it, or overwrite it with this
                text.
              </p>
              <div className={s.planActions}>
                <Button size="sm" onClick={() => void refresh()}>
                  Refresh
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  loading={busy?.startsWith("brief:overwrite") ?? false}
                  onClick={() => void post(true)}
                >
                  Overwrite
                </Button>
              </div>
            </div>
          ) : null}
          <div className={s.planActions}>
            <Button
              size="sm"
              variant="primary"
              loading={busy?.startsWith("brief:post") ?? false}
              onClick={() => void post(false)}
            >
              {detail.brief ? "Post the edit" : "Post the brief"}
            </Button>
            <Button size="sm" onClick={() => void copy()}>
              Copy brief
            </Button>
            <Button
              size="sm"
              variant="ghost"
              loading={briefs.isAsking}
              onClick={() => void briefs.ask(`brief:${ref}`, (key) => askBrief(client, repo, number, key))}
            >
              Ask Claude for a draft
            </Button>
          </div>
          {briefs.status ? <p className={s.muted}>{briefs.status}</p> : null}
          {briefs.drafts.map((draft) => (
            <div key={draft.id} className={s.proposal}>
              <span>Claude drafted a brief.</span>
              <div className={s.planActions}>
                <Button size="sm" onClick={() => takeDraft(draft)}>
                  Use Claude&apos;s draft
                </Button>
                <Button size="sm" variant="ghost" onClick={() => void briefs.dismiss(draft)}>
                  Dismiss the draft
                </Button>
              </div>
            </div>
          ))}
        </section>

        <section id={anchor("proposals")} className={s.section} aria-label="Proposals">
          <div className={s.runLine}>
            <h2 className={s.kicker}>Proposals</h2>
            <span className={s.spacer} />
            <Button
              size="sm"
              loading={additions.isAsking}
              onClick={() => void additions.ask(`groups:${epic.repo}`, (key) => askGroups(client, epic.repo, key))}
            >
              Ask Claude for additions
            </Button>
          </div>
          {additions.status ? <p className={s.muted}>{additions.status}</p> : null}
          {splits.status ? <p className={s.muted}>{splits.status}</p> : null}
          {[...additions.drafts, ...splits.drafts].length === 0 ? (
            <p className={s.muted}>No proposals for this epic.</p>
          ) : null}
          <ul className={s.plain}>
            {[...additions.drafts, ...splits.drafts].map((draft) => (
              <ProposalCard
                key={draft.id}
                suggestion={draft}
                busy={proposals.isApproving(draft.id)}
                onApprove={() => void proposals.approve(draft)}
                onDismiss={() => void (draft.kind === "split" ? splits : additions).dismiss(draft)}
                problem={proposals.problems.get(draft.id)?.text ?? null}
                actions={proposals.problems.get(draft.id)?.action}
              />
            ))}
          </ul>
        </section>
      </article>
    </div>
  );
}
