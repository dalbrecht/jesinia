// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MeasuringStrategy,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useDndContext,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
  type DragMoveEvent,
  type DragOverEvent,
  type DragStartEvent,
  type DropAnimation,
  type KeyboardCoordinateGetter,
  type KeyboardSensorOptions,
  type Over,
} from "@dnd-kit/core";
import { add, getEventCoordinates, isKeyboardEvent, type Coordinates } from "@dnd-kit/utilities";
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { VisuallyHidden } from "../components/Spinner";
import { prefersReducedMotion } from "../components/util";
import { DEFAULT_NOUN, DRAG_INSTRUCTIONS, countLabel, describeItems, dndMessages } from "./announcements";
import { DragStateContext, IDLE, type DragState, type OverState } from "./context";
import s from "./Dnd.module.css";
import { edgeScrollDelta, moveToSlot } from "./model";
import type { DragItem, DragNoun, DragSource, DropData, DropTargetInfo, Refusal } from "./types";

/** How long a finger rests on an item before it lifts (spec §6.1: long-press on touch). */
export const LONG_PRESS_MS = 250;
/** How far a finger may wander during the long-press before it counts as a scroll instead. */
const TOUCH_TOLERANCE_PX = 5;
/** How far the mouse moves with the button down before a click becomes a drag. */
const MOUSE_DISTANCE_PX = 4;
/** One auto-scroll tick, about a frame. */
const AUTO_SCROLL_INTERVAL_MS = 16;

// Mirrors --jx-duration-base and --jx-ease: the Web Animations API dnd-kit settles the preview
// with can't read CSS custom properties. Under reduced motion there is no settle at all.
const DROP_ANIMATION: DropAnimation = { duration: 200, easing: "cubic-bezier(0.22, 1, 0.36, 1)" };

/** How the preview settles on drop: not at all under reduced motion. */
export function dropAnimation(): DropAnimation | null {
  return prefersReducedMotion() ? null : DROP_ANIMATION;
}

const MOUSE_OPTIONS = { activationConstraint: { distance: MOUSE_DISTANCE_PX } };
const TOUCH_OPTIONS = { activationConstraint: { delay: LONG_PRESS_MS, tolerance: TOUCH_TOLERANCE_PX } };
const MEASURING = { droppable: { strategy: MeasuringStrategy.Always } };

// The provider speaks through its own live region (it knows insertion positions and refusal
// reasons dnd-kit doesn't), so dnd-kit's announcer stays silent.
const SILENT: Announcements = {
  onDragStart: () => undefined,
  onDragOver: () => undefined,
  onDragEnd: () => undefined,
  onDragCancel: () => undefined,
};
const ACCESSIBILITY = { announcements: SILENT, screenReaderInstructions: { draggable: DRAG_INSTRUCTIONS } };

const KEY_STEPS: Record<string, 1 | -1> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };

export type DndProviderProps = {
  /**
   * A completed drop onto a DropTarget (`position` is `null`) or into a SortableList from outside
   * it (`position` is the insertion slot: 0 before the first row, the list's length at the end).
   * Reorders within one SortableList go to that list's `onMove` instead. Refused drops never
   * arrive here.
   */
  onDrop: (items: readonly DragItem[], target: DropTargetInfo, position: number | null) => void;
  /** How announcements and the preview count things. Defaults to "item" / "items". */
  noun?: DragNoun;
  /** Replaces the default preview (the first label plus a count chip). */
  renderPreview?: (items: readonly DragItem[], refusal: Refusal | null) => ReactNode;
  children: ReactNode;
};

function isSameOrder(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

function isSameOver(a: OverState | null, b: OverState | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.droppableId === b.droppableId &&
    a.place?.slot === b.place?.slot &&
    a.isNoop === b.isNoop &&
    a.refusal?.code === b.refusal?.code &&
    a.refusal?.reason === b.refusal?.reason
  );
}

