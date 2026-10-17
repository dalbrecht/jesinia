// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { age, archiveStatus, PostList, ROW_HEIGHT } from "../PostList";
import { post } from "./fixtures";

const NOW = Date.parse("2026-10-16T12:00:00Z");
const many = Array.from({ length: 60 }, (_, i) => post(i + 1));

afterEach(() => vi.unstubAllGlobals());

describe("age", () => {
  it("reads as minutes, hours, days, then the date", () => {
    expect(age("2026-10-16T11:55:00Z", NOW)).toBe("5m");
    expect(age("2026-10-16T12:01:00Z", NOW)).toBe("0m");
    expect(age("2026-10-16T09:00:00Z", NOW)).toBe("3h");
    expect(age("2026-10-13T12:00:00Z", NOW)).toBe("3d");
    expect(age("2026-08-01T12:00:00Z", NOW)).toBe("1 Aug");
    expect(age("not a date", NOW)).toBe("");
  });
});

describe("archiveStatus", () => {
  it("a kept post with no archive state is in the KB", () => {
    expect(archiveStatus(post(1))).toBeNull();
    expect(archiveStatus(post(1, { kept_page_id: "k" }))).toBe("done");
    expect(archiveStatus(post(1, { kept_page_id: "k", archive_state: "archiving" }))).toBe("archiving");
    expect(archiveStatus(post(1, { kept_page_id: "k", archive_state: "failed" }))).toBe("failed");
  });
});

describe("PostList", () => {
  const props = { onSelect: () => undefined, onEndReached: () => undefined, empty: "none" };

  it("scrolls the selection into view, down and back up", () => {
    const view = render(<PostList posts={many} selectedId={null} {...props} />);
    const scroller = screen.getByTestId("post-scroller");
    view.rerender(<PostList posts={many} selectedId={40} {...props} />);
    expect(scroller.scrollTop).toBe(40 * ROW_HEIGHT - 720);
    expect(screen.getByRole("option", { name: /Post 40\b/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-activedescendant", "post-40");
    view.rerender(<PostList posts={many} selectedId={2} {...props} />);
    expect(scroller.scrollTop).toBe(ROW_HEIGHT);
    // Already in view: no jump.
    view.rerender(<PostList posts={many} selectedId={3} {...props} />);
    expect(scroller.scrollTop).toBe(ROW_HEIGHT);
  });

  it("measures the scroller and follows its size", () => {
    let resize: () => void = () => undefined;
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(fn: () => void) {
          resize = fn;
        }
        observe() {}
        disconnect = disconnect;
      },
    );
    const view = render(<PostList posts={many} selectedId={null} {...props} />);
    const scroller = screen.getByTestId("post-scroller");
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: ROW_HEIGHT * 2 });
    resize();
    view.rerender(<PostList posts={many} selectedId={null} {...props} />);
    // Two rows in view plus the overscan below.
    expect(screen.getAllByRole("option")).toHaveLength(2 + 6);
    view.unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it("an empty list says so", () => {
    render(<PostList posts={[]} selectedId={null} {...props} empty="Nothing here." />);
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
