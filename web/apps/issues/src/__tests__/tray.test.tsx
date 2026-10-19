// SPDX-License-Identifier: AGPL-3.0-or-later
// The Pending changes tray (spec §6.1, §6.2; rulings IR45, IR46, IR58): every GitHub-changing
// action other than an approval stages here; Apply sends one batch, polled to its per-issue
// results, with Retry and Abandon.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApplyBatch, Pending } from "../api";
import { BATCH_POLL_MS } from "../Tray";
import {
  batch,
  callsTo,
  DONE_STEP,
  finished,
  issuesRoutes,
  json,
  pending,
  REPO,
  renderApp,
  tick,
  toast,
} from "./fixtures";

const tray = () => screen.getByRole("region", { name: "Pending changes" });
const ISSUE = "/issues/acme/app/1";

afterEach(() => {
  vi.useRealTimers();
});

/** A tray backed by a mutable list, so staging and undoing show up on the next read. */
function trayRoutes(start: Pending[] = []) {
  let changes = [...start];
  return {
    get changes() {
      return changes;
    },
    routes: {
      "GET /api/issues/pending": () => json({ changes }),
      "POST /api/issues/pending": async (req: Request) => {
        const body = (await req.json()) as Pick<Pending, "repo" | "n" | "change" | "source">;
        const staged = pending(`p${changes.length + 1}`, body);
        changes = [...changes, staged];
        return json(staged, 201);
      },
      "DELETE /api/issues/pending/p1": () => {
        changes = changes.filter((c) => c.id !== "p1");
        return json(null, 204);
      },
      "DELETE /api/issues/pending": () => {
        changes = [];
        return json(null, 204);
      },
    },
    clear() {
      changes = [];
    },
  };
}

describe("staging", () => {
  it("a toolbar picker change stages in the tray with an Idempotency-Key, and writes nothing", async () => {
    const t = trayRoutes();
    const { calls } = renderApp(issuesRoutes(t.routes), ISSUE);
    expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Priority" }), "priority: high");
    await waitFor(() => expect(within(tray()).getByText("Set priority to high")).toBeInTheDocument());
    const [post] = callsTo(calls, "POST", "/api/issues/pending");
    expect(post?.headers.get("Idempotency-Key")).toMatch(/[0-9a-f-]{36}/);
    expect(await post?.clone().json()).toEqual({
      repo: REPO,
      n: 1,
      change: { kind: "set_dimension", dimension: "priority", value: "priority: high" },
      source: "picker",
    });
    expect(within(tray()).getByText("acme/app#1")).toBeInTheDocument();
    expect(within(tray()).getByText("picker")).toBeInTheDocument();
    expect(callsTo(calls, "POST", "/api/issues/pending/apply")).toHaveLength(0);
  });

  it("a readiness picker stages too, and a failed stage says why", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "POST /api/issues/pending": () =>
          json({ error: "TrayChangeStagedError", detail: "a priority change is staged" }, 409),
      }),
      ISSUE,
    );
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Readiness" }), "blocked");
    expect(await toast(/already staged a change for this in Pending changes/)).toBeTruthy();
    expect(await callsTo(calls, "POST", "/api/issues/pending")[0]?.clone().json()).toMatchObject({
      change: { kind: "set_dimension", dimension: "readiness", value: "blocked" },
    });
  });
});

