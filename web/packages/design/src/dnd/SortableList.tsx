// SPDX-License-Identifier: AGPL-3.0-or-later
import { useDroppable } from "@dnd-kit/core";
import { SortableContext, useSortable } from "@dnd-kit/sortable";
import { useMemo, type ReactNode } from "react";
import { cx } from "../components/util";
import { useDragState } from "./context";
import s from "./Dnd.module.css";
import { dropStateFor } from "./DropTarget";
import type { Accepts, DragItem, DragSource, DropData, DropTargetInfo } from "./types";

export type SortableListProps<T extends DragItem> = {
  /** Unique within the DndProvider; reported to `onDrop` for items dropped in from outside. */
  id: string;
  /** The list's accessible name, also read out in announcements ("Over Next up, position 2 of 5."). */
  label: string;
  items: readonly T[];
  /** A reorder within this list: the new order of ids, and the items that moved. */
  onMove: (order: string[], moved: readonly T[]) => void;
  renderItem: (item: T, state: { index: number; isDragging: boolean }) => ReactNode;
  /** Selected ids; picking up a selected row drags every selected row, in list order. */
  selection?: readonly string[];
  /** For items dragged in from outside: a refusal or `null`. Reorders within the list are always allowed. */
  accepts?: Accepts;
  data?: unknown;
  className?: string;
};

type SlotBase = Omit<Extract<DropData, { kind: "slot" }>, "slot" | "isRow" | "itemId">;

const END = "\u0000end";

function rowId(listId: string, itemId: string): string {
  return `${listId}\u0000${itemId}`;
}

function SortableRow({
  data,
  isDragging,
  showLine,
  children,
}: {
  data: DropData & DragSource & { itemId: string };
  isDragging: boolean;
  showLine: boolean;
  children: ReactNode;
}) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef } = useSortable({
    id: rowId(data.info.id, data.itemId),
    data,
    attributes: { roleDescription: "draggable" },
  });
  return (
    <li ref={setNodeRef} className={s.row} data-sortable-row="" data-insert-before={showLine ? "" : undefined}>
      <div
        ref={setActivatorNodeRef}
        {...attributes}
        {...listeners}
        className={s.handle}
        data-dragging={isDragging ? "" : undefined}
      >
        {children}
      </div>
    </li>
  );
}

function EndSlot({ data, showLine }: { data: DropData; showLine: boolean }) {
  const { setNodeRef } = useDroppable({ id: rowId(data.info.id, END), data });
  return (
    <li
      ref={setNodeRef}
      className={s.end}
      aria-hidden="true"
      data-sortable-end=""
      data-insert-before={showLine ? "" : undefined}
    />
  );
}

/**
 * An ordered list you reorder by dragging (a queue, an epic's children). Rows stay put while
 * dragging; an insertion line shows where they will land, which reads the same for one row, a
 * multi-selection, or rows dragged in from elsewhere.
 */
export function SortableList<T extends DragItem>({
  id,
  label,
  items,
  onMove,
  renderItem,
  selection,
  accepts,
  data,
  className,
}: SortableListProps<T>) {
  const drag = useDragState();
  const order = useMemo(() => items.map((item) => item.id), [items]);
  const base = useMemo<SlotBase>(() => {
    const info: DropTargetInfo = data === undefined ? { id, label } : { id, label, data };
    const reorder = (next: string[], moved: readonly DragItem[]) => onMove(next, moved as readonly T[]);
    return { kind: "slot", info, accepts, listId: id, order, reorder };
  }, [id, label, data, accepts, order, onMove]);

  const isOwnDrag = drag.listId === id;
  const state = dropStateFor(drag, id, accepts, isOwnDrag);
  const over = drag.over?.targetId === id ? drag.over : null;
  const lineSlot = over?.place && !over.refusal && !over.isNoop ? over.place.slot : null;
  const selected = new Set(selection ?? []);

  return (
    <div className={cx(s.sortable, className)} data-drop-state={state}>
      <SortableContext id={id} items={order.map((itemId) => rowId(id, itemId))}>
        <ul className={s.list} aria-label={label}>
          {items.map((item, index) => {
            const dragItems = selected.has(item.id) ? items.filter((other) => selected.has(other.id)) : [item];
            const isDragging = isOwnDrag && drag.ids.has(item.id);
            return (
              <SortableRow
                key={item.id}
                data={{ ...base, slot: index, isRow: true, itemId: item.id, items: dragItems }}
                isDragging={isDragging}
                showLine={lineSlot === index}
              >
                {renderItem(item, { index, isDragging })}
              </SortableRow>
            );
          })}
          <EndSlot
            data={{ ...base, slot: items.length, isRow: false, itemId: null }}
            showLine={lineSlot === items.length}
          />
        </ul>
      </SortableContext>
      {over?.refusal ? (
        <p className={s.reason} data-drop-reason="">
          {over.refusal.reason}
        </p>
      ) : null}
    </div>
  );
}
