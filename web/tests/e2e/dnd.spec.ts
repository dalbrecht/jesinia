// SPDX-License-Identifier: AGPL-3.0-or-later
// The drag-and-drop primitives in a real browser (/design/drag.html): what jsdom can't show,
// real layout and real scroll events.
import { expect, test, type Page } from "@playwright/test";

const rowTexts = (page: Page) =>
  page.locator("[data-sortable-row]").evaluateAll((rows) => rows.map((row) => row.textContent ?? ""));

/** The row whose slot the line marks, worked out from the real geometry under the pointer. */
const expectedLine = (page: Page, x: number, y: number) =>
  page.evaluate(
    ([px, py]) => {
      const row = document
        .elementsFromPoint(px, py)
        .map((element) => element.closest("[data-sortable-row]"))
        .find(Boolean);
      if (!row) return null;
      const box = row.getBoundingClientRect();
      const marked = py > box.top + box.height / 2 ? row.nextElementSibling : row;
      return marked?.hasAttribute("data-sortable-end") ? "end" : (marked?.textContent ?? null);
    },
    [x, y],
  );

const lineAt = (page: Page) =>
  page.evaluate(() => {
    const marked = document.querySelector("[data-insert-before]");
    if (!marked) return null;
    return marked.hasAttribute("data-sortable-end") ? "end" : marked.textContent;
  });

test("auto-scroll keeps going while a drag rests near the edge, and the line marks where the drop lands", async ({
  page,
}) => {
  await page.goto("/design/drag.html");
  const pane = page.getByTestId("pane");
  const paneBox = await pane.boundingBox();
  const handleBox = await page.getByRole("button", { name: "Row 0" }).boundingBox();
  if (!paneBox || !handleBox) throw new Error("the fixture did not lay out");
  const x = handleBox.x + handleBox.width / 2;

  await page.mouse.move(x, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(x, handleBox.y + handleBox.height / 2 + 12, { steps: 3 });
  await page.mouse.move(x, paneBox.y + paneBox.height - 8, { steps: 6 });

  // Before the fix this stalled at about 80 px; resting near the edge must keep scrolling.
  await expect.poll(() => pane.evaluate((el) => el.scrollTop), { timeout: 5000 }).toBeGreaterThan(400);

  const restY = paneBox.y + paneBox.height / 2 + 5;
  await page.mouse.move(x, restY, { steps: 4 });
  const scrolled = await pane.evaluate((el) => el.scrollTop);
  expect(scrolled).toBeGreaterThan(400);
  const expected = await expectedLine(page, x, restY);
  expect(expected).not.toBeNull();
  await expect.poll(() => lineAt(page)).toBe(expected);

  await page.mouse.up();
  await expect(page.locator("[data-dnd-announcer]")).toContainText("Moved Row 0 to position");
  const order = await rowTexts(page);
  const landed = order.indexOf("Row 0");
  expect(order[landed + 1]).toBe(expected);
});

test("a keyboard drag onto a closed epic shows why it can't drop, and Tab cancels", async ({ page }) => {
  await page.goto("/design/drag.html");
  await page.getByRole("button", { name: "New issue" }).focus();
  await page.keyboard.press("Space");
  const announcer = page.locator("[data-dnd-announcer]");
  await expect(announcer).toContainText("Picked up New issue.");
  await page.keyboard.press("ArrowDown");
  await expect(announcer).toHaveText("Over Done.");
  await page.keyboard.press("ArrowDown");
  await expect(announcer).toHaveText("Can't drop on Locked epic: This epic is closed.");
  await expect(page.locator("[data-drop-state=refused]")).toContainText("This epic is closed");
  await page.keyboard.press("Tab");
  await expect(announcer).toHaveText("Drag cancelled. Nothing changed.");
  await expect(page.getByTestId("log")).toHaveText("Nothing dropped yet.");
});