describe("undo", () => {
  it("Undo takes one change out; the tray hides once it is empty", async () => {
    const t = trayRoutes([pending("p1")]);
    const { calls } = renderApp(issuesRoutes(t.routes));
    await userEvent.click(
      await within(await screen.findByRole("region", { name: "Pending changes" })).findByRole("button", {
        name: "Undo: Set priority to high on acme/app#1",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull());
    expect(callsTo(calls, "DELETE", "/api/issues/pending/p1")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("Undo all clears the tray; a held change says to undo it in its queue", async () => {
    const t = trayRoutes([pending("p1"), pending("p2", { n: 2, change: { kind: "close", reason: "completed" } })]);
    renderApp(
      issuesRoutes({
        ...t.routes,
        "DELETE /api/issues/pending/p1": () =>
          json({ error: "PendingHeldError", detail: "the queue Next holds it" }, 409),
      }),
    );
    const region = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(region).getByText("Close as completed")).toBeInTheDocument();
    await userEvent.click(within(region).getByRole("button", { name: "Undo: Set priority to high on acme/app#1" }));
    expect(await toast(/A queue item's Done holds this change/)).toBeTruthy();
    await userEvent.click(within(region).getByRole("button", { name: "Undo all" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull());
  });
});

describe("apply", () => {
  it("Apply sends one keyed batch, polls it, and shows per-issue results with Retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    const t = trayRoutes([pending("p1")]);
    const failed: ApplyBatch = batch("b1", {
      status: "finished",
      complete: false,
      issues: [
        {
          repo: REPO,
          n: 1,
          temp_id: null,
          steps: [{ kind: "labels", state: "failed", cause: "priority changed on GitHub", conflict: true }],
        },
      ],
    });
    let current: ApplyBatch = batch("b1");
    const { calls } = renderApp(
      issuesRoutes({
        ...t.routes,
        "POST /api/issues/pending/apply": () => {
          t.clear();
          return json(batch("b1"), 202, { Location: "/api/issues/pending/b1" });
        },
        "GET /api/issues/pending/b1": () => json(current),
        "POST /api/issues/pending/b1/dismiss": () => json(null, 204),
        "POST /api/issues/pending/b1/retry": () => {
          current = batch("b1");
          return json(current, 202, { Location: "/api/issues/pending/b1" });
        },
      }),
    );
    await user.click(
      await within(await screen.findByRole("region", { name: "Pending changes" })).findByRole("button", {
        name: /^Apply/,
      }),
    );
    expect(await within(tray()).findByText(/Applying/)).toBeInTheDocument();
    const [apply] = callsTo(calls, "POST", "/api/issues/pending/apply");
    expect(apply?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await apply?.clone().json()).toEqual({});
    expect(within(tray()).queryByText("Set priority to high")).toBeNull();

    current = failed;
    await tick(BATCH_POLL_MS);
    expect(await within(tray()).findByText("Some changes didn't apply")).toBeInTheDocument();
    const result = within(tray()).getByRole("listitem", { name: "acme/app#1" });
    expect(within(result).getByText(/priority changed on GitHub/)).toBeInTheDocument();
    expect(within(result).getByText("Changed on GitHub")).toBeInTheDocument();
    expect(within(tray()).queryByRole("button", { name: "Abandon" })).toBeNull(); // creates no issues

    await user.click(within(tray()).getByRole("button", { name: "Retry" }));
    expect(callsTo(calls, "POST", "/api/issues/pending/b1/retry")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await within(tray()).findByText(/Applying/)).toBeInTheDocument();
    current = finished("b1");
    await tick(BATCH_POLL_MS);
    expect(await within(tray()).findByText("All changes applied")).toBeInTheDocument();
    const polls = callsTo(calls, "GET", "/api/issues/pending/b1").length;
    await tick(BATCH_POLL_MS * 3);
    expect(callsTo(calls, "GET", "/api/issues/pending/b1")).toHaveLength(polls); // done: no more polls
    await user.click(within(tray()).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull());
  });

  it("⌘↵ applies; an answerless apply is retried with the same key", async () => {
    const t = trayRoutes([pending("p1")]);
    let attempts = 0;
    const { calls } = renderApp(
      issuesRoutes({
        ...t.routes,
        "POST /api/issues/pending/apply": () => {
          attempts += 1;
          if (attempts === 1) return json({ detail: "boom" }, 500);
          t.clear();
          return json(finished("b1"), 202);
        },
        "GET /api/issues/pending/b1": () => json(finished("b1")),
      }),
    );
    await screen.findByRole("region", { name: "Pending changes" });
    await userEvent.keyboard("{Meta>}{Enter}{/Meta}");
    expect(await toast(/Couldn't apply/)).toBeTruthy();
    await userEvent.keyboard("{Control>}{Enter}{/Control}");
    expect(await within(tray()).findByText("All changes applied")).toBeInTheDocument();
    const keys = callsTo(calls, "POST", "/api/issues/pending/apply").map((c) => c.headers.get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("applying an empty tray says nothing is staged", async () => {
    const t = trayRoutes([pending("p1")]);
    renderApp(
      issuesRoutes({
        ...t.routes,
        "POST /api/issues/pending/apply": () =>
          json({ error: "NothingPendingError", detail: "the tray is empty" }, 409),
      }),
    );
    await userEvent.click(
      await within(await screen.findByRole("region", { name: "Pending changes" })).findByRole("button", {
        name: /^Apply/,
      }),
    );
    expect(await toast(/Nothing is staged to apply/)).toBeTruthy();
  });

  it("an interrupted batch that created issues offers Retry and Abandon; Abandon is keyed", async () => {
    let abandoned = false;
    const interrupted = batch("b9", {
      origin: "epic",
      status: "interrupted",
      repo_steps: [
        { kind: "create_issue", state: "done", cause: null, conflict: false, repo: REPO, label: null, temp_id: "t1" },
      ],
      issues: [
        {
          repo: null,
          n: null,
          temp_id: "t1",
          steps: [{ kind: "parent", state: "skipped", cause: "interrupted by a stop", conflict: false }],
        },
      ],
      created: [{ temp_id: "t1", repo: REPO, n: 40 }],
    });
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/pending": { changes: [pending("p1")] },
        "POST /api/issues/pending/apply": () => json(interrupted, 202),
        "GET /api/issues/pending/b9": () => json(abandoned ? { ...interrupted, status: "abandoned" } : interrupted),
        "POST /api/issues/pending/b9/abandon": () => {
          abandoned = true;
          return json({
            batch_id: "b9",
            origin: "conversion:acme/app#3",
            state: "abandoned",
            created: [{ temp_id: "t1", repo: REPO, n: 40 }],
          });
        },
      }),
    );
    await userEvent.click(
      await within(await screen.findByRole("region", { name: "Pending changes" })).findByRole("button", {
        name: /^Apply/,
      }),
    );
    expect(await within(tray()).findByText(/Interrupted/)).toBeInTheDocument();
    expect(within(tray()).getByRole("listitem", { name: "acme/app#40" })).toBeInTheDocument();
    expect(within(tray()).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    await userEvent.click(within(tray()).getByRole("button", { name: "Abandon" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/pending/b9/abandon")).toHaveLength(1));
    expect(callsTo(calls, "POST", "/api/issues/pending/b9/abandon")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await screen.findByText(/Abandoned/)).toBeInTheDocument();
  });

  it("a batch GitHub stopped says so, with the rate limit", async () => {
    const stopped = finished("b2", {
      complete: false,
      stopped: "GitHub's rate limit",
      rate_limited: true,
      rate_limited_until: "2026-10-18T12:30:00Z",
      issues: [
        { repo: REPO, n: 1, temp_id: null, steps: [DONE_STEP] },
        {
          repo: REPO,
          n: 2,
          temp_id: null,
          steps: [{ kind: "labels", state: "failed", cause: "rate limited", conflict: false }],
        },
      ],
    });
    renderApp(
      issuesRoutes({
        "GET /api/issues/pending": { changes: [pending("p1")] },
        "POST /api/issues/pending/apply": () => json(stopped, 202),
      }),
    );
    await userEvent.click(
      await within(await screen.findByRole("region", { name: "Pending changes" })).findByRole("button", {
        name: /^Apply/,
      }),
    );
    expect(await within(tray()).findByText(/GitHub stopped this batch/)).toBeInTheDocument();
    expect(
      within(within(tray()).getByRole("listitem", { name: "acme/app#1" })).getByText("Applied"),
    ).toBeInTheDocument();
    expect(
      within(within(tray()).getByRole("listitem", { name: "acme/app#2" })).getByText(/rate limited/),
    ).toBeInTheDocument();
  });
});
