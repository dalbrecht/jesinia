// SPDX-License-Identifier: AGPL-3.0-or-later
// Rebases and head changes (plan Task 11, spec §4.1 and §7): the follow-up on a queue row, the
// stale review's "Review the new head" (R65), outdated drafts, and a frozen round's 409.
import { fakeClient, type Routes } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { CHECKING_RETRY_MS, NEW_HEAD_NOT_FETCHED } from "../pr/PrView";
import { queue, routes, row } from "./fixtures";
import { API, drafts, NEWER, PATH, reviewed } from "./pr-fixtures";

function openPr(view: PrView, over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

// Built at run time, so this file holds no bidi character itself.
const RLO = String.fromCharCode(0x202e);
const opens = (calls: Request[]) =>
  calls.filter((c) => c.method === "POST" && new URL(c.url).pathname === `${API}/open`);
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review");
});

describe("queue follow-ups", () => {
  it("files a still-approved rebase under Still approved with its note", async () => {
    const env = fakeClient(
      routes(
        queue([
          row(4, {
            kind: "rebase",
            followup_kind: "still_approved",
            followup_note: "Still approved: a rebase with the same changes as 0123456789ab",
          }),
        ]),
      ),
    );
    render(<App client={env.client} />);
    const tab = await screen.findByRole("tab", { name: /Still approved/ });
    await userEvent.click(tab);
    await userEvent.click(screen.getByRole("button", { name: "Show 1 still approved PR" }));
    expect(screen.getByText(/Still approved: a rebase with the same changes/)).toBeInTheDocument();
  });

  it("names the files a changed rebase re-reviews, hidden characters made visible", async () => {
    const env = fakeClient(
      routes(
        queue([
          row(5, {
            kind: "rereview",
            followup_kind: "changed_hunks_only",
            followup_note: "Rebased with changes since your approval: 2 files to review",
            differing_paths: [`src/a${RLO}b.py`, "src/c.py"],
          }),
        ]),
      ),
    );
    render(<App client={env.client} />);
    const card = await screen.findByRole("article", { name: /Acme\/web#5|acme\/web#5/i });
    expect(within(card).getByText("Re-review")).toBeInTheDocument();
    expect(within(card).getByText(/2 files to review/)).toBeInTheDocument();
    expect(within(card).getByText("⟪U+202E⟫")).toBeInTheDocument();
    expect(within(card).getByText("src/c.py")).toBeInTheDocument();
  });
});

describe("a stale review", () => {
  it("offers Review the new head, which fetches the PR, opens it, and says when GitHub has nothing newer", async () => {
    const view = reviewed({ stale: true }, { head_sha: NEWER });
    const { calls } = openPr(view, { [`POST ${API}/refresh`]: () => Response.json(view.pr) });
    await ready();
    await waitFor(() => expect(opens(calls)).toHaveLength(1)); // the tab's own open
    await userEvent.click(screen.getByRole("button", { name: "Review the new head" }));
    await waitFor(() => expect(opens(calls)).toHaveLength(2));
    expect(await screen.findByText(NEW_HEAD_NOT_FETCHED)).toBeInTheDocument();
  });

  it("offers nothing on a review of the current head", async () => {
    openPr(reviewed());
    await ready();
    expect(screen.queryByRole("button", { name: "Review the new head" })).toBeNull();
  });
});

describe("head changes in the drafts", () => {
  it("explains an outdated draft posts in the body", async () => {
    openPr(reviewed(), {
      [`GET ${API}/drafts`]: () =>
        Response.json(drafts({ drafts: drafts().drafts.map((d) => ({ ...d, outdated: true, in_body: true })) })),
    });
    await ready();
    await userEvent.click(
      within(screen.getByRole("navigation", { name: "Review outline" })).getByRole("link", { name: /Your comments/ }),
    );
    expect(screen.getByText("Outdated")).toBeInTheDocument();
    expect(screen.getByText(/lines changed since: it posts in the review body, not inline/)).toBeInTheDocument();
    expect(screen.queryByText(/GitHub can't anchor it inline/)).toBeNull();
  });

  it("treats a frozen round's 409 as a round change: the edit is kept, nothing saved", async () => {
    openPr(reviewed(), {
      [`PUT ${API}/drafts`]: () =>
        Response.json({ error: "RoundFrozen", detail: "round 7 is read-only", successor_round_id: 8 }, { status: 409 }),
    });
    await ready();
    await userEvent.click(
      within(screen.getByRole("navigation", { name: "Review outline" })).getByRole("link", { name: /Your comments/ }),
    );
    const box = screen.getByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
    await userEvent.click(box);
    await userEvent.paste(" More.");
    fireEvent.blur(box);
    expect(await screen.findByText(/A new review round started; your edits are kept here/)).toBeInTheDocument();
    expect(box).toHaveValue("Rounding happens once. More.");
  });
});

describe("a head still being checked (Codex P1 on PR #63)", () => {
  it("says so and opens again by itself until the round starts", async () => {
    let calls = 0;
    const { calls: sent } = openPr(reviewed({ stale: true }, { head_sha: NEWER }), {
      [`POST ${API}/open`]: () => {
        calls += 1;
        return calls === 1
          ? Response.json({
              state: "checking",
              reason: "checking this head against your previous review first (not checked yet); it opens again shortly",
              round_id: null,
              jobs: [],
            })
          : Response.json({ state: "started", reason: "reviewing", round_id: 3, jobs: ["rereview"] }, { status: 202 });
      },
    });
    await ready();
    expect(await screen.findByText(/checking this head against your previous review first/)).toBeInTheDocument();
    await waitFor(() => expect(opens(sent)).toHaveLength(2), { timeout: CHECKING_RETRY_MS + 3000 });
    expect(await screen.findByText(/Reviewing: 1 job running/)).toBeInTheDocument();
  }, 15_000);
});
