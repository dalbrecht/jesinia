// SPDX-License-Identifier: AGPL-3.0-or-later
// The Issues app end to end (Issues spec §6): a new issue arrives in the Inbox, the agent's
// suggestion is approved (it applies at once), a toolbar picker change waits in the Pending
// changes tray until Apply, and each batch shows its result. GitHub is the fake on loopback
// (fixtures/github/server.py), which records every request; the agent is the scripted `fake`
// runner. Nothing here touches the developer's GitHub, `gh` config or credentials.
import { expect, test, type Locator, type Page } from "@playwright/test";
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

/** Pick up `handle` by keyboard and step with `key` until the live region says `over`, then drop. */
async function keyboardDrag(page: Page, handle: Locator, key: "ArrowUp" | "ArrowDown", over: string) {
  const announcer = page.locator("[data-dnd-announcer]");
  await handle.focus();
  await page.keyboard.press("Space");
  await expect(announcer).toContainText("Picked up");
  for (let step = 0; step < 40; step++) {
    const said = (await announcer.textContent()) ?? "";
    if (said.includes(over)) break;
    await page.keyboard.press(key);
    // Wait for the region to say where the drag is now, never a fixed sleep.
    await expect(announcer).not.toHaveText(said);
  }
  await expect(announcer).toContainText(over);
  await page.keyboard.press("Space");
}

/** A real mouse drag from `handle` onto the middle of `target` (dnd-kit needs a few steps). */
async function mouseDrag(page: Page, handle: Locator, target: Locator) {
  const from = await handle.boundingBox();
  const to = await target.boundingBox();
  if (!from || !to) throw new Error("the drag did not lay out");
  const x = from.x + from.width / 2;
  const y = from.y + from.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 4, y + 4, { steps: 3 });
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 });
  await page.mouse.up();
}

