// SPDX-License-Identifier: AGPL-3.0-or-later
// Task 12 re-review 1: the refresh's answer survives a revision bump (N1), a dismissed batch never
// comes back (N2), focus doesn't jump later (N5), and a stale failed read raises no error (N7).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SYNCING_POLL_MS } from "../api";
import {
  callsTo,
  detail,
  finished,
  issuesRoutes,
  json,
  REPO,
  renderApp,
  row,
  S_DUP,
  S_READY,
  suggestions,
  tick,
  toast,
  view,
} from "./fixtures";

const ISSUE = "/issues/acme/app/1";
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

function held<T>(body: T, status = 200) {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { route: async () => (await gate, json(body, status)), release: () => release() };
}

const stuck = finished("b7", {
  complete: false,
  issues: [
    { repo: REPO, n: 1, temp_id: null, steps: [{ kind: "labels", state: "failed", cause: "boom", conflict: false }] },
  ],
});

describe("N1: a revision bump during the refresh", () => {
  it("still shows the refreshed comments", async () => {
    // An unfinished batch on load bumps the revision: the cache is read again while the refresh
    // (which writes the cache) is still out. Its older answer must not win.
    const refresh = held(detail());
    renderApp(
      issuesRoutes({
        "GET /api/issues/batches": { batches: [stuck] },
        "GET /api/issues/item/acme/app/1": detail({ comments: [] }),
        "POST /api/issues/item/acme/app/1/refresh": () => refresh.route(),
      }),
      ISSUE,
    );
    await screen.findByRole("region", { name: "Pending changes" });
    await screen.findByRole("heading", { level: 1 });
    await settle(); // the bumped cache read has answered (no comments), the refresh hasn't
    expect(screen.queryByText("Seen it too.")).toBeNull();
    refresh.release();
    expect(await screen.findByText("Seen it too.")).toBeInTheDocument();
  });
});

describe("N2: a dismissed batch never comes back", () => {
  it("Dismiss is kept on the server (keyed), so a reload doesn't list it", async () => {
    let dismissed = false;
    const routes = issuesRoutes({
      "GET /api/issues/batches": () => json({ batches: dismissed ? [] : [stuck] }),
      "POST /api/issues/pending/b7/dismiss": () => {
        dismissed = true;
        return json(null, 204);
      },
    });
    const first = renderApp(routes);
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    await userEvent.click(within(tray).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull());
    expect(
      callsTo(first.calls, "POST", "/api/issues/pending/b7/dismiss")[0]?.headers.get("Idempotency-Key"),
    ).toBeTruthy();
    first.unmount();
    renderApp(routes); // the reload
    await screen.findByText("Fix login");
    await settle();
    expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull();
  });

  it("a dismiss the server refuses keeps the batch and says why", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/batches": { batches: [stuck] },
        "POST /api/issues/pending/b7/dismiss": () => json({ error: "BatchRunningError", detail: "still going" }, 409),
      }),
    );
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    await userEvent.click(within(tray).getByRole("button", { name: "Dismiss" }));
    expect(await toast(/Couldn't dismiss the batch/)).toBe(true);
    expect(within(tray).getByText("Unfinished: staged changes")).toBeInTheDocument();
  });
});

describe("N5: focus doesn't jump later", () => {
  it("a failed Accept that kept its suggestion doesn't pull focus on a later render", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": suggestions([S_READY, S_DUP]),
        "POST /api/issues/suggestions/s1/approve": () => json({ error: "SuggestionStaleError", detail: "gone" }, 409),
      }),
      ISSUE,
    );
    const region = await screen.findByRole("region", { name: "Suggestions" });
    const accept = within(region).getAllByRole("button", { name: "Accept" })[0] as HTMLElement;
    await userEvent.click(accept);
    await toast(/GitHub changed/);
    await settle();
    (document.activeElement as HTMLElement).blur();
    fireEvent.click(within(region).getAllByRole("button", { name: "Edit" })[0] as HTMLElement); // re-renders
    await settle();
    expect(document.activeElement).toBe(document.body);
  });
});

describe("N7: a stale failed read", () => {
  it("raises no error over newer rows and doesn't change the poll pace", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    const slow = held({ detail: "boom" }, 500);
    try {
      const { calls } = renderApp(
        issuesRoutes({
          "GET /api/issues/backlog": (req: Request) =>
            new URL(req.url).searchParams.get("filter") ? json(view([row(2, { title: "Fresh row" })])) : slow.route(),
        }),
        "/issues/backlog",
      );
      await user.selectOptions(await screen.findByRole("combobox", { name: "Filter" }), "incomplete");
      expect(await screen.findByText("Fresh row")).toBeInTheDocument();
      slow.release();
      await tick(10);
      const reads = callsTo(calls, "GET", "/api/issues/backlog").length;
      await tick(SYNCING_POLL_MS * 2); // a counted failure would re-read on the backoff
      expect(callsTo(calls, "GET", "/api/issues/backlog")).toHaveLength(reads);
      expect(screen.queryByText(/Couldn't read the Backlog/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
