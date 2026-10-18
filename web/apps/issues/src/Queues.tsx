// SPDX-License-Identifier: AGPL-3.0-or-later
// The Queues tab (spec §5.2, §6, ruling IR15): section tabs, one per work queue, with counts.
// Queues live in Jesinia only: making one, its order and its items change at once. A queue's tab
// is a drop target (an item dropped on another queue's tab is added to it), and "Add to queue…"
// is its menu equivalent from the lists. "Split across queues" asks the agent which queue each
// item fits, shown as a draft diff to approve.
import { Button, DropTarget, Input, useToast, type DragItem } from "@jesinia/design";
import { useClient, VisibleText } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ActionMenu } from "./ActionMenu";
import {
  ActionKeys,
  addQueueItem,
  approveQueueDraft,
  askQueueSplit,
  createQueue,
  errorCause,
  getQueues,
  refOf,
  removeQueueItem,
  splitScope,
  type ItemKind,
  type QueueItem,
  type QueueSummary,
  type Suggestion,
} from "./api";
import { useDrafts } from "./drafts";
import { Drops, type DropAction } from "./drops";
import s from "./issues.module.css";
import { QueueDraft, QueueView } from "./QueueView";

/** Something to add: a list row (an issue or epic, which the cache tells apart) or a queue item,
 * which keeps its kind so a milestone stays a milestone. */
export type Addable = { repo: string; number: number; kind?: ItemKind };
const plural = (n: number, one: string, other: string) => `${n} ${n === 1 ? one : other}`;

/** Add items to a queue, one after another, at once, with Undo (spec §6.1). `onChanged` runs
 * after an add and after its Undo, so a caller can re-read counts. */
export function useAddToQueue(onChanged?: () => void) {
  const client = useClient();
  const toast = useToast();
  const keys = useRef(new ActionKeys());
  const changed = useRef(onChanged);
  changed.current = onChanged;
  return useCallback(
    async (queue: { id: string; name: string }, items: readonly Addable[], position?: number) => {
      const added: { kind: ItemKind; repo: string; n: number }[] = [];
      for (const [i, item] of items.entries()) {
        const kind = item.kind ?? "issue";
        const action = `add:${queue.id}:${kind}:${refOf(item)}`;
        const at = position === undefined ? {} : { position: position + i };
        try {
          await addQueueItem(client, queue.id, { kind, repo: item.repo, n: item.number, ...at }, keys.current.keyFor(action)); // prettier-ignore
          keys.current.settle(action);
          added.push({ kind, repo: item.repo, n: item.number });
        } catch (e) {
          keys.current.settle(action, e);
          toast.show({ tone: "danger", message: `Couldn't add ${refOf(item)} to ${queue.name}: ${errorCause(e)}` });
        }
      }
      const undo = async () => {
        let removed = 0;
        for (const item of added) {
          const action = `unadd:${queue.id}:${item.kind}:${item.repo}#${item.n}`;
          try {
            await removeQueueItem(client, queue.id, item, keys.current.keyFor(action));
            keys.current.settle(action);
            removed += 1;
          } catch (e) {
            keys.current.settle(action, e);
            toast.show({ tone: "danger", message: `Couldn't take ${item.repo}#${item.n} back out of ${queue.name}: ${errorCause(e)}` }); // prettier-ignore
          }
        }
        if (removed > 0) toast.show({ tone: "info", message: `Took ${plural(removed, "item", "items")} back out of ${queue.name}.` }); // prettier-ignore
        changed.current?.();
      };
      if (added.length > 0)
        toast.show({
          tone: "success",
          message: `Added ${plural(added.length, "item", "items")} to ${queue.name}.`,
          action: { label: "Undo", onAction: () => void undo() },
        });
      changed.current?.();
      return added.length;
    },
    [client, toast],
  );
}

/** "Add to queue…": the menu equivalent of a drop on a queue's tab. */
export function AddToQueueMenu({ targets, name }: { targets: readonly Addable[]; name: string }) {
  const client = useClient();
  const add = useAddToQueue();
  const [queues, setQueues] = useState<QueueSummary[]>([]);
  const load = useCallback(async () => {
    const found = await getQueues(client);
    setQueues(found);
    return found.map((q) => ({ id: q.id, label: q.name }));
  }, [client]);
  return (
    <ActionMenu
      label="Add to queue…"
      name={name}
      options={load}
      emptyText="No queues yet: make one in Queues."
      onPick={(id) => {
        const queue = queues.find((q) => q.id === id);
        if (queue) void add(queue, targets);
      }}
      disabled={targets.length === 0}
    />
  );
}

const asAddable = (items: readonly DragItem[]): Addable[] =>
  items.map((item) => {
    const data = item.data as QueueItem;
    return { repo: data.repo, number: data.n, kind: data.kind };
  });