/** Where dropping the dragged items on `data` would land, for the live region and the line. */
function resolveOver(drag: DragState, droppableId: string, data: DropData, slot: number | null): OverState {
  const items = drag.items ?? [];
  const base = { droppableId, targetId: data.info.id, label: data.info.label };
  if (data.kind === "target" || slot === null) {
    return { ...base, refusal: data.accepts?.(items) ?? null, isNoop: false, place: null };
  }
  if (drag.listId === data.listId) {
    const next = moveToSlot(data.order, [...drag.ids], slot);
    const position = next.findIndex((id) => drag.ids.has(id)) + 1;
    const isNoop = isSameOrder(next, data.order);
    return { ...base, refusal: null, isNoop, place: { slot, position, of: data.order.length } };
  }
  const refusal = data.accepts?.(items) ?? null;
  return { ...base, refusal, isNoop: false, place: { slot, position: slot + 1, of: data.order.length + items.length } };
}

function announceOver(over: OverState | null): string {
  if (over === null) return dndMessages.noTarget();
  if (over.refusal) return dndMessages.refused(over.label, over.refusal.reason);
  if (over.place) return dndMessages.overPosition(over.label, over.place.position, over.place.of);
  return dndMessages.over(over.label);
}

/** Whether a droppable is somewhere the keyboard should stop: not a dragged row, not a no-op slot. */
function isKeyboardStop(data: DropData | undefined, drag: DragState): data is DropData {
  if (!data) return false;
  if (data.kind === "target" || data.listId !== drag.listId) return true;
  if (data.itemId !== null && drag.ids.has(data.itemId)) return false;
  return !isSameOrder(moveToSlot(data.order, [...drag.ids], data.slot), data.order);
}

