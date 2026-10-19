// SPDX-License-Identifier: AGPL-3.0-or-later
// What the drag-and-drop live region says. Plain sentences, the brief's voice: no jargon
// ("droppable"), and every refusal carries its reason.
import type { DragItem, DragNoun } from "./types";

export const DEFAULT_NOUN: DragNoun = { one: "item", other: "items" };

/** "1 issue", "3 issues". */
export function countLabel(count: number, noun: DragNoun): string {
  return `${count} ${count === 1 ? noun.one : noun.other}`;
}

/** One item by its label ("Fix sync"); several by count ("3 issues"). */
export function describeItems(items: readonly DragItem[], noun: DragNoun): string {
  const [first] = items;
  if (items.length === 1 && first?.label) return first.label;
  return countLabel(items.length, noun);
}

function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/** The screen-reader instructions every draggable points at with `aria-describedby`. */
export const DRAG_INSTRUCTIONS =
  "To pick up, press Space or Enter. Use the arrow keys to move between drop targets, " +
  "then press Space or Enter to drop, or Escape to cancel.";

export const dndMessages = {
  pickedUp: (what: string) =>
    `Picked up ${what}. Use the arrow keys to move, Space or Enter to drop, Escape to cancel.`,
  over: (target: string) => `Over ${target}.`,
  overPosition: (list: string, position: number, of: number) => `Over ${list}, position ${position} of ${of}.`,
  refused: (target: string, reason: string) => `Can't drop on ${target}: ${sentence(reason)}`,
  noTarget: () => "Not over a drop target.",
  dropped: (what: string, target: string) => `Dropped ${what} on ${target}.`,
  moved: (what: string, position: number, of: number, list: string) =>
    `Moved ${what} to position ${position} of ${of} in ${list}.`,
  droppedAt: (what: string, list: string, position: number, of: number) =>
    `Dropped ${what} in ${list} at position ${position} of ${of}.`,
  unchanged: (what: string) => `${what} stayed where it was. Nothing changed.`,
  refusedDrop: (target: string, reason: string) => `Can't drop on ${target}: ${sentence(reason)} Nothing changed.`,
  droppedNowhere: (what: string) => `Dropped ${what} outside any drop target. Nothing changed.`,
  cancelled: () => "Drag cancelled. Nothing changed.",
};
