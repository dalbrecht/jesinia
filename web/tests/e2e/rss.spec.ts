// SPDX-License-Identifier: AGPL-3.0-or-later
// The RSS reader end to end (spec §8): the fixture feeds are served on a second loopback port
// by `make e2e-server` (E2E_FEEDS_PORT), which only the throwaway e2e server may fetch.
import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";
const kb = join(home, "kb");
const FEEDS = `http://127.0.0.1:${process.env.E2E_FEEDS_PORT ?? ""}`;
const KEPT = join(kb, "Sources", "Fixture post two.md");
const OPML = join(import.meta.dirname, "fixtures", "feeds", "reading.opml");

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

/** The RSS app once its feeds have loaded (#32: never act on a half-loaded page). */
async function openRss(page: Page) {
  await page.goto("/rss/");
  await expect(
    page.getByRole("navigation", { name: "Feeds" }).getByRole("button", { name: /All unread/ }),
  ).toBeVisible();
  await expect(page.getByText("Loading…")).toHaveCount(0);
}

test.describe.configure({ mode: "serial" });

test("add the fixture feed, skim with j, keep a post with a note and a suggested tag, find it in Notes", async ({
  page,
}) => {
  await login(page);
  await openRss(page);

  await page.getByRole("button", { name: "+ Add feed" }).click();
  const manage = page.getByRole("dialog", { name: "Feeds" });
  await manage.getByLabel("Feed or site URL").fill(`${FEEDS}/fixture.xml`);
  await manage.getByRole("button", { name: "Add feed" }).click();
  await expect(manage).toBeHidden();
  await expect(page.getByRole("heading", { name: "Fixture Feed", level: 2 })).toBeVisible();
  const posts = page.getByRole("listbox", { name: "Posts" }).getByRole("option");
  await expect(posts).toHaveCount(3);

  await page.keyboard.press("j");
  await expect(page.getByRole("heading", { name: "Fixture post three", level: 1 })).toBeVisible();
  await page.keyboard.press("j");
  await expect(page.getByRole("heading", { name: "Fixture post two", level: 1 })).toBeVisible();
  await page.keyboard.press("n");

  const keep = page.getByRole("dialog", { name: "Keep this post" });
  await keep.getByRole("textbox", { name: "Note" }).fill("A note kept from the fixture feed.");
  await keep
    .getByRole("group", { name: "Suggested tags" })
    .getByRole("button", { name: /^#fixture\b(?!-)/ })
    .click();
  await expect(keep.getByRole("list", { name: "Tags" })).toContainText("#fixture");
  await keep.getByRole("textbox", { name: "Note" }).press("ControlOrMeta+Enter");
  await expect(keep).toBeHidden();
  // The archive runs in the background; wait for it, so nothing later races its page write.
  await expect(page.getByRole("region", { name: "Reader" }).getByRole("status")).toHaveText(/In KB/, {
    timeout: 30_000,
  });
  const kept = readFileSync(KEPT, "utf8");
  expect(kept).toContain("A note kept from the fixture feed.");
  expect(kept).toContain("#fixture");

  await page.goto("/notes/");
  await page.getByRole("searchbox", { name: "Search notes" }).fill("fixture");
  await page.getByTestId("search-results").getByRole("button", { name: "Fixture post two" }).first().click();
  await expect(page.getByRole("heading", { name: "Fixture post two", level: 1 })).toBeVisible();
});

test("rename #fixture to #fixtures in Settings → Tags: the kept page follows", async ({ page }) => {
  await login(page);
  await page.goto("/settings/#tags");
  const tags = page.getByRole("region", { name: "Tags" });
  const row = tags.getByRole("listitem").filter({ has: page.getByText("#fixture", { exact: true }) });
  await row.getByRole("button", { name: "Rename" }).click();
  const rename = page.getByRole("dialog", { name: "Rename #fixture" });
  await rename.getByLabel("New name").fill("fixtures");
  await rename.getByRole("button", { name: "Preview" }).click();
  await expect(rename.getByText("Fixture post two")).toBeVisible();
  await expect(rename.locator("h4:has-text('Pages to update') + ul > li")).toHaveCount(1);
  await rename.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("Renamed #fixture to #fixtures in 1 page.").first()).toBeVisible();
  await expect.poll(() => readFileSync(KEPT, "utf8")).toContain("#fixtures");
});

test("import an OPML file in Settings → RSS; the reader shows its folder with counts", async ({ page }) => {
  await login(page);
  await page.goto("/settings/#rss");
  const rss = page.getByRole("region", { name: "RSS" });
  await expect(rss.getByRole("combobox", { name: "Keep unread posts for" })).toBeVisible();
  await expect(rss.getByText("1 feed in 0 folders")).toBeVisible();
  await rss.getByLabel("Import OPML").setInputFiles(OPML);
  await expect(rss.getByRole("status")).toHaveText("Added 2 feeds.");
  await expect(rss.getByText("3 feeds in 1 folder")).toBeVisible();

  await openRss(page);
  await page.getByRole("button", { name: "↻ Refresh" }).click();
  const feeds = page.getByRole("navigation", { name: "Feeds" });
  await expect(feeds.getByRole("button", { name: "Reading a 2" })).toBeVisible({ timeout: 30_000 });
  await expect(feeds.getByRole("button", { name: "Reading b 2" })).toBeVisible();
  // Collapsed, the folder carries its feeds' count.
  await feeds.getByRole("button", { name: "Feeds in Reading list" }).click();
  await expect(feeds.getByRole("button", { name: "Reading list 4" })).toBeVisible();
  expect(existsSync(join(kb, "Sources", "Reading a, first post.md"))).toBe(false); // nothing kept by import
});
