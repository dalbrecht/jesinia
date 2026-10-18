// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";
import { countLabel, describeItems, dndMessages } from "../dnd/announcements";
import { edgeScrollDelta, moveToSlot } from "../index";

const issues = { one: "issue", other: "issues" };

describe("moveToSlot", () => {
  const order = ["a", "b", "c", "d"];

  it("moves one item forward and back by insertion slot", () => {
    expect(moveToSlot(order, ["a"], 3)).toEqual(["b", "c", "a", "d"]);
    expect(moveToSlot(order, ["d"], 0)).toEqual(["d", "a", "b", "c"]);
    expect(moveToSlot(order, ["b"], 4)).toEqual(["a", "c", "d", "b"]);
  });

  it("keeps a non-contiguous selection together in list order", () => {
    expect(moveToSlot(order, ["c", "a"], 4)).toEqual(["b", "d", "a", "c"]);
    expect(moveToSlot(order, ["b", "d"], 0)).toEqual(["b", "d", "a", "c"]);
  });

  it("returns the same order for a slot next to the item itself", () => {
    expect(moveToSlot(order, ["b"], 1)).toEqual(order);
    expect(moveToSlot(order, ["b"], 2)).toEqual(order);
  });

  it("ignores ids that aren't in the list and clamps the slot", () => {
    expect(moveToSlot(order, ["x"], 1)).toEqual(order);
    expect(moveToSlot(order, ["a"], 99)).toEqual(["b", "c", "d", "a"]);
    expect(moveToSlot(order, ["d"], -3)).toEqual(["d", "a", "b", "c"]);
  });
});

describe("edgeScrollDelta", () => {
  const rect = { top: 100, bottom: 300, left: 0, right: 400 };

  it("does nothing away from the edges", () => {
    expect(edgeScrollDelta({ x: 200, y: 200 }, rect)).toEqual({ x: 0, y: 0 });
  });

  it("scrolls up near the top and down near the bottom, faster closer in", () => {
    const nearTop = edgeScrollDelta({ x: 200, y: 110 }, rect);
    const atTop = edgeScrollDelta({ x: 200, y: 100 }, rect);
    expect(nearTop.y).toBeLessThan(0);
    expect(atTop.y).toBeLessThan(nearTop.y);
    expect(edgeScrollDelta({ x: 200, y: 295 }, rect).y).toBeGreaterThan(0);
  });

  it("keeps the top speed just outside the container, but not far away", () => {
    expect(edgeScrollDelta({ x: 200, y: 90 }, rect)).toEqual(edgeScrollDelta({ x: 200, y: 100 }, rect));
    expect(edgeScrollDelta({ x: 200, y: 20 }, rect)).toEqual({ x: 0, y: 0 });
    expect(edgeScrollDelta({ x: 900, y: 110 }, rect)).toEqual({ x: 0, y: 0 });
  });

  it("scrolls sideways near the left and right edges", () => {
    expect(edgeScrollDelta({ x: 5, y: 200 }, rect).x).toBeLessThan(0);
    expect(edgeScrollDelta({ x: 398, y: 200 }, rect).x).toBeGreaterThan(0);
  });
});

describe("announcements", () => {
  const one = [{ id: "1", label: "Fix sync" }];
  const three = [{ id: "1", label: "Fix sync" }, { id: "2" }, { id: "3" }];

  it("names one item by its label and many by count", () => {
    expect(describeItems(one, issues)).toBe("Fix sync");
    expect(describeItems([{ id: "9" }], issues)).toBe("1 issue");
    expect(describeItems(three, issues)).toBe("3 issues");
    expect(countLabel(1, issues)).toBe("1 issue");
    expect(countLabel(2, issues)).toBe("2 issues");
  });

  it("reads as plain sentences", () => {
    expect(dndMessages.pickedUp("3 issues")).toBe(
      "Picked up 3 issues. Use the arrow keys to move, Space or Enter to drop, Escape to cancel.",
    );
    expect(dndMessages.over("Epic A")).toBe("Over Epic A.");
    expect(dndMessages.overPosition("Next up", 2, 5)).toBe("Over Next up, position 2 of 5.");
    expect(dndMessages.refused("Epic A", "An epic can't contain itself")).toBe(
      "Can't drop on Epic A: An epic can't contain itself.",
    );
    expect(dndMessages.refused("Epic A", "Already there.")).toBe("Can't drop on Epic A: Already there.");
    expect(dndMessages.noTarget()).toBe("Not over a drop target.");
    expect(dndMessages.dropped("3 issues", "Epic A")).toBe("Dropped 3 issues on Epic A.");
    expect(dndMessages.moved("Fix sync", 3, 4, "Next up")).toBe("Moved Fix sync to position 3 of 4 in Next up.");
    expect(dndMessages.droppedAt("Fix sync", "Next up", 1, 3)).toBe("Dropped Fix sync in Next up at position 1 of 3.");
    expect(dndMessages.unchanged("Fix sync")).toBe("Fix sync stayed where it was. Nothing changed.");
    expect(dndMessages.refusedDrop("Epic A", "Already there")).toBe(
      "Can't drop on Epic A: Already there. Nothing changed.",
    );
    expect(dndMessages.droppedNowhere("Fix sync")).toBe("Dropped Fix sync outside any drop target. Nothing changed.");
    expect(dndMessages.cancelled()).toBe("Drag cancelled. Nothing changed.");
  });
});
