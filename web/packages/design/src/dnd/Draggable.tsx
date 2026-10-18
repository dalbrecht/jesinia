// SPDX-License-Identifier: AGPL-3.0-or-later
import { useDraggable } from "@dnd-kit/core";
import { useMemo, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cx } from "../components/util";
import { useDragState } from "./context";
import s from "./Dnd.module.css";
import { useMergedRef } from "./refs";
import type { DragHandleProps, DragItem, DragSource } from "./types";

/** The sensors own these on the handle, so a caller can't set them (they'd be overridden). */
type SensorOwned = "id" | "children" | "role" | "tabIndex" | "onKeyDown" | "onMouseDown" | "onTouchStart";

export type DraggableProps = Omit<HTMLAttributes<HTMLDivElement>, SensorOwned> & {
  /** Unique among the provider's draggables. */
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
  /**
   * Content, making the whole root the handle (it must then hold no interactive children), or a
   * function that receives `DragHandleProps` to spread onto a dedicated handle element.
   */
  children: ReactNode | ((handle: DragHandleProps) => ReactNode);
  ref?: Ref<HTMLDivElement>;
};

/**
 * Something you can pick up: by pointer, by long-press, or by Space or Enter on its handle.
 * Keys pressed on a control inside the handle never start a drag.
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
    return { items, listId: null, grabbedId: id };
  }, [id, label, data, group]);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef } = useDraggable({ id, data: source, disabled });
  const drag = useDragState();
  const isDragging = drag.listId === null && drag.ids.has(id);
  const isCustomHandle = typeof children === "function";
  const setRootRef = useMergedRef(ref, setNodeRef);
  const setWholeRef = useMergedRef(ref, setNodeRef, setActivatorNodeRef);
  const handle: DragHandleProps = {
    ...attributes,
    ...(listeners as HTMLAttributes<HTMLElement> | undefined),
    ref: setActivatorNodeRef,
    "data-drag-id": id,
  };
  const common = { ...rest, className: cx(s.draggable, className), "data-dragging": isDragging ? "" : undefined };
  if (isCustomHandle) {
    return (
      <div ref={setRootRef} {...common}>
        {children(handle)}
      </div>
    );
  }
  return (
    <div {...common} {...handle} ref={setWholeRef} className={cx(s.draggable, s.handle, className)}>
      {children}
    </div>
  );
}
