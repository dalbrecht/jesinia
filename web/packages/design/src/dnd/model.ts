// SPDX-License-Identifier: AGPL-3.0-or-later
// Pure drag-and-drop geometry and ordering, kept apart from React so it is tested directly.

/** Pixels from a scroll container's edge where auto-scroll starts. */
export const AUTO_SCROLL_EDGE_PX = 40;
/** The fastest auto-scroll step, in pixels per tick, reached at (or just past) the edge. */
export const AUTO_SCROLL_MAX_STEP_PX = 16;

type Point = { readonly x: number; readonly y: number };
type Box = { readonly top: number; readonly bottom: number; readonly left: number; readonly right: number };

/**
 * The order after moving `moved` to insertion `slot` of `order` (0 = before the first item,
 * `order.length` = the end). The moved items keep their list order and land together. Ids not
 * in the list are ignored; the slot is clamped.
 */
export function moveToSlot(order: readonly string[], moved: readonly string[], slot: number): string[] {
  const movedSet = new Set(moved.filter((id) => order.includes(id)));
  if (movedSet.size === 0) return [...order];
  const clamped = Math.min(Math.max(slot, 0), order.length);
  const before = order.slice(0, clamped).filter((id) => movedSet.has(id)).length;
  const rest = order.filter((id) => !movedSet.has(id));
  const block = order.filter((id) => movedSet.has(id));
  const at = clamped - before;
  return [...rest.slice(0, at), ...block, ...rest.slice(at)];
}

function axisStep(p: number, start: number, end: number, edge: number, maxStep: number): number {
  if (p < start - edge || p > end + edge) return 0;
  if (p < start + edge) return -Math.ceil(Math.min(1, (start + edge - p) / edge) * maxStep);
  if (p > end - edge) return Math.ceil(Math.min(1, (p - (end - edge)) / edge) * maxStep);
  return 0;
}

/**
 * How far to scroll a container this tick for a pointer at `pointer`: zero away from the edges,
 * faster the closer to an edge, at full speed just outside it (so a pointer resting on a header
 * above a list keeps scrolling it), and zero once the pointer is well clear of the container.
 */
export function edgeScrollDelta(
  pointer: Point,
  rect: Box,
  edge: number = AUTO_SCROLL_EDGE_PX,
  maxStep: number = AUTO_SCROLL_MAX_STEP_PX,
): { x: number; y: number } {
  const withinX = pointer.x >= rect.left - edge && pointer.x <= rect.right + edge;
  const withinY = pointer.y >= rect.top - edge && pointer.y <= rect.bottom + edge;
  return {
    x: withinY ? axisStep(pointer.x, rect.left, rect.right, edge, maxStep) : 0,
    y: withinX ? axisStep(pointer.y, rect.top, rect.bottom, edge, maxStep) : 0,
  };
}
