// SPDX-License-Identifier: AGPL-3.0-or-later
// One work queue (spec §5.2, §6.1, rulings IR15, IR52): flat, or grouped by priority bands. Order
// is local and changes at once, with Undo. A move into another band also stages that priority
// label (`source` `queue`); while the author has a priority change staged from the tray, that move
// is refused (409) and the copy says so, but reordering within the band always works. Done (the
// row's checkbox, the `x` key, or a drop on the Done zone) moves the item to the collapsed Done
// group at once and stages closing the issue, or handing it to an agent (agent-ready). Every item
// shows its readiness and a warning when it isn't ready, and has Copy brief. The agent's
// proposals (suggest next, order) show as a draft diff to approve, changing only the queue.
import { Badge, Button, DropTarget, SortableList, useToast, type DragItem } from "@jesinia/design";
import { useClient, VisibleText, visibleText } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ActionMenu } from "./ActionMenu";
import {
  ActionKeys,
  addQueueItem,
  approveQueueDraft,
  askQueue,
  bandQueueItem,
  doneQueueItem,
  errorCause,
  getEpic,
  getMilestone,
  getQueue,
  isRefusal,
  moveQueueItem,
  queueScope,
  readinessText,
  removeQueueItem,
  shortValue,
  undoQueueItem,
  type Queue,
  type QueueItem,
  type Suggestion,
} from "./api";
import { useDrafts } from "./drafts";
import type { DropAction } from "./drops";
import { copyText } from "./EpicView";
import s from "./issues.module.css";
import { useTray } from "./Tray";

type Row = DragItem & { data: QueueItem };

const refOfItem = (item: QueueItem) => `${item.repo}#${item.n}`;
const idOf = (item: QueueItem) => `${item.kind}:${refOfItem(item).toLowerCase()}`;
const rowOf = (item: QueueItem): Row => ({
  id: idOf(item),
  label: `${refOfItem(item)} ${visibleText(item.title ?? "")}`.trim(),
  data: item,
});
const bandName = (value: string | null) => (value === null ? "No priority" : `${shortValue("priority", value)} priority`.replace(/^./, (c) => c.toUpperCase())); // prettier-ignore

/** Done closes an issue or epic (or hands it to an agent); a milestone isn't the author's to close. */
const canBeDone = (item: QueueItem) => item.kind !== "milestone";

const isEditable = (target: EventTarget | null) =>
  target instanceof HTMLTextAreaElement ||
  (target instanceof HTMLInputElement && !["checkbox", "radio", "button"].includes(target.type));

/** The brief to hand an agent for one item: an epic's or milestone's, else the issue itself. */
async function briefOf(client: ReturnType<typeof useClient>, item: QueueItem): Promise<string> {
  if (item.kind === "epic") {
    const epic = await getEpic(client, item.repo, item.n);
    return epic.brief?.text ?? epic.default_brief;
  }
  if (item.kind === "milestone") return (await getMilestone(client, item.repo, item.n)).brief;
  return `# ${item.title ?? refOfItem(item)}\n\n${refOfItem(item)}${item.url ? `\n${item.url}` : ""}\n`;
}

