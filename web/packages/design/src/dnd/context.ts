// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useContext } from "react";
import type { DragItem, Refusal } from "./types";

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
  readonly over: OverState | null;
};

export const IDLE: DragState = { items: null, ids: new Set(), listId: null, over: null };

export const DragStateContext = createContext<DragState>(IDLE);

/** The live drag, for Draggable, DropTarget and SortableList to draw their states from. */
export function useDragState(): DragState {
  return useContext(DragStateContext);
}