test("Settings → Issues sets the labels up; drags stage two parent links, a band move and a Done; one Apply writes them", async ({
  page,
}) => {
  const auth = { Authorization: `Bearer ${token()}` };
  // A partial save, as Settings sends: only `watched`.
  const watched = await page.request.patch(`/api/issues/repos/${REPO}`, {
    headers: keyed(auth),
    data: { watched: true },
  });
  expect(watched.ok()).toBe(true);
  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);

  // Settings → Issues: preview the label setup (the fixture repo lacks `epic` and `initiative`),
  // then apply it as an approval; the labels land on "GitHub".
  await page.goto("/settings/#issues");
  const repo = page.getByRole("group", { name: REPO });
  await expect(repo.getByText("The default label convention")).toBeVisible();
  await repo.getByRole("button", { name: `Preview setup for ${REPO}` }).click();
  const setup = repo.getByRole("region", { name: `Setup for ${REPO}` });
  await expect(setup.getByRole("list")).toContainText("Create epic");
  await setup.getByRole("button", { name: "Apply setup" }).click();
  await expect(setup.getByText("Setup applied: every step done.")).toBeVisible({ timeout: 30_000 });
  const created = (await githubRequests(page))
    .filter((r) => r.method === "POST" && r.path === `/repos/${REPO}/labels`)
    .map((r) => (r.body as { name: string }).name);
  expect(created).toEqual(expect.arrayContaining(["epic", "initiative"]));

  // Fixtures on "GitHub": an epic, two issues for it, one for a theme, and three queue items.
  const open = async (title: string, labels: string[]) => {
    const made = await page.request.post(`${github()}/__issues`, { data: { title, body: "", labels } });
    return ((await made.json()) as { number: number }).number;
  };
  const ready = ["bug", "area:cli", "priority: medium", "agent-ready"];
  const epic = await open("Greeter epic", ["epic", "enhancement", "area:cli", "priority: medium", "agent-ready"]);
  const first = await open("Epic child one", ready);
  const second = await open("Epic child two", ready);
  const themed = await open("Theme me", ready);
  const high = await open("Queue high", ["bug", "area:cli", "priority: high", "agent-ready"]);
  const moved = await open("Queue moved", ready);
  const human = await open("Queue human task", ["bug", "area:cli", "priority: medium", "human-needed"]);
  expect((await page.request.post(`/api/issues/repos/${REPO}/sync`, { headers: keyed(auth) })).ok()).toBe(true);
  const ref = (n: number) => `${REPO}#${n}`;
  const tag = "greeter-polish";
  // Themes are tags from the KB's catalog: the theme needs the fixture KB.
  const kb = await page.request.put("/api/kb/path", { headers: auth, data: { path: join(home, "kb") } });
  expect(kb.ok()).toBe(true);
  const themedFirst = await page.request.post(`/api/issues/themes/${tag}/issues`, {
    headers: keyed(auth),
    data: { issues: [{ repo: REPO, n: high }] },
  });
  expect(themedFirst.ok(), await themedFirst.text()).toBe(true);
  const name = `Agents ${Date.now()}`;
  const queue = (await (
    await page.request.post("/api/issues/queues", { headers: keyed(auth), data: { name } })
  ).json()) as {
    id: string;
  };
  for (const n of [high, moved, human]) {
    const added = await page.request.post(`/api/issues/queues/${queue.id}/items`, {
      headers: keyed(auth),
      data: { kind: "issue", repo: REPO, n },
    });
    expect(added.ok()).toBe(true);
  }
  const writesBefore = (await githubRequests(page)).length;
  const applies: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/api/issues/pending/apply") applies.push(r.url());
  });
  const staged = async () =>
    (
      (await (await page.request.get("/api/issues/pending", { headers: auth })).json()) as {
        changes: { n: number | null; change: { kind: string } }[];
      }
    ).changes.map((c) => `${c.change.kind} ${c.n}`);

  // Two issues dragged together onto the epic with the real mouse: each stages its parent link.
  await page.goto("/issues/epics");
  const pool = page.getByRole("list", { name: "Open issues without an epic" });
  await pool.getByRole("checkbox", { name: `Select ${ref(first)}` }).check();
  await pool.getByRole("checkbox", { name: `Select ${ref(second)}` }).check();
  await mouseDrag(
    page,
    pool.getByRole("button", { name: `Move ${ref(first)} Epic child one` }),
    page.getByRole("group", { name: `${ref(epic)} Greeter epic` }),
  );
  await expect.poll(staged).toEqual(expect.arrayContaining([`set_parent ${first}`, `set_parent ${second}`]));

  // One issue onto a theme, by keyboard: local, nothing staged for GitHub.
  await page.goto("/issues/backlog");
  const backlog = page.getByRole("list", { name: "Backlog" });
  await keyboardDrag(
    page,
    backlog.getByRole("button", { name: `Move ${ref(themed)} Theme me` }),
    "ArrowDown",
    `Over #${tag}.`,
  );
  await expect
    .poll(async () => {
      const got = (await (await page.request.get(`/api/issues/themes/${tag}`, { headers: auth })).json()) as {
        issues: { n: number }[];
      };
      return got.issues.map((i) => i.n).sort((a, b) => a - b);
    })
    .toEqual([themed, high].sort((a, b) => a - b));

  // The queue by priority: reorder the medium band (local), move an item up a band (staged),
  // and mark the human task Done (its close is staged).
  await page.goto("/issues/queues");
  await page.getByRole("tab").filter({ hasText: name }).click();
  await page.getByRole("radio", { name: "By priority" }).check();
  const order = async () =>
    (
      (await (await page.request.get(`/api/issues/queues/${queue.id}`, { headers: auth })).json()) as {
        items: { n: number }[];
      }
    ).items.map((i) => i.n);
  await keyboardDrag(
    page,
    page.getByRole("button", { name: `Move ${ref(human)} Queue human task` }),
    "ArrowUp",
    "Over Medium priority, position 1 of 2.",
  );
  await expect.poll(order).toEqual([high, human, moved]);
  await keyboardDrag(
    page,
    page.getByRole("button", { name: `Move ${ref(moved)} Queue moved` }),
    "ArrowUp",
    "Over High priority, position 2 of 2.",
  );
  await expect.poll(staged).toContain(`set_dimension ${moved}`);
  // The box never shows checked: Done moves the item to the Done group.
  await page.getByRole("checkbox", { name: `Done: ${ref(human)} Queue human task` }).click();
  await expect.poll(staged).toContain(`close ${human}`);
  // Staged, not written: nothing reached GitHub yet.
  expect((await githubRequests(page)).slice(writesBefore).filter((r) => r.method !== "GET")).toEqual([]);

  // One Apply: one batch writes the parent links, the band's priority and the close.
  const tray = page.getByRole("region", { name: "Pending changes" });
  await tray.getByRole("button", { name: /^Apply/ }).click();
  await expect(tray.getByText("All changes applied").last()).toBeVisible({ timeout: 30_000 });
  expect(applies).toHaveLength(1);
  const writes = (await githubRequests(page)).slice(writesBefore).filter((r) => r.method !== "GET");
  const subIssues = writes.filter((w) => w.method === "POST" && w.path === `/repos/${REPO}/issues/${epic}/sub_issues`);
  expect(subIssues.map((w) => (w.body as { sub_issue_id: number }).sub_issue_id).sort()).toEqual(
    [7000 + first, 7000 + second].sort(),
  );
  const movedLabels = `/repos/${REPO}/issues/${moved}/labels`;
  expect(
    writes
      .filter((w) => w.path.startsWith(movedLabels))
      .map((w) => `${w.method} ${decodeURIComponent(w.path)}`)
      .sort(),
  ).toEqual([`DELETE ${movedLabels}/priority: medium`, `POST ${movedLabels}`].sort());
  expect(writes.filter((w) => w.path === `/repos/${REPO}/issues/${human}`)).toEqual([
    expect.objectContaining({ method: "PATCH", body: expect.objectContaining({ state: "closed" }) }),
  ]);
  // The theme stayed local, and nothing else was written.
  expect(writes).toHaveLength(subIssues.length + 3);
  const closed = await page.request.get(`${github()}/repos/${REPO}/issues/${human}`, {
    headers: { Authorization: "Bearer e2e" },
  });
  expect(((await closed.json()) as { state: string }).state).toBe("closed");
});
