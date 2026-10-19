// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useContext } from "react";
import type { Accepts, DragItem, Refusal } from "./types";

/** Where the drag is pointing now, resolved once by the provider for every target to read. */
export type OverState = {
  /** dnd-kit's droppable id (a DropTarget's id, or a SortableList row or end slot). */
  readonly droppableId: string;
  /** The DropTarget or SortableList id. */
  readonly targetId: string;
  /** The target's or list's label, for announcements. */
  readonly label: string;
  readonly refusal: Refusal | null;
  /** Dropping here would leave the list as it is. */
  readonly isNoop: boolean;
  /**
   * For a SortableList: the insertion slot, and where the first dragged item would land
   * (1-based) of how many. `null` for a DropTarget.
   */
  readonly place: { readonly slot: number; readonly position: number; readonly of: number } | null;
};

export type DragState = {
  readonly items: readonly DragItem[] | null;
  readonly ids: ReadonlySet<string>;
  readonly listId: string | null;
  readonly grabbedId: string | null;
  readonly over: OverState | null;
};

export const IDLE: DragState = { items: null, ids: new Set(), listId: null, grabbedId: null, over: null };

export const DragStateContext = createContext<DragState>(IDLE);

/** The live drag, for Draggable, DropTarget and SortableList to draw their states from. */
export function useDragState(): DragState {
  return useContext(DragStateContext);
}

/**
 * `idle` with no drag; `available` or `unavailable` (it would refuse) while something is
 * dragged elsewhere; `over` or `refused` while the drag points at it.
 */
export type DropState = "idle" | "available" | "unavailable" | "over" | "refused";

/**
 * The state a DropTarget or SortableList draws.
 *
 * @param drag The live drag.
 * @param targetId The DropTarget's or SortableList's id.
 * @param accepts Its `accepts`, asked only while the drag is elsewhere (to dim refusing targets).
 * @param isOwnDrag The drag started in this SortableList: its own rows are always welcome, so
 *   `accepts` (meant for outside items) is not asked.
 * @returns The target's state.
 */
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

/** The element of the DropTarget or SortableList `targetId`, for placing focus after a drop. */
export function findDropElement(targetId: string): HTMLElement | undefined {
  return [...document.querySelectorAll<HTMLElement>("[data-drop-id]")].find((el) => el.dataset.dropId === targetId);
}
