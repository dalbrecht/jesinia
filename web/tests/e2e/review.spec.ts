// SPDX-License-Identifier: AGPL-3.0-or-later
// The Review app end to end (spec §2, §3, §7). The e2e server talks to a fake GitHub on
// loopback (fixtures/github/server.py) that records every request, and its agent is the scripted
// `fake` runner, whose review outputs are valid against the real job schemas. Nothing here
// touches the developer's GitHub, `gh` config or credentials.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";
const PR = "e2e-dev/greeter#1";
const REVIEWS = "/repos/e2e-dev/greeter/pulls/1/reviews";

function token(): string {
  const m = /^token = "([^"]+)"/m.exec(readFileSync(join(home, "data", "config.toml"), "utf8"));
  if (!m?.[1]) throw new Error("no token");
  return m[1];
}

type Recorded = { method: string; path: string; query: string; body: unknown };

/** What has reached the fake GitHub so far. */
async function githubRequests(page: Page): Promise<Recorded[]> {
  const port = readFileSync(join(home, "github.port"), "utf8").trim();
  const response = await page.request.get(`http://127.0.0.1:${port}/__requests`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as Recorded[];
}

async function login(page: Page) {
  const auth = { Authorization: `Bearer ${token()}` };
  // The review agent is the scripted fake runner; pre-reviews wait for the author's own load.
  const saved = await page.request.put("/api/review/settings", {
    headers: { ...auth, "Idempotency-Key": "e2e-review-settings" },
    data: { prereview: "on_open", runner: "fake", repos: [] },
  });
  expect(saved.ok()).toBe(true);
  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);
}

test("the queue shows what waits on CI and on pushes to settle, and a PR that can't load gets its own tab", async ({
  page,
}) => {
  await login(page);
  await page.goto("/review/");
  // Signed in through the fake GitHub: no auth banner, and both open PRs are listed.
  await expect(page.locator("[data-tone=danger]").filter({ hasText: "GitHub auth missing:" })).toHaveCount(0);
  const waiting = page.getByRole("article").filter({ hasText: "e2e-dev/greeter#2" });
  await expect(waiting.getByText("Waiting for CI")).toBeVisible();
  await expect(waiting.getByText("CI running")).toBeVisible();
  // CI is green on #1, but a new head waits for pushes to settle before a background run.
  const settling = page.getByRole("article").filter({ hasText: "e2e-dev/greeter#1" });
  await expect(settling.getByText("CI passing")).toBeVisible();
  await expect(settling.getByText("Waiting for pushes to settle")).toBeVisible();

  const tabs = page.getByRole("navigation", { name: "Open reviews" });
  await expect(tabs.getByRole("link", { name: /Queue/ })).toHaveAttribute("aria-current", "page");

  // A review link opens its tab even when the PR can't be loaded; the cause is shown.
  await page.goto("/review/e2e-dev/greeter/77");
  await expect(tabs.getByRole("link", { name: /e2e-dev\/greeter#77/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText(/Couldn't load e2e-dev\/greeter#77/).first()).toBeVisible();
  await tabs.getByRole("button", { name: "Close e2e-dev/greeter#77" }).click();
  await expect(page).toHaveURL(/\/review\/$/);
});

test("a deep review shows its coverage; approving with a BLOCKING comment warns, then posts exactly one review", async ({
  page,
}) => {
  await login(page);
  await page.goto("/review/");
  await page.getByRole("textbox", { name: "Load a PR" }).fill(PR);
  await page.getByRole("button", { name: "Load", exact: true }).click();
  await expect(page).toHaveURL(/\/review\/e2e-dev\/greeter\/1$/);

  // Switch to a deep review: the walkthrough, background, metrics and quiz are generated, and
  // Jesinia computes the coverage itself from the diff.
  await page.getByRole("button", { name: "Switch to deep" }).click();
  const tools = page.getByRole("group", { name: "Review tools" });
  await expect(tools.getByText("Deep", { exact: true })).toBeVisible();
  await expect(tools.getByText(/Walkthrough covers 100% · 2 of 2 files/)).toBeVisible({ timeout: 60_000 });
  const outline = page.getByRole("navigation", { name: "Review outline" });
  for (const part of ["Background", "Metrics", "Quiz"]) {
    await expect(outline.getByRole("link", { name: part })).toBeVisible();
  }

  // The finding became a draft comment; the author edits it and marks it BLOCKING.
  await outline.getByRole("link", { name: /Your comments/ }).click();
  const comment = page.getByRole("textbox", { name: "Comment on src/greeter.py:5–6", exact: true });
  await expect(comment).toHaveValue(/fallback greeting is a literal/);
  await comment.fill("BLOCKING: return a named constant so the fallback is searchable.");
  await expect(page.getByText("Drafts saved.")).toBeVisible();

  // Approve: Jesinia warns about the BLOCKING text (and that its verdict isn't Approve);
  // Approve stays locked until each warning is confirmed.
  await page.getByRole("button", { name: "Approve…" }).click();
  const dialog = page.getByRole("dialog", { name: /Approve e2e-dev\/greeter#1 on GitHub/ });
  const blocking = dialog.getByRole("group", { name: /You're approving, but the review says BLOCKING/ });
  await expect(blocking).toBeVisible();
  const approve = dialog.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeDisabled();
  await expect(dialog.getByRole("group", { name: /Jesinia's verdict isn't Approve/ })).toBeVisible();
  for (const box of await dialog.getByRole("checkbox", { name: "Approve anyway" }).all()) await box.check();
  // Counted from here, so another spec's (or a retried run's) reviews can't skew it (final M5).
  const reviewPosts = async () => (await githubRequests(page)).filter((r) => r.method === "POST" && r.path === REVIEWS);
  const before = (await reviewPosts()).length;
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(
    page.getByText(/Posted to GitHub: https:\/\/github\.com\/e2e-dev\/greeter\/pull\/1#pullrequestreview-/).first(),
  ).toBeVisible();

  // Exactly one review reached GitHub, atomically: the event, the head it covers, the
  // inline BLOCKING comment and the exactly-once marker.
  const posts = await reviewPosts();
  expect(posts).toHaveLength(before + 1);
  const review = posts[posts.length - 1]?.body as {
    event: string;
    commit_id: string;
    body: string;
    comments: { path: string; line: number; body: string }[];
  };
  expect(review.event).toBe("APPROVE");
  expect(review.commit_id).toMatch(/^[0-9a-f]{40}$/);
  expect(review.comments).toHaveLength(1);
  expect(review.comments[0]).toMatchObject({ path: "src/greeter.py", line: 6 });
  expect(review.comments[0]?.body).toContain("BLOCKING");
  expect(JSON.stringify(review)).toContain("jesinia-posting");

  // Back in the queue the reviewed PR shows its finished pre-review and coverage.
  await page.getByRole("link", { name: /Queue/ }).click();
  await page.getByRole("tab", { name: /Loaded/ }).click();
  const row = page.getByRole("article").filter({ hasText: "e2e-dev/greeter#1" });
  await expect(row.getByText("Pre-review ready")).toBeVisible();
  await expect(row.getByText(/100%/)).toBeVisible();
});
