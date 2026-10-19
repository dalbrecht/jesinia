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
import { isKeyboardEvent, type Coordinates } from "@dnd-kit/utilities";
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { VisuallyHidden } from "../components/Spinner";
import { prefersReducedMotion } from "../components/util";
import { DEFAULT_NOUN, DRAG_INSTRUCTIONS, countLabel, describeItems, dndMessages } from "./announcements";
import { DragStateContext, IDLE, findDropElement, type DragState, type OverState } from "./context";
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

// Tab cancels rather than commits (dnd-kit's default drops on Tab): moving focus away from a
// keyboard drag must never stage a change.
const KEYBOARD_CODES = { start: ["Space", "Enter"], cancel: ["Escape", "Tab"], end: ["Space", "Enter"] };

const KEY_STEPS: Record<string, 1 | -1> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };

/** The keys a keyboard drag reacts to once picked up. */
const DRAG_KEYS = new Set([...Object.keys(KEY_STEPS), ...KEYBOARD_CODES.end, ...KEYBOARD_CODES.cancel]);

/**
 * dnd-kit's keyboard sensor starts listening a task after the pick-up (so the activating key
 * doesn't also drop), and a browser may run input before that timer: an arrow pressed at once
 * would be lost (IR62). From the pick-up until the sensor listens, drag keys are held here,
 * then replayed in order, one per task. Returns the cleanup (stops holding, drops the rest).
 */
function holdEarlyKeys(activator: Event): () => void {
  const target = activator.target instanceof Node ? activator.target : null;
  const doc = target?.ownerDocument ?? document;
  const held: KeyboardEvent[] = [];
  const timers: ReturnType<typeof setTimeout>[] = [];
  const replayed = new WeakSet<Event>();
  let isListening = false; // the sensor's listener is attached: replaying now
  const stop = () => doc.removeEventListener("keydown", hold, true);
  function hold(e: KeyboardEvent) {
    if (e === activator || replayed.has(e) || !DRAG_KEYS.has(e.code)) return;
    // Until every held key has replayed, a new one queues behind them (never overtakes).
    e.preventDefault();
    e.stopImmediatePropagation();
    held.push(e);
    if (isListening && held.length === 1) timers.push(setTimeout(replay));
  }
  function replay() {
    const next = held.shift();
    if (!next) {
      stop();
      return;
    }
    const at = doc.activeElement ?? doc.body;
    const { key, code, shiftKey, altKey, ctrlKey, metaKey } = next;
    const again = new KeyboardEvent("keydown", { key, code, shiftKey, altKey, ctrlKey, metaKey, bubbles: true, cancelable: true }); // prettier-ignore
    replayed.add(again);
    at.dispatchEvent(again);
    timers.push(setTimeout(replay));
  }
  doc.addEventListener("keydown", hold, true);
  // The sensor's timer was set after this one, in the same task: two hops land after it.
  timers.push(
    setTimeout(() =>
      timers.push(
        setTimeout(() => {
          isListening = true;
          replay();
        }),
      ),
    ),
  );
  return () => {
    stop();
    held.length = 0;
    for (const timer of timers) clearTimeout(timer);
  };
}