/** A queue draft as a diff: items to add, a new order, or which queue each item fits. */
export function QueueDraft({
  draft,
  names,
  order,
  onApprove,
  onDismiss,
}: {
  draft: Suggestion;
  names: ReadonlyMap<string, string>;
  /** The queue's current order, by `owner/repo#n` (lowercased), for an order draft. */
  order: readonly string[];
  onApprove: () => void;
  onDismiss: () => void;
}) {
  const items = (Array.isArray(draft.payload.items) ? draft.payload.items : []) as {
    repo?: unknown;
    n?: unknown;
    reason?: unknown;
    from?: unknown;
    to?: unknown;
  }[];
  const what =
    draft.kind === "queue_next" ? "Add next" : draft.kind === "queue_order" ? "A new order" : "Split across queues";
  return (
    <li className={s.proposal} aria-label={what}>
      <div className={s.sugWhat}>{what}</div>
      <ol className={s.diff}>
        {items.map((item, i) => {
          const ref = `${String(item.repo)}#${String(item.n)}`;
          const was = order.indexOf(ref.toLowerCase());
          const mark =
            draft.kind === "queue_next"
              ? "+ Add"
              : draft.kind === "queue_order"
                ? was === i
                  ? "="
                  : `${was === -1 ? "new" : was + 1} → ${i + 1}`
                : `${names.get(String(item.from)) ?? String(item.from)} → ${names.get(String(item.to)) ?? String(item.to)}`;
          return (
            <li key={ref} className={draft.kind === "queue_order" && was === i ? s.diffKeep : s.diffAdd}>
              <span>{mark}</span> <span className={s.ref}>{ref}</span>{" "}
              {typeof item.reason === "string" ? (
                <span className={s.why}>
                  <VisibleText text={item.reason} />
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      <span className={s.why}>
        <VisibleText text={draft.reason} />
      </span>
      <div className={s.planActions}>
        <Button size="sm" variant="primary" onClick={onApprove} aria-label={`Approve: ${what}`}>
          Approve
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss} aria-label={`Dismiss: ${what}`}>
          Dismiss
        </Button>
      </div>
    </li>
  );
}

export function QueueView({ id, onChanged }: { id: string; onChanged: () => void }) {
  const client = useClient();
  const toast = useToast();
  const tray = useTray();
  const keys = useRef(new ActionKeys());
  const [queue, setQueue] = useState<Queue | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [banded, setBanded] = useState(false);
  const reads = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  // After Done, Remove or Undo the focused row unmounts; this names the row that takes focus.
  const focusNext = useRef<string | null>(null);

  useEffect(() => {
    const wanted = focusNext.current;
    if (wanted === null || queue === null) return;
    focusNext.current = null;
    const lost = document.activeElement === null || document.activeElement === document.body;
    if (!lost) return; // the user (or a drop) has put focus somewhere already
    const row = [...(box.current?.querySelectorAll<HTMLElement>("[data-row-id]") ?? [])].find((el) => el.dataset.rowId === wanted); // prettier-ignore
    const target = row?.querySelector<HTMLElement>("input[type=checkbox]") ?? row?.querySelector<HTMLElement>("button");
    (target ?? box.current)?.focus();
  }, [queue]);

  const read = useCallback(async () => {
    const at = ++reads.current;
    try {
      const found = await getQueue(client, id);
      if (at === reads.current) {
        setQueue(found);
        setProblem(null);
      }
    } catch (e) {
      if (at === reads.current) setProblem(errorCause(e));
    }
  }, [client, id]);

  useEffect(() => {
    void read();
  }, [read, tray.revision]);

  /** Runs a keyed queue write and shows the queue it answers with. */
  const write = async <T,>(action: string, work: (key: string) => Promise<T>, shown: (t: T) => Queue): Promise<T | null> => {
    reads.current += 1; // an older read answering later mustn't undo this
    try {
      const result = await work(keys.current.keyFor(action));
      keys.current.settle(action);
      setQueue(shown(result));
      onChanged();
      return result;
    } catch (e) {
      keys.current.settle(action, e);
      throw e;
    }
  }; // prettier-ignore

  const next = useDrafts({ scope: queueScope("next", id) }, (d) => d.kind === "queue_next");
  const ordering = useDrafts({ scope: queueScope("order", id) }, (d) => d.kind === "queue_order");

  if (queue === null) {
    return problem === null ? (
      <p className={s.empty}>Opening the queue…</p>
    ) : (
      <div className={s.problem} role="alert">{`Couldn't open the queue: ${problem}`}</div>
    );
  }

  const name = queue.name;

  /** The row to focus once `item` leaves the open list: the next one, else the one before. */
  const neighbourOf = (item: QueueItem) => {
    const at = queue.items.findIndex((i) => idOf(i) === idOf(item));
    const near = queue.items[at + 1] ?? queue.items[at - 1];
    return near ? idOf(near) : "";
  };

  /** Move `item` to `position` among the open items, at once, with Undo. */
  const move = async (item: QueueItem, position: number, undoable = true) => {
    const from = queue.items.findIndex((i) => idOf(i) === idOf(item));
    try {
      await write(
        `move:${idOf(item)}:${position}`,
        (key) => moveQueueItem(client, id, item, position, key),
        (q) => q,
      );
      if (!undoable) return;
      toast.show({
        tone: "info",
        message: `Moved ${refOfItem(item)} to position ${position + 1} in ${name}.`,
        action: { label: "Undo", onAction: () => void move(item, from, false) },
      });
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't move ${refOfItem(item)}: ${errorCause(e)}` });
    }
  };

  /** A reorder inside one list (flat, or one band): its new place among all the open items. */
  const reorder = (list: readonly QueueItem[], order: string[], moved: readonly Row[]) => {
    const first = moved[0];
    if (!first) return;
    const at = order.indexOf(first.id);
    const others = queue.items.filter((i) => idOf(i) !== first.id);
    const after = list.find((i) => idOf(i) === order[at + 1]);
    const before = list.find((i) => idOf(i) === order[at - 1]);
    const position = after
      ? others.findIndex((i) => idOf(i) === idOf(after))
      : before
        ? others.findIndex((i) => idOf(i) === idOf(before)) + 1
        : 0;
    void move(first.data, position);
  };

  /** A move into a band: reorders at once and stages the priority when the band differs. */
  const intoBand = async (item: QueueItem, band: string, position: number) => {
    try {
      const done = await write(
        `band:${idOf(item)}:${band}:${position}`,
        (key) => bandQueueItem(client, id, item, band, position, key),
        (a) => a.queue,
      );
      if (done?.staged) {
        await tray.refresh();
        toast.show({ tone: "info", message: `Staged: set the priority of ${refOfItem(item)} to ${shortValue("priority", band)}. Nothing is sent until you apply.` }); // prettier-ignore
      }
    } catch (e) {
      const message = isRefusal(e, "TrayChangeStagedError")
        ? `${refOfItem(item)} already has a priority change staged from the tray, so it can't move to another band: undo that change in Pending changes first. Reordering within its band still works.` // prettier-ignore
        : `Couldn't move ${refOfItem(item)} to ${bandName(band)}: ${errorCause(e)}`;
      toast.show({ tone: "danger", message });
    }
  };

  const done = async (item: QueueItem, then: "close" | "agent_ready", comment?: string) => {
    const body = comment === undefined ? { then } : { then, comment };
    focusNext.current = neighbourOf(item);
    try {
      await write(
        `done:${idOf(item)}:${then}`,
        (key) => doneQueueItem(client, id, item, body, key),
        (a) => a.queue,
      );
      await tray.refresh();
      toast.show({
        tone: "info",
        message: `Done: ${refOfItem(item)}. ${then === "close" ? "Closing it" : "Setting it agent-ready"} is staged in Pending changes.`, // prettier-ignore
      });
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't mark ${refOfItem(item)} done: ${errorCause(e)}` });
    }
  };

  const undo = async (item: QueueItem) => {
    focusNext.current = idOf(item);
    try {
      await write(
        `undo:${idOf(item)}`,
        (key) => undoQueueItem(client, id, item, key),
        (q) => q,
      );
      await tray.refresh();
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't undo the Done of ${refOfItem(item)}: ${errorCause(e)}` });
    }
  };

  /** Remove at once, with Undo: it goes back at its place with its note (its added date resets). */
  const remove = async (item: QueueItem) => {
    const from = queue.items.findIndex((i) => idOf(i) === idOf(item));
    focusNext.current = neighbourOf(item);
    try {
      await write(
        `remove:${idOf(item)}`,
        (key) => removeQueueItem(client, id, item, key),
        (q) => q,
      );
      await tray.refresh();
      toast.show({
        tone: "info",
        message: `Removed ${refOfItem(item)} from ${name}.`,
        action: { label: "Undo", onAction: () => void putBack(item, from) },
      });
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't remove ${refOfItem(item)}: ${errorCause(e)}` });
    }
  };

  const putBack = async (item: QueueItem, position: number) => {
    const note = item.note ? { note: item.note } : {};
    focusNext.current = idOf(item);
    try {
      await write(
        `putback:${idOf(item)}`,
        (key) => addQueueItem(client, id, { kind: item.kind, repo: item.repo, n: item.n, position: Math.max(position, 0), ...note }, key), // prettier-ignore
        (q) => q,
      );
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't put ${refOfItem(item)} back: ${errorCause(e)}` });
    }
  };

  const copyBrief = async (item: QueueItem) => {
    try {
      const text = await briefOf(client, item);
      if (await copyText(text)) toast.show({ tone: "success", message: `Copied the brief for ${refOfItem(item)}.` });
      else toast.show({ tone: "danger", message: "Couldn't copy: the browser refused the clipboard." });
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't read the brief for ${refOfItem(item)}: ${errorCause(e)}` });
    }
  };

  const approve = async (draft: Suggestion, drafts: typeof next) => {
    try {
      const approval = await write(`approve:${draft.id}`, (key) => approveQueueDraft(client, draft.id, key), (a) => a.queues.find((q) => q.id === id) ?? queue); // prettier-ignore
      const skipped = approval?.skipped ?? [];
      toast.show(
        skipped.length > 0
          ? { tone: "warn", message: `Approved; skipped because they changed since: ${skipped.join(", ")}.` }
          : { tone: "success", message: `Approved: ${name} changed here only.` },
      );
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't approve the proposal: ${errorCause(e)}` });
    }
    await drafts.reload();
  };

  const onKey = (item: QueueItem) => (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "x" || e.metaKey || e.ctrlKey || e.altKey || isEditable(e.target)) return;
    if (!canBeDone(item) || (e.target instanceof Element && e.target.closest("[role=menu]"))) return;
    e.preventDefault();
    void done(item, "close");
  };

  const values = queue.bands.map((b) => b.value).filter((v): v is string => v !== null);

  const renderItem = (row: Row, { index, handleProps }: { index: number; handleProps: object }) => {
    const item = row.data;
    const ref = refOfItem(item);
    const at = queue.items.findIndex((i) => idOf(i) === row.id);
    const doable = canBeDone(item);
    const options = [
      ...(doable ? [{ id: "agent", label: "Done: hand it to an agent (agent-ready)" }] : []),
      ...(at > 0 ? [{ id: "up", label: "Move up" }] : []),
      ...(at < queue.items.length - 1 ? [{ id: "down", label: "Move down" }] : []),
      ...values.filter((v) => v !== item.priority).map((v) => ({ id: `band:${v}`, label: `Move to ${bandName(v).toLowerCase()}` })), // prettier-ignore
      { id: "remove", label: `Remove from ${name}` },
    ];
    const pick = (choice: string) => {
      if (choice === "agent") void done(item, "agent_ready");
      else if (choice === "up") void move(item, at - 1);
      else if (choice === "down") void move(item, at + 1);
      else if (choice === "remove") void remove(item);
      else void intoBand(item, choice.slice("band:".length), 0);
    };
    return (
      <div className={s.queueRow} onKeyDown={onKey(item)} data-index={index} data-row-id={row.id}>
        <button type="button" {...handleProps} className={s.grip} aria-label={`Move ${row.label}`}>
          <span aria-hidden="true">⠿</span>
        </button>
        {doable ? (
          <input
            type="checkbox"
            checked={false}
            aria-label={`Done: ${row.label}`}
            aria-keyshortcuts="x"
            onChange={() => void done(item, "close")}
          />
        ) : null}
        <span className={s.ref}>{ref}</span>
        {item.kind !== "issue" ? <Badge tone="neutral">{item.kind}</Badge> : null}
        <span className={s.queueTitle}>{item.title === null ? "Not cached" : <VisibleText text={item.title} />}</span>
        {item.readiness ? (
          <Badge tone={item.readiness.ready ? "success" : "warn"} icon={item.readiness.ready ? "✓" : "!"}>
            <VisibleText text={readinessText(item.readiness)} />
          </Badge>
        ) : null}
        {item.pending_priority ? (
          <Badge tone="neutral">{`${shortValue("priority", item.pending_priority)} priority staged`}</Badge>
        ) : null}
        {item.warning ? (
          <span className={s.warning}>
            ⚠ <VisibleText text={item.warning} />
          </span>
        ) : null}
        {item.note ? (
          <span className={s.muted}>
            <VisibleText text={item.note} />
          </span>
        ) : null}
        <span className={s.spacer} />
        <Button size="sm" variant="ghost" aria-label={`Copy brief for ${ref}`} onClick={() => void copyBrief(item)}>
          Copy brief
        </Button>
        <ActionMenu
          label="More…"
          name={`More for ${ref}`}
          options={options}
          onPick={pick}
          create={doable ? { label: `Done, with a comment on ${ref}`, submitLabel: "Done", onCreate: (comment) => void done(item, "close", comment) } : undefined} // prettier-ignore
        />
      </div>
    );
  };

  const itemsOf = (rows: readonly DragItem[]) => rows.map((r) => r.data as QueueItem);
  // One write after another, so each answer's queue doesn't race the others.
  const inTurn = (rows: readonly DragItem[], each: (item: QueueItem) => Promise<void>) =>
    void itemsOf(rows).reduce((prev, item) => prev.then(() => each(item)), Promise.resolve());
  const doneZone: DropAction = { drop: (rows) => inTurn(rows.filter((r) => canBeDone(r.data as QueueItem)), (i) => done(i, "close")) }; // prettier-ignore
  const removeZone: DropAction = { drop: (rows) => inTurn(rows, remove) };
  const warned = queue.items.filter((i) => i.warning !== null).length;
  const order = queue.items.map((i) => refOfItem(i).toLowerCase());

  return (
    <div className={s.queue} ref={box} tabIndex={-1}>
      <div className={s.listBar}>
        {queue.description ? (
          <span className={s.muted}>
            <VisibleText text={queue.description} />
          </span>
        ) : null}
        <fieldset className={s.segmented}>
          <legend className={s.muted}>Show</legend>
          {[false, true].map((on) => (
            <label key={String(on)} className={s.segment} data-on={banded === on || undefined}>
              <input type="radio" name={`queue-${id}-mode`} checked={banded === on} onChange={() => setBanded(on)} />
              {on ? "By priority" : "Flat"}
            </label>
          ))}
        </fieldset>
        <span className={s.spacer} />
        {warned > 0 ? <span className={s.warning}>{`⚠ ${warned} not ready: see each warning`}</span> : null}
        <Button
          size="sm"
          loading={next.isAsking}
          onClick={() => void next.ask(`next:${id}`, (key) => askQueue(client, id, "next", key))}
        >
          Suggest next
        </Button>
        <Button
          size="sm"
          loading={ordering.isAsking}
          onClick={() => void ordering.ask(`order:${id}`, (key) => askQueue(client, id, "order", key))}
        >
          Order this queue
        </Button>
      </div>
      {next.status ? <p className={s.muted}>{next.status}</p> : null}
      {ordering.status ? <p className={s.muted}>{ordering.status}</p> : null}
      {[...next.drafts, ...ordering.drafts].length > 0 ? (
        <ul className={s.plain} aria-label="Proposals">
          {next.drafts.map(
            (draft) =>
            <QueueDraft key={draft.id} draft={draft} names={new Map()} order={order} onApprove={() => void approve(draft, next)} onDismiss={() => void next.dismiss(draft)} />, // prettier-ignore
          )}
          {ordering.drafts.map(
            (draft) =>
            <QueueDraft key={draft.id} draft={draft} names={new Map()} order={order} onApprove={() => void approve(draft, ordering)} onDismiss={() => void ordering.dismiss(draft)} />, // prettier-ignore
          )}
        </ul>
      ) : null}
      {banded ? (
        queue.bands.map((band) => {
          const label = bandName(band.value);
          const rows = band.items.map(rowOf);
          const data: DropAction = {
            drop: (dropped, position) => {
              const first = itemsOf(dropped)[0];
              if (first && band.value !== null) void intoBand(first, band.value, position ?? band.items.length);
            },
          };
          return (
            <div key={label} className={s.band}>
              <h3 className={s.kicker}>{label}</h3>
              <SortableList
                id={`band:${id}:${label}`}
                label={label}
                items={rows}
                handle="custom"
                emptyLabel="Drop items here"
                data={data}
                accepts={() =>
                  band.value === null
                    ? {
                        code: "no_value",
                        reason: "Taking the priority off isn't something a queue stages: pick a band",
                      }
                    : null
                }
                onMove={(o, moved) => reorder(band.items, o, moved)}
                renderItem={renderItem}
              />
            </div>
          );
        })
      ) : (
        <SortableList
          id={`queue:${id}`}
          label={name}
          items={queue.items.map(rowOf)}
          handle="custom"
          emptyLabel="Nothing open: add issues and epics with Add to queue…"
          onMove={(o, moved) => reorder(queue.items, o, moved)}
          renderItem={renderItem}
        />
      )}
      <div className={s.zones}>
        <DropTarget
          id={`done:${id}`}
          label={`Mark done in ${name}`}
          data={doneZone}
          className={s.zone}
          role="group"
          aria-label={`Mark done in ${name}`}
        >
          ✓ Drop here to mark done
        </DropTarget>
        <DropTarget
          id={`remove:${id}`}
          label={`Remove from ${name}`}
          data={removeZone}
          className={s.zone}
          role="group"
          aria-label={`Remove from ${name}`}
        >
          Drop here to remove from the queue
        </DropTarget>
      </div>
      <details className={s.doneGroup} role="group" aria-label={`Done (${queue.done.length})`}>
        <summary>{`Done (${queue.done.length})`}</summary>
        {queue.done.length === 0 ? <p className={s.muted}>Nothing done yet.</p> : null}
        <ul className={s.plain}>
          {queue.done.map((item) => (
            <li key={idOf(item)} className={s.doneRow}>
              <span className={s.ref}>{refOfItem(item)}</span>
              <span className={s.queueTitle}>
                {item.title === null ? "Not cached" : <VisibleText text={item.title} />}
              </span>
              <span className={s.muted}>
                {item.done_pending ? "its change waits in Pending changes" : "closed on GitHub"}
              </span>
              <span className={s.spacer} />
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Undo Done: ${refOfItem(item)}`}
                onClick={() => void undo(item)}
              >
                Undo
              </Button>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
