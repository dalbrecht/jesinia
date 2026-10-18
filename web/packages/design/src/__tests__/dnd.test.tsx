// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useState, type ReactNode } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { dropAnimation } from "../dnd/DndProvider";
import {
  DndProvider,
  Draggable,
  DropTarget,
  LONG_PRESS_MS,
  SortableList,
  type DragItem,
  type DropTargetInfo,
  type Refusal,
} from "../index";

const issues = { one: "issue", other: "issues" };
type OnDrop = (items: readonly DragItem[], target: DropTargetInfo, position: number | null) => void;

const announcer = () => document.querySelector("[data-dnd-announcer]");
const said = () => announcer()?.textContent ?? "";
const preview = () => document.querySelector("[data-drag-preview]");
const dropState = (name: string) =>
  screen.getByText(name).closest("[data-drop-state]")?.getAttribute("data-drop-state");

/** Lets the drag overlay finish its post-drop update inside act. */
const settle = () => act(async () => {});

beforeAll(() => {
  // jsdom has no scrollIntoView; the keyboard sensor calls it on pick-up.
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function Board({
  onDrop,
  accepts,
  children,
}: {
  onDrop: OnDrop;
  accepts?: (items: readonly DragItem[]) => Refusal | null;
  children?: ReactNode;
}) {
  return (
    <DndProvider onDrop={onDrop} noun={issues}>
      <Draggable id="1" label="Fix sync" data={{ n: 1 }}>
        Fix sync
      </Draggable>
      <DropTarget id="epic-a" label="Epic A">
        Epic A
      </DropTarget>
      <DropTarget id="epic-b" label="Epic B" data={{ kind: "epic" }} {...(accepts ? { accepts } : {})}>
        Epic B
      </DropTarget>
      {children}
    </DndProvider>
  );
}

async function pickUp(name: string) {
  const user = userEvent.setup();
  const handle = screen.getByRole("button", { name: (accessibleName) => accessibleName.includes(name) });
  handle.focus();
  await user.keyboard("[Space]");
  await waitFor(() => expect(said()).toMatch(/^Picked up/));
  return user;
}

describe("DndProvider with Draggable and DropTarget (keyboard)", () => {
  it("has an empty live region from the start and describes how to drag", () => {
    render(<Board onDrop={vi.fn()} />);
    expect(announcer()).toHaveAttribute("aria-live", "assertive");
    expect(said()).toBe("");
    const handle = screen.getByRole("button", { name: "Fix sync" });
    expect(handle).toHaveAttribute("aria-roledescription", "draggable");
    expect(handle).toHaveAccessibleDescription(/press Space or Enter/);
  });

  it("picks up, moves between targets with the arrows, and drops", async () => {
    const onDrop = vi.fn<OnDrop>();
    render(<Board onDrop={onDrop} />);
    const user = await pickUp("Fix sync");
    expect(said()).toBe("Picked up Fix sync. Use the arrow keys to move, Space or Enter to drop, Escape to cancel.");
    expect(dropState("Epic A")).toBe("available");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Epic A."));
    expect(dropState("Epic A")).toBe("over");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Epic B."));
    await user.keyboard("[ArrowDown]"); // already at the last target: stays
    await user.keyboard("[ArrowUp]");
    await waitFor(() => expect(said()).toBe("Over Epic A."));
    await user.keyboard("[ArrowRight]");
    await waitFor(() => expect(said()).toBe("Over Epic B."));
    await user.keyboard("[Space]");
    await waitFor(() => expect(onDrop).toHaveBeenCalledOnce());
    expect(onDrop).toHaveBeenCalledWith(
      [{ id: "1", label: "Fix sync", data: { n: 1 } }],
      { id: "epic-b", label: "Epic B", data: { kind: "epic" } },
      null,
    );
    expect(said()).toBe("Dropped Fix sync on Epic B.");
    expect(dropState("Epic B")).toBe("idle");
  });

  it("drops with Enter too, and says so when dropped outside any target", async () => {
    const onDrop = vi.fn<OnDrop>();
    render(<Board onDrop={onDrop} />);
    const user = await pickUp("Fix sync");
    await user.keyboard("[Enter]");
    await waitFor(() => expect(said()).toBe("Dropped Fix sync outside any drop target. Nothing changed."));
    expect(onDrop).not.toHaveBeenCalled();
  });

  it("Escape cancels the drag and nothing is dropped", async () => {
    const onDrop = vi.fn<OnDrop>();
    render(<Board onDrop={onDrop} />);
    const user = await pickUp("Fix sync");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(dropState("Epic A")).toBe("over"));
    await user.keyboard("[Escape]");
    await waitFor(() => expect(said()).toBe("Drag cancelled. Nothing changed."));
    expect(onDrop).not.toHaveBeenCalled();
    expect(dropState("Epic A")).toBe("idle");
    expect(preview()).toBeNull();
  });

  it("a refused target shows its reason, announces it, and refuses the drop", async () => {
    const onDrop = vi.fn<OnDrop>();
    const accepts = vi.fn(() => ({ code: "self", reason: "An epic can't contain itself" }));
    render(<Board onDrop={onDrop} accepts={accepts} />);
    const user = await pickUp("Fix sync");
    expect(dropState("Epic B")).toBe("unavailable");
    expect(screen.queryByText("An epic can't contain itself")).toBeNull();
    await user.keyboard("[ArrowDown][ArrowDown]");
    await waitFor(() => expect(said()).toBe("Can't drop on Epic B: An epic can't contain itself."));
    expect(dropState("Epic B")).toBe("refused");
    const reasons = screen.getAllByText("An epic can't contain itself");
    expect(reasons.some((r) => r.closest("[data-drop-state]"))).toBe(true);
    expect(preview()).toHaveAttribute("data-refused");
    expect(accepts).toHaveBeenCalledWith([{ id: "1", label: "Fix sync", data: { n: 1 } }]);
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toBe("Can't drop on Epic B: An epic can't contain itself. Nothing changed."));
    expect(onDrop).not.toHaveBeenCalled();
  });

  it("dragging one of a multi-selection carries the whole selection and its count", async () => {
    const onDrop = vi.fn<OnDrop>();
    const selection = [
      { id: "1", label: "Fix sync" },
      { id: "2", label: "Tidy tray" },
      { id: "3", label: "Add theme" },
    ];
    render(
      <DndProvider onDrop={onDrop} noun={issues}>
        {selection.map((item) => (
          <Draggable key={item.id} id={item.id} label={item.label} group={selection}>
            {item.label}
          </Draggable>
        ))}
        <Draggable id="4" label="Lonely" group={selection}>
          Lonely
        </Draggable>
        <DropTarget id="epic" label="Epic A">
          Epic A
        </DropTarget>
      </DndProvider>,
    );
    const user = await pickUp("Tidy tray");
    expect(said()).toMatch(/^Picked up 3 issues\./);
    expect(preview()).toHaveTextContent("3 issues");
    expect(preview()?.querySelector("[data-drag-count]")).toHaveTextContent("3 issues");
    for (const name of ["Fix sync", "Tidy tray", "Add theme"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("data-dragging");
    }
    expect(screen.getByRole("button", { name: "Lonely" })).not.toHaveAttribute("data-dragging");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Epic A."));
    await user.keyboard("[Space]");
    await waitFor(() => expect(onDrop).toHaveBeenCalledOnce());
    expect(onDrop.mock.calls[0]?.[0]).toEqual(selection);
    expect(said()).toBe("Dropped 3 issues on Epic A.");
  });

  it("a draggable outside the selection drags alone, with no count", async () => {
    render(
      <DndProvider onDrop={vi.fn()} noun={issues}>
        <Draggable id="4" label="Lonely" group={[{ id: "1" }, { id: "2" }]}>
          Lonely
        </Draggable>
      </DndProvider>,
    );
    await pickUp("Lonely");
    expect(said()).toMatch(/^Picked up Lonely\./);
    expect(preview()).toHaveTextContent("Lonely");
    expect(preview()?.querySelector("[data-drag-count]")).toBeNull();
  });

  it("renders a custom preview", async () => {
    render(
      <DndProvider onDrop={vi.fn()} renderPreview={(items) => <span>Moving {items.length}</span>}>
        <Draggable id="1" label="Fix sync">
          Fix sync
        </Draggable>
      </DndProvider>,
    );
    await pickUp("Fix sync");
    expect(preview()).toHaveTextContent("Moving 1");
  });

  it("the first arrow goes to the nearest target in that direction", async () => {
    render(
      <DndProvider onDrop={vi.fn()}>
        <DropTarget id="epic-a" label="Epic A">
          Epic A
        </DropTarget>
        <DropTarget id="epic-b" label="Epic B">
          Epic B
        </DropTarget>
        <Draggable id="1" label="Fix sync">
          Fix sync
        </Draggable>
        <DropTarget id="epic-c" label="Epic C">
          Epic C
        </DropTarget>
      </DndProvider>,
    );
    let user = await pickUp("Fix sync");
    await user.keyboard("[ArrowUp]");
    await waitFor(() => expect(said()).toBe("Over Epic B."));
    await user.keyboard("[Escape]");
    await waitFor(() => expect(said()).toBe("Drag cancelled. Nothing changed."));
    user = await pickUp("Fix sync");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Epic C."));
  });

  it("with nothing in the arrow's direction, the first arrow goes to the nearest target the other way", async () => {
    const { unmount } = render(<Board onDrop={vi.fn()} />); // the draggable comes first
    let user = await pickUp("Fix sync");
    await user.keyboard("[ArrowUp]");
    await waitFor(() => expect(said()).toBe("Over Epic A."));
    await user.keyboard("[Escape]");
    unmount();
    render(
      <DndProvider onDrop={vi.fn()}>
        <DropTarget id="epic-a" label="Epic A">
          Epic A
        </DropTarget>
        <DropTarget id="epic-b" label="Epic B">
          Epic B
        </DropTarget>
        <Draggable id="1" label="Fix sync">
          Fix sync
        </Draggable>
      </DndProvider>,
    );
    user = await pickUp("Fix sync");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Epic B."));
  });

  it("forwards refs and native attributes to the root elements", () => {
    const handleRef = createRef<HTMLDivElement>();
    let target: HTMLDivElement | null = null;
    render(
      <DndProvider onDrop={vi.fn()}>
        <Draggable id="1" label="Fix sync" ref={handleRef} className="row" title="Drag to move">
          Fix sync
        </Draggable>
        <DropTarget
          id="epic"
          label="Epic A"
          ref={(node) => {
            target = node;
          }}
          aria-label="Epic A drop zone"
        >
          Epic A
        </DropTarget>
      </DndProvider>,
    );
    expect(handleRef.current).toBe(screen.getByRole("button", { name: "Fix sync" }));
    expect(handleRef.current).toHaveAttribute("title", "Drag to move");
    expect(handleRef.current).toHaveClass("row");
    expect(target).toHaveAttribute("aria-label", "Epic A drop zone");
  });

  it("a disabled draggable can't be picked up", async () => {
    render(
      <DndProvider onDrop={vi.fn()}>
        <Draggable id="1" label="Fix sync" disabled>
          Fix sync
        </Draggable>
      </DndProvider>,
    );
    const handle = screen.getByRole("button", { name: "Fix sync" });
    handle.focus();
    await userEvent.keyboard("[Space]");
    expect(said()).toBe("");
  });
});

describe("touch long-press", () => {
  function touch(target: Element, type: "touchStart" | "touchMove" | "touchEnd", x: number, y: number) {
    const touches = type === "touchEnd" ? [] : [{ clientX: x, clientY: y, identifier: 0, target }];
    fireEvent[type](target, { touches, changedTouches: [{ clientX: x, clientY: y, identifier: 0, target }] });
  }

  it(`lifts after a ${LONG_PRESS_MS} ms press and not before`, async () => {
    vi.useFakeTimers();
    render(<Board onDrop={vi.fn()} />);
    const handle = screen.getByRole("button", { name: "Fix sync" });
    expect(LONG_PRESS_MS).toBe(250);
    touch(handle, "touchStart", 10, 10);
    act(() => vi.advanceTimersByTime(LONG_PRESS_MS - 1));
    expect(said()).toBe("");
    act(() => vi.advanceTimersByTime(1));
    expect(said()).toMatch(/^Picked up Fix sync\./);
    touch(handle, "touchEnd", 10, 10);
    act(() => vi.advanceTimersByTime(10));
    expect(said()).toBe("Dropped Fix sync outside any drop target. Nothing changed.");
    await settle();
  });

  it("a finger that moves before the press completes scrolls instead of lifting", () => {
    vi.useFakeTimers();
    render(<Board onDrop={vi.fn()} />);
    const handle = screen.getByRole("button", { name: "Fix sync" });
    touch(handle, "touchStart", 10, 10);
    act(() => vi.advanceTimersByTime(100));
    touch(handle, "touchMove", 10, 60);
    act(() => vi.advanceTimersByTime(LONG_PRESS_MS));
    expect(said()).toBe("");
  });
});

/** Gives each element a fixed box; jsdom lays nothing out. */
function layout(element: Element, box: { top: number; left?: number; width?: number; height: number }) {
  const left = box.left ?? 0;
  const width = box.width ?? 300;
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
    top: box.top,
    left,
    width,
    height: box.height,
    bottom: box.top + box.height,
    right: left + width,
    x: left,
    y: box.top,
    toJSON: () => ({}),
  });
}