export function Queues() {
  const client = useClient();
  const toast = useToast();
  const keys = useRef(new ActionKeys());
  const [queues, setQueues] = useState<QueueSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [revision, setRevision] = useState(0);
  const tabs = useRef(new Map<string, HTMLButtonElement>());

  const read = useCallback(async () => {
    try {
      const found = await getQueues(client);
      setQueues(found);
      setSelected((prev) => (prev !== null && found.some((q) => q.id === prev) ? prev : (found[0]?.id ?? null)));
    } catch (e) {
      toast.show({ tone: "danger", message: `Couldn't read the queues: ${errorCause(e)}` });
    }
  }, [client, toast]);

  useEffect(() => {
    void read();
  }, [read]);
  const add = useAddToQueue(() => void read());

  const create = async () => {
    const wanted = name.trim();
    if (!wanted) return;
    const action = `queue:${wanted}`;
    try {
      const made = await createQueue(client, wanted, keys.current.keyFor(action));
      keys.current.settle(action);
      setName("");
      await read();
      setSelected(made.id);
    } catch (e) {
      keys.current.settle(action, e);
      toast.show({ tone: "danger", message: `Couldn't make the queue: ${errorCause(e)}` });
    }
  };

  const list = queues ?? [];
  const ids = list.map((q) => q.id);
  const split = useDrafts(ids.length >= 2 ? { scope: splitScope(ids) } : null, (d) => d.kind === "queue_split");

  const approveSplit = async (draft: Suggestion) => {
    const action = `approve:${draft.id}`;
    try {
      const approval = await approveQueueDraft(client, draft.id, keys.current.keyFor(action));
      keys.current.settle(action);
      if (approval.skipped.length > 0) {
        toast.show({
          tone: "warn",
          message: `Approved; skipped because they changed since: ${approval.skipped.join(", ")}.`,
        });
      } else toast.show({ tone: "success", message: "Approved: the queues changed here only." });
      await read();
      setRevision((n) => n + 1);
      await split.reload();
    } catch (e) {
      keys.current.settle(action, e);
      toast.show({ tone: "danger", message: `Couldn't approve the split: ${errorCause(e)}` });
    }
  };

  const moveTab = (e: KeyboardEvent<HTMLDivElement>) => {
    const at = ids.indexOf(selected ?? "");
    const next = e.key === "ArrowRight" ? at + 1 : e.key === "ArrowLeft" ? at - 1 : null;
    if (next === null || ids.length === 0) return;
    const id = ids[(next + ids.length) % ids.length] as string;
    e.preventDefault();
    setSelected(id);
    tabs.current.get(id)?.focus();
  };

  return (
    <Drops>
      <div className={s.queuesPane}>
        <div className={s.listBar}>
          <div role="tablist" aria-label="Queues" className={s.queueTabs} onKeyDown={moveTab}>
            {list.map((q) => {
              const data: DropAction = { drop: (items) => void add(q, asAddable(items)) };
              const isSelected = q.id === selected;
              return (
                <DropTarget
                  key={q.id}
                  id={`queue-tab:${q.id}`}
                  label={q.name}
                  data={data}
                  accepts={() => (isSelected ? { code: "same_queue", reason: "It's in this queue already: drop it in the list to reorder" } : null)} // prettier-ignore
                  className={s.queueTab}
                  role="presentation"
                >
                  <button
                    ref={(el) => {
                      if (el) tabs.current.set(q.id, el);
                      else tabs.current.delete(q.id);
                    }}
                    type="button"
                    role="tab"
                    aria-selected={isSelected}
                    tabIndex={isSelected ? 0 : -1}
                    className={s.wtab}
                    onClick={() => setSelected(q.id)}
                  >
                    <VisibleText text={q.name} /> <span className={s.muted}>{q.open}</span>
                    {q.warnings > 0 ? <span className={s.muted}>{` · ${q.warnings} ⚠`}</span> : null}
                  </button>
                </DropTarget>
              );
            })}
          </div>
          <span className={s.spacer} />
          <form
            className={s.menuCreate}
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
          >
            <Input
              label="New queue"
              hideLabel
              placeholder="New queue"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <Button size="sm" type="submit" disabled={name.trim() === ""}>
              Make queue
            </Button>
          </form>
          {ids.length >= 2 ? (
            <Button
              size="sm"
              loading={split.isAsking}
              onClick={() => void split.ask(`split:${ids.join(",")}`, (key) => askQueueSplit(client, ids, key))}
            >
              Split across queues
            </Button>
          ) : null}
        </div>
        {split.status ? <p className={s.muted}>{split.status}</p> : null}
        {split.drafts.length > 0 ? (
          <ul className={s.plain} aria-label="Split proposals">
            {split.drafts.map((draft) => (
              <QueueDraft
                key={draft.id}
                draft={draft}
                names={new Map(list.map((q) => [q.id, q.name]))}
                order={[]}
                onApprove={() => void approveSplit(draft)}
                onDismiss={() => void split.dismiss(draft)}
              />
            ))}
          </ul>
        ) : null}
        {queues !== null && list.length === 0 ? (
          <p className={s.empty}>No queues yet. Make one above, then add issues and epics with Add to queue….</p>
        ) : null}
        {selected !== null ? (
          <div
            role="tabpanel"
            aria-label={list.find((q) => q.id === selected)?.name ?? "Queue"}
            className={s.queuePanel}
          >
            <QueueView key={`${selected}:${revision}`} id={selected} onChanged={() => void read()} />
          </div>
        ) : null}
      </div>
    </Drops>
  );
}
