// SPDX-License-Identifier: AGPL-3.0-or-later
import { useDroppable } from "@dnd-kit/core";
import { useMemo, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cx } from "../components/util";
import { useDragState, type DragState } from "./context";
import s from "./Dnd.module.css";
import { useMergedRef } from "./refs";
import type { Accepts, DropData } from "./types";

/**
 * `idle` with no drag; `available` or `unavailable` (it would refuse) while something is
 * dragged elsewhere; `over` or `refused` while the drag points at it.
 */
export type DropState = "idle" | "available" | "unavailable" | "over" | "refused";

export function dropStateFor(
  drag: DragState,
  targetId: string,
  accepts: Accepts | undefined,
  isOwnDrag = false,
): DropState {
  if (!drag.items) return "idle";
  if (drag.over?.targetId === targetId) return drag.over.refusal ? "refused" : "over";
  return !isOwnDrag && accepts?.(drag.items) ? "unavailable" : "available";
}

export type DropTargetProps = Omit<HTMLAttributes<HTMLDivElement>, "id" | "children"> & {
  /** Unique within the DndProvider; reported to `onDrop`. */
  id: string;
  /** Read out in announcements ("Over Epic A."). */
  label: string;
  data?: unknown;
  /** A refusal (shown and announced) or `null` to take the items. Omitted: takes anything. */
  accepts?: Accepts;
  children: ReactNode;
  ref?: Ref<HTMLDivElement>;
};

/** A place to drop: an epic, a board column, a theme, a Done or Remove zone. */
export function DropTarget({ id, label, data, accepts, children, className, ref, ...rest }: DropTargetProps) {
  const dropData = useMemo<DropData>(
    () => ({ kind: "target", info: data === undefined ? { id, label } : { id, label, data }, accepts }),
    [id, label, data, accepts],
  );
  const { setNodeRef } = useDroppable({ id, data: dropData });
  const setRef = useMergedRef(setNodeRef, ref);
  const drag = useDragState();
  const state = dropStateFor(drag, id, accepts);
  const refusal = state === "refused" ? drag.over?.refusal : null;
  return (
    <div ref={setRef} {...rest} className={cx(s.target, className)} data-drop-state={state}>
      {children}
      {refusal ? (
        <span className={s.reason} data-drop-reason="">
          {refusal.reason}
        </span>
      ) : null}
    </div>
  );
}