export type DndProviderProps = {
  /**
   * A completed drop onto a DropTarget (`position` is `null`) or into a SortableList from outside
   * it (`position` is the insertion slot: 0 before the first row, the list's length at the end).
   * Reorders within one SortableList go to that list's `onMove` instead. Refused drops never
   * arrive here.
   */
  onDrop: (items: readonly DragItem[], target: DropTargetInfo, position: number | null) => void;
  /**
   * After a keyboard drop: the item id whose handle takes focus next (preferring one inside the
   * target). Omitted, or returning `undefined`, keeps focus where it is; if the drop removed the
   * focused handle, focus moves to the dropped item's handle in the target, or else the target.
   */
  focusAfterDrop?: (items: readonly DragItem[], target: DropTargetInfo) => string | undefined;
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
  const added = items.filter((item) => !data.order.includes(item.id)).length;
  return { ...base, refusal, isNoop: false, place: { slot, position: slot + 1, of: data.order.length + added } };
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

/**
 * Scrollable ancestors of each node, innermost first, the over node's before the active one's,
 * then the document itself when it scrolls (its root reports `overflow: visible`).
 */
function scrollContainers(...nodes: (HTMLElement | null)[]): HTMLElement[] {
  const found: HTMLElement[] = [];
  for (const node of nodes) {
    for (let at = node?.parentElement ?? null; at; at = at.parentElement) {
      if (!found.includes(at) && isScrollContainer(at)) found.push(at);
    }
  }
  const page = document.scrollingElement as HTMLElement | null;
  if (page && !found.includes(page) && page.scrollHeight > page.clientHeight) found.push(page);
  return found;
}

/** A container's visible box; for the document, the viewport. */
function visibleRect(element: HTMLElement): { top: number; bottom: number; left: number; right: number } {
  if (element === document.scrollingElement) {
    return { top: 0, left: 0, bottom: window.innerHeight, right: window.innerWidth };
  }
  return element.getBoundingClientRect();
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
  const overNode = over ? (droppableContainers.get(over.id)?.node.current ?? null) : null;
  // Walking ancestors reads computed styles, so it happens when the nodes change, not every tick.
  const containers = useRef<HTMLElement[]>([]);
  useEffect(() => {
    containers.current = isEnabled ? scrollContainers(overNode, activeNode) : [];
  }, [isEnabled, overNode, activeNode]);

  // A fixed tick, not requestAnimationFrame: the speed (px per tick) then doesn't double on a
  // 120 Hz display.
  useEffect(() => {
    if (!isEnabled) return;
    const timer = setInterval(() => {
      const at = pointer.current;
      if (!at) return;
      for (const container of containers.current) {
        const step = edgeScrollDelta(at, visibleRect(container));
        const movedY = scrollAxis(container, "Top", step.y);
        const movedX = scrollAxis(container, "Left", step.x);
        if (movedY || movedX) break;
      }
    }, AUTO_SCROLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [isEnabled, pointer]);
  return null;
}

/**
 * After a keyboard drop: focus the handle the app named, or, if the drop removed the focused
 * handle, the dropped item's handle (preferring one inside the target), or else the target.
 * Runs after the app's re-render and dnd-kit's own focus restore (a frame, then a task).
 */
function placeFocusAfterDrop(preferred: string | null, items: readonly DragItem[], targetId: string) {
  requestAnimationFrame(() => {
    setTimeout(() => {
      const isLost = document.activeElement === null || document.activeElement === document.body;
      if (preferred === null && !isLost) return;
      const target = findDropElement(targetId);
      const handles = [...document.querySelectorAll<HTMLElement>("[data-drag-id]")];
      const handleFor = (id: string) => {
        const matches = handles.filter((handle) => handle.dataset.dragId === id);
        return matches.find((handle) => target?.contains(handle)) ?? matches[0];
      };
      const wanted = preferred === null ? undefined : handleFor(preferred);
      const fallback = isLost ? (items.map((item) => handleFor(item.id)).find(Boolean) ?? target) : undefined;
      (wanted ?? fallback)?.focus();
    });
  });
}

function DefaultPreview({
  items,
  grabbedId,
  noun,
  refusal,
}: {
  items: readonly DragItem[];
  grabbedId: string | null;
  noun: DragNoun;
  refusal: Refusal | null;
}) {
  const count = countLabel(items.length, noun);
  const grabbed = items.find((item) => item.id === grabbedId) ?? items[0];
  return (
    <>
      <span className={s.previewLabel}>{grabbed?.label ?? count}</span>
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
export function DndProvider({
  onDrop,
  focusAfterDrop,
  noun = DEFAULT_NOUN,
  renderPreview,
  children,
}: DndProviderProps) {
  const [drag, setDrag] = useState<DragState>(IDLE);
  const [message, setMessage] = useState("");
  const [keyboardTarget, setKeyboardTarget] = useState<string | null>(null);
  const [isPointerDrag, setIsPointerDrag] = useState(false);
  const dragRef = useRef<DragState>(IDLE);
  const overDataRef = useRef<DropData | null>(null);
  const keyboardTargetRef = useRef<string | null>(null);
  const releaseKeysRef = useRef<(() => void) | null>(null);
  useEffect(() => () => releaseKeysRef.current?.(), []);
  const isKeyboardDragRef = useRef(false);
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
      // A pointer crossing the gaps between targets would interrupt speech at every gap.
      if (next !== null || isKeyboardDragRef.current) setMessage(announceOver(next));
    },
    [update],
  );

  const finish = useCallback(() => {
    releaseKeysRef.current?.();
    releaseKeysRef.current = null;
    update(IDLE);
    overDataRef.current = null;
    moveKeyboardTarget(null);
    pointerRef.current = null;
    setIsPointerDrag(false);
  }, [update, moveKeyboardTarget]);

  const onDragStart = useCallback(
    (event: DragStartEvent) => {
      const source = event.active.data.current as DragSource | undefined;
      const items = source?.items ?? [{ id: String(event.active.id) }];
      const isKeyboard = isKeyboardEvent(event.activatorEvent);
      isKeyboardDragRef.current = isKeyboard;
      releaseKeysRef.current?.();
      releaseKeysRef.current = isKeyboard && event.activatorEvent ? holdEarlyKeys(event.activatorEvent) : null;
      pointerRef.current = null;
      setIsPointerDrag(!isKeyboard);
      moveKeyboardTarget(null);
      update({
        items,
        ids: new Set(items.map((item) => item.id)),
        listId: source?.listId ?? null,
        grabbedId: source?.grabbedId ?? String(event.active.id),
        over: null,
      });
      setMessage(dndMessages.pickedUp(describeItems(items, noun)));
    },
    [noun, update, moveKeyboardTarget],
  );

  const onDragMove = useCallback(
    (event: DragMoveEvent) => {
      // Keyboard targets come from the arrow keys (onDragOver), never from geometry. The pointer
      // itself is read in collision detection (it runs first, on every move and scroll).
      if (isKeyboardDragRef.current) return;
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
      const isKeyboard = isKeyboardDragRef.current;
      finish();
      const settleFocus = (target: DropTargetInfo) => {
        if (isKeyboard) placeFocusAfterDrop(focusAfterDrop?.(items, target) ?? null, items, target.id);
      };
      if (!over || !data) {
        // A list row that never left its list didn't go anywhere.
        setMessage(current.listId === null ? dndMessages.droppedNowhere(what) : dndMessages.unchanged(what));
      } else if (over.refusal) {
        setMessage(dndMessages.refusedDrop(over.label, over.refusal.reason));
      } else if (data.kind === "target" || over.place === null) {
        onDrop(items, data.info, null);
        settleFocus(data.info);
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
        settleFocus(data.info);
        setMessage(dndMessages.droppedAt(what, over.label, position, of));
      }
    },
    [noun, onDrop, focusAfterDrop, finish],
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
    return {
      coordinateGetter,
      keyboardCodes: KEYBOARD_CODES,
      scrollBehavior: prefersReducedMotion() ? "auto" : "smooth",
    };
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
      // The real pointer: activation point plus translate, with no scroll adjustment, so
      // auto-scroll and the half-row rule see where the pointer actually is.
      if (args.pointerCoordinates) pointerRef.current = args.pointerCoordinates;
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
                <DefaultPreview items={drag.items} grabbedId={drag.grabbedId} noun={noun} refusal={refusal} />
              )}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
      <div aria-live="assertive" aria-atomic="true" data-dnd-announcer="">
        <VisuallyHidden>{message}</VisuallyHidden>
      </div>
    </DragStateContext.Provider>
  );
}
