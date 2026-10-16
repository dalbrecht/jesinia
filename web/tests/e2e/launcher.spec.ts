// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";

function token(): string {
  const config = readFileSync(join(home, "data", "config.toml"), "utf8");
  const m = /^token = "([^"]+)"/m.exec(config);
  if (!m?.[1]) throw new Error("no token in the e2e config");
  return m[1];
}

test("log in, set the KB path, see push mode, and sync", async ({ page, request }) => {
  const nonce = await request.post("/api/auth/nonce", { headers: { Authorization: `Bearer ${token()}` } });
  expect(nonce.ok()).toBe(true);
  const { url } = (await nonce.json()) as { url: string };
  await page.goto(url);
  await expect(page.getByRole("heading", { name: /Good (morning|afternoon|evening)/ })).toBeVisible();

  await page.goto("/settings/");
  const field = page.getByLabel("KB folder");
  await field.fill(join(home, "kb"));
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/KB folder saved/).first()).toBeVisible();

  await expect(page.getByTestId("sync-mode")).toHaveText("Commit and push");
  await page.getByRole("button", { name: "Sync now" }).click();
  await expect(page.getByTestId("sync-last")).toContainText(/pushed|nothing to sync/, { timeout: 60_000 });
});

test("the API refuses a browser without a session", async ({ request }) => {
  expect((await request.get("/api/kb/conflicts")).status()).toBe(401);
});