function scrollable(element: HTMLElement, contentHeight: number, viewportHeight: number) {
  let top = 0;
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: contentHeight });
  Object.defineProperty(element, "clientHeight", { configurable: true, value: viewportHeight });
  Object.defineProperty(element, "scrollWidth", { configurable: true, value: 300 });
  Object.defineProperty(element, "clientWidth", { configurable: true, value: 300 });
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = value;
    },
  });
}

describe("auto-scroll", () => {
  it("scrolls a list while the pointer rests near its edge, and stops away from it", async () => {
    vi.useFakeTimers();
    render(
      <DndProvider onDrop={vi.fn()}>
        <div data-testid="pane" style={{ overflowY: "auto", height: 200 }}>
          <Draggable id="1" label="Fix sync">
            Fix sync
          </Draggable>
        </div>
      </DndProvider>,
    );
    const pane = screen.getByTestId("pane");
    scrollable(pane, 1000, 200);
    layout(pane, { top: 0, height: 200 });
    const handle = screen.getByRole("button", { name: "Fix sync" });
    layout(handle, { top: 0, height: 40 });

    fireEvent.mouseDown(handle, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.mouseMove(document, { clientX: 10, clientY: 30 });
    fireEvent.mouseMove(document, { clientX: 10, clientY: 195 });
    act(() => vi.advanceTimersByTime(160));
    const scrolled = pane.scrollTop;
    expect(scrolled).toBeGreaterThan(0);

    fireEvent.mouseMove(document, { clientX: 10, clientY: 100 });
    act(() => vi.advanceTimersByTime(160));
    expect(pane.scrollTop).toBe(scrolled);

    fireEvent.mouseMove(document, { clientX: 10, clientY: 2 });
    act(() => vi.advanceTimersByTime(5000));
    expect(pane.scrollTop).toBe(0);

    fireEvent.mouseMove(document, { clientX: 10, clientY: 199 });
    fireEvent.mouseUp(document, { clientX: 10, clientY: 199 });
    const atDrop = pane.scrollTop;
    act(() => vi.advanceTimersByTime(500));
    expect(pane.scrollTop).toBe(atDrop);
    await settle();
  });

  it("scrolls the outer container once the inner one can't go further", async () => {
    vi.useFakeTimers();
    render(
      <DndProvider onDrop={vi.fn()}>
        <div data-testid="outer" style={{ overflowY: "auto" }}>
          <div data-testid="inner" style={{ overflowY: "auto" }}>
            <Draggable id="1" label="Fix sync">
              Fix sync
            </Draggable>
          </div>
        </div>
      </DndProvider>,
    );
    const outer = screen.getByTestId("outer");
    const inner = screen.getByTestId("inner");
    scrollable(outer, 1000, 200);
    scrollable(inner, 400, 200);
    inner.scrollTop = 200; // already at its bottom
    layout(outer, { top: 0, height: 200 });
    layout(inner, { top: 0, height: 200 });
    const handle = screen.getByRole("button", { name: "Fix sync" });
    layout(handle, { top: 0, height: 40 });

    fireEvent.mouseDown(handle, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.mouseMove(document, { clientX: 10, clientY: 30 });
    fireEvent.mouseMove(document, { clientX: 10, clientY: 195 });
    act(() => vi.advanceTimersByTime(160));
    expect(inner.scrollTop).toBe(200);
    expect(outer.scrollTop).toBeGreaterThan(0);
    fireEvent.mouseUp(document, { clientX: 10, clientY: 195 });
    await settle();
  });

  it("does not scroll during keyboard drags", async () => {
    render(
      <DndProvider onDrop={vi.fn()}>
        <div data-testid="pane" style={{ overflowY: "auto" }}>
          <Draggable id="1" label="Fix sync">
            Fix sync
          </Draggable>
        </div>
      </DndProvider>,
    );
    const pane = screen.getByTestId("pane");
    scrollable(pane, 1000, 200);
    layout(pane, { top: 0, height: 200 });
    await pickUp("Fix sync");
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(pane.scrollTop).toBe(0);
  });
});

type Row = { id: string; label: string };
const rows: Row[] = ["a", "b", "c", "d"].map((id) => ({ id, label: `Issue ${id.toUpperCase()}` }));

function Queue({
  onMove,
  onDrop = vi.fn(),
  selection,
  accepts,
  extra,
  initial = rows,
}: {
  onMove?: (order: string[], moved: readonly DragItem[]) => void;
  onDrop?: OnDrop;
  selection?: readonly string[];
  accepts?: (items: readonly DragItem[]) => Refusal | null;
  extra?: ReactNode;
  initial?: Row[];
}) {
  const [items, setItems] = useState(initial);
  return (
    <DndProvider onDrop={onDrop} noun={issues}>
      {extra}
      <SortableList
        id="next-up"
        label="Next up"
        items={items}
        {...(selection ? { selection } : {})}
        {...(accepts ? { accepts } : {})}
        onMove={(order, moved) => {
          onMove?.(order, moved);
          setItems(order.map((id) => items.find((i) => i.id === id) as Row));
        }}
        renderItem={(item, { index }) => (
          <span>
            {index + 1}. {item.label}
          </span>
        )}
      />
    </DndProvider>
  );
}

const rowTexts = () => [...document.querySelectorAll("[data-sortable-row]")].map((r) => r.textContent);
const lineBefore = () => {
  const marked = document.querySelector("[data-insert-before]");
  if (!marked) return null;
  return marked.hasAttribute("data-sortable-end") ? "end" : marked.textContent;
};

describe("SortableList", () => {
  it("renders an ordered list of draggable rows", () => {
    render(<Queue />);
    expect(screen.getByRole("list", { name: "Next up" })).toBeInTheDocument();
    expect(rowTexts()).toEqual(["1. Issue A", "2. Issue B", "3. Issue C", "4. Issue D"]);
    expect(screen.getByRole("button", { name: "2. Issue B" })).toHaveAttribute("aria-roledescription", "draggable");
  });

  it("reorders with the keyboard, skipping positions that change nothing, with an insertion line", async () => {
    const onMove = vi.fn();
    const onDrop = vi.fn<OnDrop>();
    render(<Queue onMove={onMove} onDrop={onDrop} />);
    const user = await pickUp("2. Issue B");
    expect(lineBefore()).toBeNull();
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 3 of 4."));
    expect(lineBefore()).toBe("4. Issue D");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 4 of 4."));
    expect(lineBefore()).toBe("end");
    await user.keyboard("[ArrowUp][ArrowUp]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 1 of 4."));
    expect(lineBefore()).toBe("1. Issue A");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 3 of 4."));
    await user.keyboard("[Space]");
    await waitFor(() => expect(onMove).toHaveBeenCalledOnce());
    expect(onMove).toHaveBeenCalledWith(["a", "c", "b", "d"], [{ id: "b", label: "Issue B" }]);
    expect(onDrop).not.toHaveBeenCalled();
    expect(said()).toBe("Moved Issue B to position 3 of 4 in Next up.");
    await waitFor(() => expect(rowTexts()).toEqual(["1. Issue A", "2. Issue C", "3. Issue B", "4. Issue D"]));
    expect(lineBefore()).toBeNull();
  });

  it("moves a multi-selection together", async () => {
    const onMove = vi.fn();
    render(<Queue onMove={onMove} selection={["c", "a"]} />);
    const user = await pickUp("1. Issue A");
    expect(said()).toMatch(/^Picked up 2 issues\./);
    expect(screen.getByRole("button", { name: "3. Issue C" })).toHaveAttribute("data-dragging");
    await user.keyboard("[ArrowDown][ArrowDown][ArrowDown]");
    await waitFor(() => expect(lineBefore()).toBe("end"));
    await user.keyboard("[Space]");
    await waitFor(() => expect(onMove).toHaveBeenCalledOnce());
    expect(onMove.mock.calls[0]?.[0]).toEqual(["b", "d", "a", "c"]);
    expect(said()).toBe("Moved 2 issues to position 3 of 4 in Next up.");
  });

  it("drops an item from elsewhere at a position, through the provider", async () => {
    const onDrop = vi.fn<OnDrop>();
    const onMove = vi.fn();
    render(
      <Queue
        onDrop={onDrop}
        onMove={onMove}
        extra={
          <Draggable id="x" label="New issue">
            New issue
          </Draggable>
        }
      />,
    );
    const user = await pickUp("New issue");
    await user.keyboard("[ArrowDown][ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 2 of 5."));
    expect(lineBefore()).toBe("2. Issue B");
    await user.keyboard("[Space]");
    await waitFor(() => expect(onDrop).toHaveBeenCalledOnce());
    expect(onDrop).toHaveBeenCalledWith([{ id: "x", label: "New issue" }], { id: "next-up", label: "Next up" }, 1);
    expect(onMove).not.toHaveBeenCalled();
    expect(said()).toBe("Dropped New issue in Next up at position 2 of 5.");
  });

  it("a list that refuses outside items shows why, but still reorders its own", async () => {
    const onDrop = vi.fn<OnDrop>();
    const accepts = () => ({ code: "duplicate", reason: "Already in this queue" });
    render(
      <Queue
        onDrop={onDrop}
        accepts={accepts}
        extra={
          <Draggable id="x" label="New issue">
            New issue
          </Draggable>
        }
      />,
    );
    const user = await pickUp("New issue");
    await user.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Can't drop on Next up: Already in this queue."));
    expect(lineBefore()).toBeNull();
    expect(screen.getByRole("list", { name: "Next up" }).closest("[data-drop-state]")).toHaveAttribute(
      "data-drop-state",
      "refused",
    );
    await user.keyboard("[Escape]");
    await waitFor(() => expect(said()).toBe("Drag cancelled. Nothing changed."));
    const again = await pickUp("1. Issue A");
    await again.keyboard("[ArrowDown]");
    await waitFor(() => expect(said()).toBe("Over Next up, position 2 of 4."));
  });

  it("dropping a row back where it was changes nothing", async () => {
    const onMove = vi.fn();
    render(<Queue onMove={onMove} initial={rows.slice(0, 1)} />);
    const user = await pickUp("1. Issue A");
    await user.keyboard("[ArrowDown]"); // the only other slot is a no-op, so there is nowhere to go
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toBe("Dropped Issue A outside any drop target. Nothing changed."));
    expect(onMove).not.toHaveBeenCalled();
  });

  it("shows the insertion line on the half of the row under the pointer, and drops there", async () => {
    vi.useFakeTimers();
    const onMove = vi.fn();
    render(<Queue onMove={onMove} />);
    const items = [...document.querySelectorAll("[data-sortable-row]")];
    items.forEach((row, index) => layout(row, { top: index * 40, height: 40 }));
    layout(document.querySelector("[data-sortable-end]") as Element, { top: 160, height: 8 });
    const handle = screen.getByRole("button", { name: "1. Issue A" });

    fireEvent.mouseDown(handle, { button: 0, clientX: 20, clientY: 20 });
    fireEvent.mouseMove(document, { clientX: 20, clientY: 30 });
    fireEvent.mouseMove(document, { clientX: 20, clientY: 110 }); // lower half of C
    act(() => vi.advanceTimersByTime(20));
    expect(lineBefore()).toBe("4. Issue D");
    fireEvent.mouseMove(document, { clientX: 20, clientY: 85 }); // upper half of C
    act(() => vi.advanceTimersByTime(20));
    expect(lineBefore()).toBe("3. Issue C");
    fireEvent.mouseMove(document, { clientX: 20, clientY: 10 }); // over itself: nothing would change
    act(() => vi.advanceTimersByTime(20));
    expect(lineBefore()).toBeNull();
    fireEvent.mouseMove(document, { clientX: 20, clientY: 150 }); // lower half of D
    act(() => vi.advanceTimersByTime(20));
    expect(lineBefore()).toBe("end");
    fireEvent.mouseUp(document, { clientX: 20, clientY: 150 });
    act(() => vi.advanceTimersByTime(20));
    expect(onMove).toHaveBeenCalledWith(["b", "c", "d", "a"], [{ id: "a", label: "Issue A" }]);
    await settle();
  });
});

