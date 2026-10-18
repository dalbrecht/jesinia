// SPDX-License-Identifier: AGPL-3.0-or-later
// The Issues app end to end (Issues spec §6): a new issue arrives in the Inbox, the agent's
// suggestion is approved (it applies at once), a toolbar picker change waits in the Pending
// changes tray until Apply, and each batch shows its result. GitHub is the fake on loopback
// (fixtures/github/server.py), which records every request; the agent is the scripted `fake`
// runner. Nothing here touches the developer's GitHub, `gh` config or credentials.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";
const REPO = "e2e-dev/greeter";

function token(): string {
  const m = /^token = "([^"]+)"/m.exec(readFileSync(join(home, "data", "config.toml"), "utf8"));
  if (!m?.[1]) throw new Error("no token");
  return m[1];
}

const github = () => `http://127.0.0.1:${readFileSync(join(home, "github.port"), "utf8").trim()}`;

type Recorded = { method: string; path: string; query: string; body: unknown };

async function githubRequests(page: Page): Promise<Recorded[]> {
  const response = await page.request.get(`${github()}/__requests`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as Recorded[];
}

let keys = 0;
const keyed = (auth: Record<string, string>) => ({ ...auth, "Idempotency-Key": `e2e-issues-${Date.now()}-${keys++}` });

test("a new issue: approve the agent's suggestion at once, stage a picker change, apply the tray", async ({ page }) => {
  const auth = { Authorization: `Bearer ${token()}` };
  // Watch the fixture repo, then open an issue on "GitHub" after it was added: it is new, so it
  // lands in the Inbox (an issue that was there before goes to the Backlog).
  const watched = await page.request.put(`/api/issues/repos/${REPO}`, {
    headers: keyed(auth),
    data: { watched: true, settings_schema: null },
  });
  expect(watched.ok()).toBe(true);
  expect((await page.request.post(`/api/issues/repos/${REPO}/sync`, { headers: keyed(auth) })).ok()).toBe(true);
  const opened = await page.request.post(`${github()}/__issues`, {
    data: {
      title: "Greeter crashes on an empty name",
      body: "Calling **greet('')** raises.\n\n<script>alert(1)</script>",
      labels: ["bug", "area:cli", "priority: medium"],
    },
  });
  const { number } = (await opened.json()) as { number: number };
  const ref = `${REPO}#${number}`;
  const labelsPath = `/repos/${REPO}/issues/${number}/labels`;
  expect((await page.request.post(`/api/issues/repos/${REPO}/sync`, { headers: keyed(auth) })).ok()).toBe(true);

  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);
  await page.goto("/issues/");
  const inbox = page.getByRole("list", { name: "Inbox" });
  await expect(inbox.getByText("needs readiness")).toBeVisible();
  await inbox.getByRole("button", { name: "Greeter crashes on an empty name", exact: true }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(`/issues/${REPO}/${number}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Greeter crashes on an empty name");
  // The body is Markdown rendered safely: emphasis kept, the script tag never.
  await expect(page.getByRole("region", { name: "Summary" }).locator("strong")).toHaveText("greet('')");
  await expect(page.getByRole("region", { name: "Summary" }).locator("script")).toHaveCount(0);

  // Ask the agent, then accept its suggestion: an approval applies to GitHub at once.
  const suggestions = page.getByRole("region", { name: "Suggestions" });
  await suggestions.getByRole("button", { name: "Suggest with Claude" }).click();
  const ready = suggestions.getByRole("listitem").filter({ hasText: "Set readiness to agent-ready" });
  await expect(ready).toBeVisible({ timeout: 30_000 });
  const before = (await githubRequests(page)).filter((r) => r.path === labelsPath).length;
  await ready.getByRole("button", { name: "Accept" }).click();
  const tray = page.getByRole("region", { name: "Pending changes" });
  await expect(tray.getByText(`Approved: Set readiness to agent-ready on ${ref}`)).toBeVisible();
  await expect(tray.getByText("All changes applied")).toBeVisible({ timeout: 30_000 });
  const added = (await githubRequests(page)).filter((r) => r.path === labelsPath).slice(before);
  expect(added).toEqual([expect.objectContaining({ method: "POST", body: { labels: ["agent-ready"] } })]);
  await expect(page.getByRole("combobox", { name: "Readiness" })).toHaveValue("agent-ready");

  // A picker change is staged, not written: nothing reaches GitHub until Apply.
  await page.getByRole("combobox", { name: "Priority" }).selectOption("priority: high");
  await expect(tray.getByText("Set priority to high")).toBeVisible();
  // Staged only: the tray has read it back, and no label write reached GitHub since.
  const labelWrites = async () =>
    (await githubRequests(page)).filter((r) => r.method !== "GET" && r.path.startsWith(labelsPath)).length;
  expect(await labelWrites()).toBe(1);
  await tray.getByRole("button", { name: /^Apply/ }).click();
  await expect(tray.getByText("All changes applied")).toHaveCount(2, { timeout: 30_000 });
  const results = tray.getByRole("listitem", { name: ref });
  await expect(results.last().getByText("Applied")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Priority" })).toHaveValue("priority: high");

  // One add and one remove against a fresh read, never a replacement of the whole set.
  const writes = (await githubRequests(page)).filter((r) => r.method !== "GET" && r.path.startsWith(labelsPath));
  const described = writes.map(
    (w) => `${w.method} ${decodeURIComponent(w.path.slice(labelsPath.length))} ${JSON.stringify(w.body)}`,
  );
  expect(described.slice(1).sort()).toEqual(["DELETE /priority: medium null", 'POST  {"labels":["priority: high"]}']);
});

test("a queue's order is local: a real mouse drag reorders it at once, and nothing reaches GitHub", async ({
  page,
}) => {
  const auth = { Authorization: `Bearer ${token()}` };
  expect(
    (await page.request.put(`/api/issues/repos/${REPO}`, { headers: keyed(auth), data: { watched: true, settings_schema: null } })).ok(), // prettier-ignore
  ).toBe(true);
  const opened: number[] = [];
  for (const title of ["Queue drag first", "Queue drag second"]) {
    const made = await page.request.post(`${github()}/__issues`, {
      data: { title, body: "", labels: ["bug", "area:cli", "priority: medium"] },
    });
    opened.push(((await made.json()) as { number: number }).number);
  }
  expect((await page.request.post(`/api/issues/repos/${REPO}/sync`, { headers: keyed(auth) })).ok()).toBe(true);
  const name = `Drag ${Date.now()}`;
  const made = await page.request.post("/api/issues/queues", { headers: keyed(auth), data: { name } });
  expect(made.ok()).toBe(true);
  const { id } = (await made.json()) as { id: string };
  for (const n of opened) {
    const added = await page.request.post(`/api/issues/queues/${id}/items`, {
      headers: keyed(auth),
      data: { kind: "issue", repo: REPO, n },
    });
    expect(added.ok()).toBe(true);
  }
  const order = async () => {
    const got = (await (await page.request.get(`/api/issues/queues/${id}`, { headers: auth })).json()) as {
      items: { n: number }[];
    };
    return got.items.map((item) => item.n);
  };
  expect(await order()).toEqual(opened);
  const writesBefore = (await githubRequests(page)).filter((r) => r.method !== "GET").length;

  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);
  await page.goto("/issues/queues");
  await page.getByRole("tab").filter({ hasText: name }).click();
  const list = page.getByRole("list", { name });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  const handle = list.getByRole("button", { name: `Move ${REPO}#${opened[1]} Queue drag second` });
  const top = await list.getByRole("listitem").first().boundingBox();
  const box = await handle.boundingBox();
  if (!top || !box) throw new Error("the queue did not lay out");
  const x = box.x + box.width / 2;
  await page.mouse.move(x, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(x, box.y + box.height / 2 - 6, { steps: 3 });
  await page.mouse.move(x, top.y + 4, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator("[data-dnd-announcer]")).toContainText("to position 1 of 2");
  await expect.poll(order).toEqual([opened[1], opened[0]]);
  await expect(list.getByRole("listitem").first()).toContainText("Queue drag second");
  // Local only: no write reached GitHub, and nothing waits in the tray.
  expect((await githubRequests(page)).filter((r) => r.method !== "GET").length).toBe(writesBefore);
  await expect(page.getByRole("region", { name: "Pending changes" })).toHaveCount(0);
});