function follows(anchor: Node, node: Node): boolean {
  return (anchor.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

function isScrollContainer(element: HTMLElement): boolean {
  const style = getComputedStyle(element);
  const scrolls = /(auto|scroll|overlay)/.test(`${style.overflowY} ${style.overflowX}`);
  return scrolls && (element.scrollHeight > element.clientHeight || element.scrollWidth > element.clientWidth);
}

/** Scrollable ancestors of each node, innermost first, the over node's before the active one's. */
function scrollContainers(...nodes: (HTMLElement | null)[]): HTMLElement[] {
  const found: HTMLElement[] = [];
  for (const node of nodes) {
    for (let at = node?.parentElement ?? null; at; at = at.parentElement) {
      if (!found.includes(at) && isScrollContainer(at)) found.push(at);
    }
  }
  return found;
}

function scrollAxis(element: HTMLElement, axis: "Top" | "Left", step: number): boolean {
  if (step === 0) return false;
  const max = axis === "Top" ? element.scrollHeight - element.clientHeight : element.scrollWidth - element.clientWidth;
  const current = element[`scroll${axis}`];
  const next = Math.min(Math.max(current + step, 0), max);
  if (next === current) return false;
  element[`scroll${axis}`] = next;
  return true;
}

/**
 * Scrolls the list or board under the pointer while it rests near an edge. dnd-kit's own
 * auto-scroll only scrolls the containers around the dragged item, so dragging from the Backlog
 * into a long queue in another pane would never scroll the queue; this one also scrolls the
 * containers around the target under the pointer.
 */
function AutoScroller({ pointer, isEnabled }: { pointer: MutableRefObject<Coordinates | null>; isEnabled: boolean }) {
  const { activeNode, over, droppableContainers } = useDndContext();
  const nodes = useRef<(HTMLElement | null)[]>([]);
  nodes.current = [over ? (droppableContainers.get(over.id)?.node.current ?? null) : null, activeNode];

  useEffect(() => {
    if (!isEnabled) return;
    const timer = setInterval(() => {
      const at = pointer.current;
      if (!at) return;
      for (const container of scrollContainers(...nodes.current)) {
        const step = edgeScrollDelta(at, container.getBoundingClientRect());
        const movedY = scrollAxis(container, "Top", step.y);
        const movedX = scrollAxis(container, "Left", step.x);
        if (movedY || movedX) break;
      }
    }, AUTO_SCROLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [isEnabled, pointer]);
  return null;
}

function DefaultPreview({
  items,
  noun,
  refusal,
}: {
  items: readonly DragItem[];
  noun: DragNoun;
  refusal: Refusal | null;
}) {
  const count = countLabel(items.length, noun);
  return (
    <>
      <span className={s.previewLabel}>{items[0]?.label ?? count}</span>
      {items.length > 1 ? (
        <span className={s.previewCount} data-drag-count="">
          {count}
        </span>
      ) : null}
      {refusal ? <span className={s.previewReason}>{refusal.reason}</span> : null}
    </>
  );
}

/**
 * The one drag-and-drop context an app mounts. Pointer (mouse and trackpad), touch (long-press)
 * and keyboard sensors; auto-scroll near the edges of scrolling lists; a drag preview with a
 * count; and a live region that announces every pick-up, move, refusal and drop.
 */
export function DndProvider({ onDrop, noun = DEFAULT_NOUN, renderPreview, children }: DndProviderProps) {
  const [drag, setDrag] = useState<DragState>(IDLE);
  const [message, setMessage] = useState("");
  const [keyboardTarget, setKeyboardTarget] = useState<string | null>(null);
  const [isPointerDrag, setIsPointerDrag] = useState(false);
  const dragRef = useRef<DragState>(IDLE);
  const overDataRef = useRef<DropData | null>(null);
  const keyboardTargetRef = useRef<string | null>(null);
  const isKeyboardDragRef = useRef(false);
  const startRef = useRef<Coordinates | null>(null);
  const pointerRef = useRef<Coordinates | null>(null);

  const update = useCallback((next: DragState) => {
    dragRef.current = next;
    setDrag(next);
  }, []);

  const moveKeyboardTarget = useCallback((id: string | null) => {
    keyboardTargetRef.current = id;
    setKeyboardTarget(id);
  }, []);

  const refresh = useCallback(
    (over: Over | null) => {
      const current = dragRef.current;
      if (!current.items) return;
      const data = (over?.data.current as DropData | undefined) ?? null;
      let next: OverState | null = null;
      if (over && data) {
        let slot = data.kind === "slot" ? data.slot : null;
        const pointer = pointerRef.current;
        // A row's lower half means "after this row".
        if (slot !== null && data.kind === "slot" && data.isRow && pointer && !isKeyboardDragRef.current) {
          if (pointer.y > over.rect.top + over.rect.height / 2) slot += 1;
        }
        next = resolveOver(current, String(over.id), data, slot);
      }
      overDataRef.current = data;
      if (isSameOver(current.over, next)) return;
      update({ ...current, over: next });
      setMessage(announceOver(next));
    },
    [update],
  );

  const finish = useCallback(() => {
    update(IDLE);
    overDataRef.current = null;
    moveKeyboardTarget(null);
    startRef.current = null;
    pointerRef.current = null;
    setIsPointerDrag(false);
  }, [update, moveKeyboardTarget]);

  const onDragStart = useCallback(
    (event: DragStartEvent) => {
      const source = event.active.data.current as DragSource | undefined;
      const items = source?.items ?? [{ id: String(event.active.id) }];
      const isKeyboard = isKeyboardEvent(event.activatorEvent);
      isKeyboardDragRef.current = isKeyboard;
      startRef.current = isKeyboard ? null : getEventCoordinates(event.activatorEvent);
      pointerRef.current = startRef.current;
      setIsPointerDrag(!isKeyboard);
      moveKeyboardTarget(null);
      update({ items, ids: new Set(items.map((item) => item.id)), listId: source?.listId ?? null, over: null });
      setMessage(dndMessages.pickedUp(describeItems(items, noun)));
    },
    [noun, update, moveKeyboardTarget],
  );

  const onDragMove = useCallback(
    (event: DragMoveEvent) => {
      // Keyboard targets come from the arrow keys (onDragOver), never from geometry.
      if (isKeyboardDragRef.current || !startRef.current) return;
      pointerRef.current = add(startRef.current, event.delta);
      refresh(event.over);
    },
    [refresh],
  );

  const onDragOver = useCallback((event: DragOverEvent) => refresh(event.over), [refresh]);

  const onDragEnd = useCallback(
    (_event: DragEndEvent) => {
      const current = dragRef.current;
      const over = current.over;
      const data = overDataRef.current;
      const items = current.items ?? [];
      const what = describeItems(items, noun);
      finish();
      if (!over || !data) {
        setMessage(dndMessages.droppedNowhere(what));
      } else if (over.refusal) {
        setMessage(dndMessages.refusedDrop(over.label, over.refusal.reason));
      } else if (data.kind === "target" || over.place === null) {
        onDrop(items, data.info, null);
        setMessage(dndMessages.dropped(what, over.label));
      } else if (over.isNoop) {
        setMessage(dndMessages.unchanged(what));
      } else if (current.listId === data.listId) {
        const { slot, position, of } = over.place;
        data.reorder(moveToSlot(data.order, [...current.ids], slot), items);
        setMessage(dndMessages.moved(what, position, of, over.label));
      } else {
        const { slot, position, of } = over.place;
        onDrop(items, data.info, slot);
        setMessage(dndMessages.droppedAt(what, over.label, position, of));
      }
    },
    [noun, onDrop, finish],
  );

  const onDragCancel = useCallback(() => {
    finish();
    setMessage(dndMessages.cancelled());
  }, [finish]);

  // Arrow keys step through the drop targets in document order. The chosen target is held in
  // state, so collision detection (below) reports it whatever the geometry says.
  const keyboardOptions = useMemo<KeyboardSensorOptions>(() => {
    const coordinateGetter: KeyboardCoordinateGetter = (event, { context }) => {
      const step = KEY_STEPS[event.code];
      if (!step) return undefined;
      event.preventDefault();
      const current = dragRef.current;
      const stops = context.droppableContainers
        .getEnabled()
        .filter((container) => isKeyboardStop(container.data.current as DropData | undefined, current))
        .flatMap((container) =>
          container.node.current ? [{ id: String(container.id), node: container.node.current }] : [],
        )
        .sort((a, b) => (follows(a.node, b.node) ? -1 : 1));
      if (stops.length === 0) return undefined;
      const at = stops.findIndex((stop) => stop.id === keyboardTargetRef.current);
      let next: number;
      if (at === -1) {
        const anchor = context.activeNode;
        const after = anchor ? stops.findIndex((stop) => follows(anchor, stop.node)) : -1;
        const before = anchor ? stops.findLastIndex((stop) => follows(stop.node, anchor)) : -1;
        if (step > 0) next = after === -1 ? stops.length - 1 : after;
        else next = before === -1 ? 0 : before;
      } else {
        next = Math.min(Math.max(at + step, 0), stops.length - 1);
        if (next === at) return undefined;
      }
      const target = stops[next] as { id: string };
      moveKeyboardTarget(target.id);
      const rect = context.droppableRects.get(target.id);
      return rect ? { x: rect.left, y: rect.top } : undefined;
    };
    return { coordinateGetter };
  }, [moveKeyboardTarget]);

  const sensors = useSensors(
    useSensor(MouseSensor, MOUSE_OPTIONS),
    useSensor(TouchSensor, TOUCH_OPTIONS),
    useSensor(KeyboardSensor, keyboardOptions),
  );

  const collisionDetection = useCallback<CollisionDetection>(
    (args) => {
      if (isKeyboardDragRef.current) {
        const isKnown = args.droppableContainers.some((container) => container.id === keyboardTarget);
        return keyboardTarget !== null && isKnown ? [{ id: keyboardTarget }] : [];
      }
      return pointerWithin(args);
    },
    [keyboardTarget],
  );

  const refusal = drag.over?.refusal ?? null;
  return (
    <DragStateContext.Provider value={drag}>
      <DndContext
        sensors={sensors}
        collisionDetection={collisionDetection}
        measuring={MEASURING}
        autoScroll={false}
        accessibility={ACCESSIBILITY}
        onDragStart={onDragStart}
        onDragMove={onDragMove}
        onDragOver={onDragOver}
        onDragEnd={onDragEnd}
        onDragCancel={onDragCancel}
      >
        {children}
        <AutoScroller pointer={pointerRef} isEnabled={isPointerDrag} />
        <DragOverlay dropAnimation={dropAnimation()}>
          {drag.items ? (
            <div className={s.preview} data-drag-preview="" data-refused={refusal ? "" : undefined} aria-hidden="true">
              {renderPreview ? (
                renderPreview(drag.items, refusal)
              ) : (
                <DefaultPreview items={drag.items} noun={noun} refusal={refusal} />
              )}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
      <div role="status" aria-live="assertive" aria-atomic="true" data-dnd-announcer="">
        <VisuallyHidden>{message}</VisuallyHidden>
      </div>
    </DragStateContext.Provider>
  );
}
