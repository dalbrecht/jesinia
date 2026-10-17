// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";
const kb = join(home, "kb");
const ROUND_TRIP = join(kb, "Ideas", "Round Trip.md");
const FRONT = readFileSync(ROUND_TRIP, "utf8").split("Intro paragraph.")[0] ?? "";
const PROVENANCE =
  "<!-- jesinia:ai agent=claude-code model=m at=2026-10-01T00:00:00Z session=s1 -->\nEarlier AI text.\n<!-- /jesinia:ai -->";

function token(): string {
  const m = /^token = "([^"]+)"/m.exec(readFileSync(join(home, "data", "config.toml"), "utf8"));
  if (!m?.[1]) throw new Error("no token");
  return m[1];
}

async function login(page: Page) {
  const auth = { Authorization: `Bearer ${token()}` };
  const set = await page.request.put("/api/kb/path", { headers: auth, data: { path: kb } });
  expect(set.ok()).toBe(true);
  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);
}

async function openBySearch(page: Page, q: string, title: string) {
  await page.getByRole("searchbox", { name: "Search notes" }).fill(q);
  await page.getByTestId("search-results").getByRole("button", { name: title }).first().click();
  await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("search, chat with a streamed reply, reject a pending write, save a note", async ({ page }) => {
  await login(page);
  await page.goto("/notes/");
  await openBySearch(page, "fixture", "Fixture topic");

  await expect(page.getByRole("combobox")).toHaveValue("fake");
  await page.getByRole("textbox", { name: "Message" }).fill("What is this page about?");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("log").getByText("It is about the fixture topic.", { exact: false })).toBeVisible();

  // The agent's kb_write became a pending write; rejecting it writes nothing.
  await expect(page.getByText("Notes/Agent draft.md").first()).toBeVisible();
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByText("Rejected · Notes/Agent draft.md")).toBeVisible();
  expect(existsSync(join(kb, "Notes", "Agent draft.md"))).toBe(false);

  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByText("Saved to Notes/Note on Fixture topic.md.").first()).toBeVisible();
  const note = readFileSync(join(kb, "Notes", "Note on Fixture topic.md"), "utf8");
  expect(note).toContain("What is this page about?\n\n<!-- jesinia:ai agent=fake model=fake-model at=");
  expect(note).toContain("<!-- /jesinia:ai -->");
  expect(note).toMatch(/About: \[\[[0-9a-f-]{36}\|Fixture topic\]\]/);
});

test("editing keeps noncanonical front matter and provenance byte-identical", async ({ page }) => {
  await login(page);
  await page.goto("/notes/");
  await openBySearch(page, "round", "Round Trip");
  const editor = page.getByRole("textbox", { name: "Edit Round Trip" });
  await editor.click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("\nAdded line.");
  await expect(page.getByTestId("save-state")).toHaveText("Saved", { timeout: 10_000 });
  await expect.poll(() => readFileSync(ROUND_TRIP, "utf8")).toContain("Added line.");
  const text = readFileSync(ROUND_TRIP, "utf8");
  expect(text.startsWith(FRONT)).toBe(true);
  expect(text).toContain(PROVENANCE);
});

test("a change on disk during an unsaved edit offers Keep mine", async ({ page }) => {
  await login(page);
  await page.goto("/notes/");
  await openBySearch(page, "round", "Round Trip");
  await page.getByRole("textbox", { name: "Edit Round Trip" }).click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("\nUnsaved local.");
  writeFileSync(ROUND_TRIP, readFileSync(ROUND_TRIP, "utf8").replace("Intro paragraph.", "Intro edited elsewhere."));
  await expect(page.getByText("Changed on disk:")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Keep mine" }).click();
  await expect.poll(() => readFileSync(ROUND_TRIP, "utf8"), { timeout: 10_000 }).toContain("Unsaved local.");
  expect(readFileSync(ROUND_TRIP, "utf8").startsWith(FRONT)).toBe(true);
});

test("a kept page opens in the Read view: rendered headings, its local image, an AI callout", async ({ page }) => {
  await login(page);
  await page.goto("/notes/page/0199e000-0000-7000-8000-000000000002");
  await expect(page.getByRole("heading", { name: "Kept fixture", level: 1 })).toBeVisible({ timeout: 10_000 });
  const read = page.getByRole("article", { name: "Read view" });
  await expect(read.getByRole("heading", { name: "Article", level: 2 })).toBeVisible();
  await expect(read).not.toContainText("##");
  await expect(read).not.toContainText("source/rss");
  const image = read.getByRole("img", { name: "A red square" });
  await expect(image).toHaveAttribute("src", "/api/kb/assets/0199e000-0000-7000-8000-000000000002/red.png");
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(16);
  await expect(read).toContainText("a remote chart (img.example)");
  for (const src of await read.locator("img").evaluateAll((imgs) => imgs.map((i) => i.getAttribute("src")))) {
    expect(src).toMatch(/^\/api\/kb\/assets\//);
  }
  await expect(read.getByRole("note", { name: "Written by an agent" })).toContainText("AI · claude-code · m");

  // ⌘E / Ctrl+E switches to the editor and back.
  await page.keyboard.press("ControlOrMeta+e");
  await expect(page.getByRole("textbox", { name: "Edit Kept fixture" })).toBeVisible();
  await page.getByRole("button", { name: "Read" }).click();
  await read.getByRole("link", { name: "a sibling page" }).click();
  await expect(page.getByRole("heading", { name: "Round Trip", level: 1 })).toBeVisible();
});

test("on an 800 px window the page gets the screen and the chat is a sheet", async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 900 });
  await login(page);
  await page.goto("/notes/page/0199e000-0000-7000-8000-000000000002");
  const read = page.getByRole("article", { name: "Read view" });
  await expect(read.getByRole("heading", { name: "Article", level: 2 })).toBeInViewport({ timeout: 10_000 });
  await expect(page.getByRole("complementary", { name: "Chat" })).toBeHidden();

  const asker = page.getByRole("button", { name: "Open the chat about this page" });
  await asker.click();
  const sheet = page.getByRole("dialog", { name: "Chat about this page" });
  await expect(sheet).toBeVisible();
  const box = await sheet.boundingBox();
  expect(box?.height ?? 0).toBeLessThanOrEqual(900 * 0.7 + 1);
  await page.getByRole("textbox", { name: "Message" }).fill("a draft");
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(asker).toBeFocused();
  await asker.click();
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue("a draft");
});

test("on a 360 px phone the page toolbar wraps and nothing is clipped", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await login(page);
  await page.goto("/notes/page/0199e000-0000-7000-8000-000000000002");
  const title = page.getByRole("heading", { name: "Kept fixture", level: 1 });
  await expect(title).toBeVisible({ timeout: 10_000 });
  const asker = page.getByRole("button", { name: "Open the chat about this page" });
  await expect(asker).toBeInViewport({ ratio: 1 });
  const main = await page.getByRole("region", { name: "Page" }).boundingBox();
  const right = (main?.x ?? 0) + (main?.width ?? 0);
  for (const control of [title, asker, page.getByRole("button", { name: "Read" }), page.getByTestId("save-state")]) {
    const box = await control.boundingBox();
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(right + 0.5);
  }
  await asker.click();
  await expect(page.getByRole("dialog", { name: "Chat about this page" })).toBeVisible();
});
