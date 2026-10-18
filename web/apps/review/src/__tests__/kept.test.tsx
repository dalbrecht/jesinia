// SPDX-License-Identifier: AGPL-3.0-or-later
// Kept review pages and How it works (plan Task 16, spec §6.2, §8): a finished deep review says
// where its KB page went (or why not, with Retry), and offers the drafted How it works, written
// only on Accept and never over an edit. KB text is shown with its hidden characters visible.
import { fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { readArtifacts } from "../pr/model";
import { queue, routes } from "./fixtures";
import { API, ARTIFACTS, drafts, PATH, reviewed, ROUND } from "./pr-fixtures";

const RLO = "\u202E";
const PDF = "\u202C";
const KEPT = {
  state: "kept",
  page_id: "0199e000-0000-7000-8000-000000000001",
  path: "Reviews/acme-web/#3 Bill.md",
  error: null,
};
const SEED = {
  kind: "seed",
  text: `How acme/web works.\n\nBilling ${RLO}gnirts${PDF} runs nightly.`,
  base_rev: null,
  page_id: null,
  state: "proposed",
  error: null,
};

function finished(artifacts: object, round: object = {}): PrView {
  return reviewed({ round: { ...ROUND, done: true, ...round }, artifacts: { ...ARTIFACTS, ...artifacts } });
}

/** The PR view served is the first, until a write's handler moves it on to the next. */
function setup(views: [PrView, ...PrView[]], over: (next: () => void) => Routes = () => ({})) {
  window.history.replaceState(null, "", PATH);
  let shown = 0;
  const next = () => {
    shown = Math.min(shown + 1, views.length - 1);
  };
  const env = fakeClient(
    routes(queue([views[0].pr]), {
      [`GET ${API}`]: () => Response.json(views[shown]),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      ...over(next),
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const recommendation = async () => {
  await screen.findByRole("heading", { level: 2, name: "Overview" });
  await userEvent.click(within(outline()).getByRole("link", { name: /Recommendation/ }));
};
const kept = () => screen.findByRole("region", { name: "Kept in your KB" });
const error = (status: number, code: string, detail: string) => Response.json({ error: code, detail }, { status });

beforeEach(() => localStorage.clear());

describe("the kept review page", () => {
  it("links the kept page, its path with hidden characters made visible", async () => {
    const path = `Reviews/acme-web/#3 Bill${RLO}gnirts.md`;
    setup([finished({ kept: { ...KEPT, path } })]);
    await recommendation();
    const link = within(await kept()).getByRole("link");
    expect(link).toHaveAttribute("href", `/notes/page/${KEPT.page_id}`);
    expect(link.textContent).toContain("⟪U+202E⟫");
  });

  it("a failed keep says why; Retry keeps it again under one key", async () => {
    const failed = { state: "failed", page_id: null, path: null, error: `the KB folder ${RLO}is missing` };
    let attempt = 0;
    const { calls } = setup([finished({ kept: failed }), finished({ kept: KEPT })], (next) => ({
      [`POST ${API}/keep`]: () => {
        attempt += 1;
        if (attempt === 1) return error(503, "x", "down");
        next();
        return Response.json(KEPT);
      },
    }));
    await recommendation();
    // KB causes can carry a PR's title: hidden characters are made visible (R62).
    expect(within(await kept()).getByText(/Couldn't keep the review page/).textContent).toBe(
      "Couldn't keep the review page (the KB folder ⟪U+202E⟫is missing).",
    );
    await userEvent.click(within(await kept()).getByRole("button", { name: "Retry" }));
    expect(await within(await kept()).findByRole("alert")).toHaveTextContent("Couldn't keep the review page: down.");
    await userEvent.click(within(await kept()).getByRole("button", { name: "Retry" }));
    expect(await within(await kept()).findByRole("link")).toBeVisible();
    const [first, second] = sent(calls, "POST", `${API}/keep`);
    expect(first?.headers.get("Idempotency-Key")).toBe(second?.headers.get("Idempotency-Key"));
    expect(await second?.json()).toEqual({ round_id: 3 });
  });

  it("a round never kept offers to keep it; a kept-but-failed answer shows on the round", async () => {
    const failed = { state: "failed", page_id: null, path: null, error: "no KB path is configured" };
    setup([finished({}), finished({ kept: failed })], (next) => ({
      [`POST ${API}/keep`]: () => {
        next();
        return Response.json(failed);
      },
    }));
    await recommendation();
    await userEvent.click(within(await kept()).getByRole("button", { name: "Keep it" }));
    expect(await within(await kept()).findByText(/no KB path is configured/)).toBeVisible();
  });

  it("an unfinished review shows nothing about keeping", async () => {
    setup([finished({ kept: KEPT }, { done: false })]);
    await recommendation();
    expect(screen.queryByRole("region", { name: "Kept in your KB" })).toBeNull();
  });
});

describe("How it works", () => {
  it("shows the draft with hidden characters visible and writes it only on Accept", async () => {
    const accepted = { ...SEED, state: "accepted" };
    const views: [PrView, PrView] = [
      finished({ kept: KEPT, system_draft: SEED }),
      finished({ kept: KEPT, system_draft: accepted }),
    ];
    const { calls } = setup(views, (next) => ({
      [`POST ${API}/system/accept`]: () => {
        next();
        return Response.json({ page_id: "p", path: "Reviews/acme-web/How it works.md", rev: "r" });
      },
    }));
    await recommendation();
    const draft = await screen.findByRole("region", { name: "Start How it works" });
    expect(within(draft).getByLabelText("Drafted How it works").textContent).toContain("⟪U+202E⟫");
    expect(sent(calls, "POST", `${API}/system/accept`)).toHaveLength(0);
    await userEvent.click(within(draft).getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("How it works is updated with this review.")).toBeVisible();
    const [accept] = sent(calls, "POST", `${API}/system/accept`);
    expect(await accept?.json()).toEqual({ round_id: 3, base_rev: null });
  });

  it("an edit since the draft is refused, the fresh draft is shown and can be accepted", async () => {
    const stale = { ...SEED, kind: "update", base_rev: "a".repeat(64), page_id: "p", text: "Old draft." };
    const fresh = { ...stale, base_rev: "b".repeat(64), text: "Fresh draft." };
    let attempt = 0;
    const views: [PrView, PrView] = [
      finished({ kept: KEPT, system_draft: stale }),
      finished({ kept: KEPT, system_draft: fresh }),
    ];
    const { calls } = setup(views, (next) => ({
      [`POST ${API}/system/accept`]: () => {
        attempt += 1;
        if (attempt > 1) return Response.json({ page_id: "p", path: "x", rev: "c" });
        next(); // the server drafted again against the edited page
        return error(409, "SystemPageChanged", "changed");
      },
    }));
    await recommendation();
    const draft = await screen.findByRole("region", { name: "Update How it works" });
    await userEvent.click(within(draft).getByRole("button", { name: "Accept" }));
    expect(await within(await kept()).findByRole("alert")).toHaveTextContent(
      /changed since that draft was made, so nothing was written and the edit is kept\. Jesinia drafted it again/,
    );
    expect(await within(draft).findByText("Fresh draft.")).toBeVisible();
    await userEvent.click(within(draft).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/system/accept`)).toHaveLength(2));
    const [, second] = sent(calls, "POST", `${API}/system/accept`);
    expect(await second?.json()).toEqual({ round_id: 3, base_rev: "b".repeat(64) });
  });

  it("an accept whose answer was lost never replays onto a newer round's draft (Codex P2)", async () => {
    const later = { ...SEED, text: "A newer round's seed." };
    let attempt = 0;
    const views: [PrView, PrView] = [
      finished({ kept: KEPT, system_draft: SEED }),
      finished({ kept: KEPT, system_draft: later }, { id: 4 }),
    ];
    const { calls } = setup(views, (next) => ({
      [`POST ${API}/system/accept`]: () => {
        attempt += 1;
        if (attempt === 1) {
          next(); // a new round started meanwhile; the answer to this accept is lost
          return error(502, "x", "the answer was lost");
        }
        return Response.json({ page_id: "p", path: "x", rev: "c" });
      },
    }));
    await recommendation();
    const draft = await screen.findByRole("region", { name: "Start How it works" });
    await userEvent.click(within(draft).getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("A newer round's seed.")).toBeVisible();
    await userEvent.click(
      within(await screen.findByRole("region", { name: "Start How it works" })).getByRole("button", {
        name: "Accept",
      }),
    );
    await waitFor(() => expect(sent(calls, "POST", `${API}/system/accept`)).toHaveLength(2));
    const [first, second] = sent(calls, "POST", `${API}/system/accept`);
    expect(second?.headers.get("Idempotency-Key")).not.toBe(first?.headers.get("Idempotency-Key"));
    expect(await second?.json()).toEqual({ round_id: 4, base_rev: null });
  });

  it("Keep it after a lost answer and a new round sends a new key", async () => {
    let attempt = 0;
    const views: [PrView, PrView] = [finished({}), finished({}, { id: 4 })];
    const { calls } = setup(views, (next) => ({
      [`POST ${API}/keep`]: () => {
        attempt += 1;
        if (attempt === 1) {
          next();
          return error(502, "x", "the answer was lost");
        }
        return Response.json(KEPT);
      },
    }));
    await recommendation();
    await userEvent.click(within(await kept()).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(sent(calls, "GET", API).length).toBeGreaterThan(1));
    await userEvent.click(within(await kept()).getByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/keep`)).toHaveLength(2));
    const [first, second] = sent(calls, "POST", `${API}/keep`);
    expect(second?.headers.get("Idempotency-Key")).not.toBe(first?.headers.get("Idempotency-Key"));
    expect(await second?.json()).toEqual({ round_id: 4 });
  });

  it("Not now hides the draft", async () => {
    const update = { ...SEED, kind: "update", base_rev: "a".repeat(64), page_id: "p" };
    setup([finished({ kept: KEPT, system_draft: update })]);
    await recommendation();
    const draft = await screen.findByRole("region", { name: "Update How it works" });
    await userEvent.click(within(draft).getByRole("button", { name: "Not now" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Update How it works" })).toBeNull());
  });

  it("a withdrawn draft offers nothing to accept", async () => {
    setup([finished({ kept: KEPT, system_draft: { ...SEED, state: "withdrawn", text: "" } })]);
    await recommendation();
    expect(await within(await kept()).findByText(/there is nothing new to add/)).toBeVisible();
    expect(within(await kept()).queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it("a rebase round offers no keeping", async () => {
    setup([finished({ followup: { kind: "still_approved" } })]);
    await recommendation();
    expect(screen.queryByRole("region", { name: "Kept in your KB" })).toBeNull();
  });

  it("another failure to accept says why", async () => {
    setup([finished({ kept: KEPT, system_draft: SEED })], () => ({
      [`POST ${API}/system/accept`]: () => error(503, "KbUnavailable", "no KB path is configured"),
    }));
    await recommendation();
    await userEvent.click(
      within(await screen.findByRole("region", { name: "Start How it works" })).getByRole("button", { name: "Accept" }),
    );
    expect(await within(await kept()).findByRole("alert")).toHaveTextContent(
      "Couldn't update How it works: no KB path is configured.",
    );
  });
});

describe("reading the artifacts", () => {
  it("reads only well-formed records", () => {
    const view = (artifacts: object) => readArtifacts(finished(artifacts));
    expect(view({}).kept).toBeNull();
    expect(view({ kept: { state: "odd" } }).kept).toBeNull();
    expect(view({ kept: { state: "kept", page_id: 1 } }).kept).toEqual({
      state: "kept",
      page_id: null,
      path: null,
      error: null,
    });
    expect(view({ system_draft: { kind: "other", state: "proposed" } }).systemDraft).toBeNull();
    expect(view({ system_draft: { kind: "seed", state: "odd" } }).systemDraft).toBeNull();
    expect(view({ system_draft: { kind: "seed", state: "failed", error: "boom" } }).systemDraft).toEqual({
      kind: "seed",
      text: "",
      base_rev: null,
      state: "failed",
      error: "boom",
    });
  });

  it("a failed draft says why", async () => {
    setup([finished({ kept: KEPT, system_draft: { ...SEED, state: "failed", text: "", error: "boom" } })]);
    await recommendation();
    expect(within(await kept()).getByText("Couldn't draft How it works (boom).")).toBeVisible();
  });
});