describe("SortableList pointer no-op", () => {
  it("dropping a row back on itself says nothing changed", async () => {
    vi.useFakeTimers();
    const onMove = vi.fn();
    render(<Queue onMove={onMove} />);
    [...document.querySelectorAll("[data-sortable-row]")].forEach((row, index) =>
      layout(row, { top: index * 40, height: 40 }),
    );
    const handle = screen.getByRole("button", { name: "2. Issue B" });
    fireEvent.mouseDown(handle, { button: 0, clientX: 20, clientY: 50 });
    fireEvent.mouseMove(document, { clientX: 20, clientY: 60 });
    fireEvent.mouseMove(document, { clientX: 20, clientY: 70 });
    act(() => vi.advanceTimersByTime(20));
    expect(said()).toBe("Over Next up, position 2 of 4.");
    expect(lineBefore()).toBeNull();
    fireEvent.mouseUp(document, { clientX: 20, clientY: 70 });
    act(() => vi.advanceTimersByTime(20));
    expect(said()).toBe("Issue B stayed where it was. Nothing changed.");
    expect(onMove).not.toHaveBeenCalled();
    await settle();
  });

  it("passes the list's data to onDrop for items dropped in from outside", async () => {
    const onDrop = vi.fn<OnDrop>();
    render(
      <DndProvider onDrop={onDrop}>
        <Draggable id="x" label="New issue">
          New issue
        </Draggable>
        <SortableList
          id="q"
          label="Queue"
          data={{ queue: 7 }}
          items={[{ id: "a", label: "A" }]}
          onMove={vi.fn()}
          renderItem={(item) => item.label}
        />
      </DndProvider>,
    );
    const user = await pickUp("New issue");
    await user.keyboard("[ArrowDown][ArrowDown][Space]");
    await waitFor(() => expect(onDrop).toHaveBeenCalledOnce());
    expect(onDrop).toHaveBeenCalledWith(
      [{ id: "x", label: "New issue" }],
      { id: "q", label: "Queue", data: { queue: 7 } },
      1,
    );
  });
});

describe("reduced motion", () => {
  function prefersReduced(reduce: boolean) {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (query: string) => ({ matches: reduce && query.includes("reduce"), media: query }) as MediaQueryList,
    });
  }
  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("settles the preview over the base duration, or not at all under reduced motion", () => {
    prefersReduced(false);
    expect(dropAnimation()).toEqual({ duration: 200, easing: "cubic-bezier(0.22, 1, 0.36, 1)" });
    prefersReduced(true);
    expect(dropAnimation()).toBeNull();
  });

  it("still drags and drops under reduced motion", async () => {
    prefersReduced(true);
    const onDrop = vi.fn<OnDrop>();
    render(<Board onDrop={onDrop} />);
    const user = await pickUp("Fix sync");
    await user.keyboard("[ArrowDown][Space]");
    await waitFor(() => expect(onDrop).toHaveBeenCalledOnce());
    expect(preview()).toBeNull();
  });
});
