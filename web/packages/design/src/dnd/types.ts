// SPDX-License-Identifier: AGPL-3.0-or-later
/** One thing being dragged: an issue, an epic, a queue row. `id` is unique within its provider. */
export type DragItem = {
  readonly id: string;
  /** Read out in announcements ("Picked up Fix sync.") and shown in the drag preview. */
  readonly label?: string;
  readonly data?: unknown;
};

/**
 * Why a target won't take the dragged items (ruling IR17): a typed refusal with a reason a
 * person can read, never a string-or-bool. `code` is for the app; `reason` is shown and announced.
 */
export type Refusal = { readonly code: string; readonly reason: string };

/** Returns a refusal, or `null` when the target takes the items. */
export type Accepts = (items: readonly DragItem[]) => Refusal | null;

/** The target that received a drop, as `DndProvider.onDrop` reports it. */
export type DropTargetInfo = { readonly id: string; readonly label: string; readonly data?: unknown };

/** How announcements and the preview count things: `{ one: "issue", other: "issues" }`. */
export type DragNoun = { readonly one: string; readonly other: string };

/** What a draggable carries (dnd-kit's active `data`). */
export type DragSource = {
  readonly items: readonly DragItem[];
  /** The SortableList the items come from, or `null` for a plain Draggable. */
  readonly listId: string | null;
};

/** What a drop target carries (dnd-kit's droppable `data`). */
export type DropData =
  | { readonly kind: "target"; readonly info: DropTargetInfo; readonly accepts: Accepts | undefined }
  | {
      readonly kind: "slot";
      readonly info: DropTargetInfo;
      readonly accepts: Accepts | undefined;
      readonly listId: string;
      /** The insertion slot this stop stands for: 0 is before the first row, `order.length` the end. */
      readonly slot: number;
      /** A row (whose lower half means the slot after it) or the list's end slot. */
      readonly isRow: boolean;
      /** The row's item id, or `null` for the end slot. */
      readonly itemId: string | null;
      readonly order: readonly string[];
      readonly reorder: (order: string[], moved: readonly DragItem[]) => void;
    };
