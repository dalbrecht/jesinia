// SPDX-License-Identifier: AGPL-3.0-or-later
import { useDraggable } from "@dnd-kit/core";
import { useMemo, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cx } from "../components/util";
import { useDragState } from "./context";
import s from "./Dnd.module.css";
import { useMergedRef } from "./refs";
import type { DragItem, DragSource } from "./types";

export type DraggableProps = Omit<HTMLAttributes<HTMLDivElement>, "id" | "children"> & {
  /** Unique within the DndProvider. */
  id: string;
  /** Read out when picked up and shown in the preview. */
  label: string;
  data?: unknown;
  /**
   * The multi-selection this item belongs to. When it includes this item, picking the item up
   * drags the whole group; otherwise the item drags alone.
   */
  group?: readonly DragItem[];
  disabled?: boolean;
  children: ReactNode;
  ref?: Ref<HTMLDivElement>;
};

/**
 * Makes its root the drag handle: pointer, long-press and keyboard (Space or Enter) all lift it.
 * The root's onMouseDown, onTouchStart and onKeyDown belong to the drag sensors.
 */
export function Draggable({
  id,
  label,
  data,
  group,
  disabled = false,
  children,
  className,
  ref,
  ...rest
}: DraggableProps) {
  const source = useMemo<DragSource>(() => {
    const self: DragItem = data === undefined ? { id, label } : { id, label, data };
    const items = group?.some((item) => item.id === id) ? group : [self];
    return { items, listId: null };
  }, [id, label, data, group]);
  const { attributes, listeners, setNodeRef } = useDraggable({ id, data: source, disabled });
  const setRef = useMergedRef(setNodeRef, ref);
  const drag = useDragState();
  const isDragging = drag.listId === null && drag.ids.has(id);
  return (
    <div
      ref={setRef}
      {...rest}
      {...attributes}
      {...listeners}
      className={cx(s.draggable, className)}
      data-dragging={isDragging ? "" : undefined}
    >
      {children}
    </div>
  );
}
