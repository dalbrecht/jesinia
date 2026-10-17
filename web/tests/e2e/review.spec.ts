// SPDX-License-Identifier: AGPL-3.0-or-later
// The Review app end to end (spec §2): the e2e server has no GitHub token (fake_runner_app),
// so the queue shows its auth-missing state with the exact command, never an empty page.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const home = process.env.E2E_HOME ?? "";

function token(): string {
  const m = /^token = "([^"]+)"/m.exec(readFileSync(join(home, "data", "config.toml"), "utf8"));
  if (!m?.[1]) throw new Error("no token");
  return m[1];
}

async function login(page: Page) {
  const auth = { Authorization: `Bearer ${token()}` };
  const { url } = (await (await page.request.post("/api/auth/nonce", { headers: auth })).json()) as { url: string };
  await page.goto(url);
}

test("the queue opens with GitHub signed out: the banner names the command; a linked PR gets its own tab", async ({
  page,
}) => {
  await login(page);
  await page.goto("/review/");
  const banner = page.locator("[data-tone=danger]").filter({ hasText: "GitHub auth missing:" });
  await expect(banner).toBeVisible();
  await expect(banner.locator("code", { hasText: "gh auth login" })).toBeVisible();
  await expect(page.getByText(/Nothing is cached yet/)).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Load a PR" })).toBeVisible();
  const tabs = page.getByRole("navigation", { name: "Open reviews" });
  await expect(tabs.getByRole("link", { name: /Queue/ })).toHaveAttribute("aria-current", "page");

  // A review link opens its tab even when the PR can't be loaded; the cause is shown.
  await page.goto("/review/acme/web/7");
  await expect(tabs.getByRole("link", { name: /acme\/web#7/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText(/Couldn't load acme\/web#7/).first()).toBeVisible();
  await tabs.getByRole("button", { name: "Close acme/web#7" }).click();
  await expect(page).toHaveURL(/\/review\/$/);
});
