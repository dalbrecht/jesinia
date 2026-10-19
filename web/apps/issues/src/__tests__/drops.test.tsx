// SPDX-License-Identifier: AGPL-3.0-or-later
// Task 13's drops (spec §5.2, §6.1, rulings IR15, IR17, IR22, IR39): an epic takes issues as
// staged sub-issue links and refuses an epic into its own child; a queue's order is local and
// immediate while a band move stages the priority; Done stages the close; a theme drop is local
// with Undo; Copy brief puts the brief on the clipboard. Driven by keyboard, the drag primitives'
// equivalent of a pointer drag.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Pending, Queue } from "../api";
import {
  callsTo,
  epic,
  epicDetail,
  issuesRoutes,
  json,
  moveTo,
  pickUp,
  queue,
  queueItem,
  queueSummary,
  renderApp,
  ROW_1,
  ROW_2,
  row,
  said,
  settleDrag,
  stagingRoutes,
  theme,
  toast,
  view,
} from "./fixtures";

afterEach(settleDrag);

const bodies = async (calls: Request[]) => Promise.all(calls.map((c) => c.clone().json() as Promise<unknown>));

describe("epics", () => {
  it("dropping 2 issues on an epic stages 2 SetParent changes, one per issue (IR39)", async () => {
    const staged: Pending[] = [];
    const { calls } = renderApp(
      issuesRoutes({
        ...stagingRoutes(staged),
        "GET /api/issues/epics": { epics: [epic(10, { title: "Onboarding" })] },
        "GET /api/issues/backlog": view([ROW_1, ROW_2]),
      }),
      "/issues/epics",
    );
    const pool = await screen.findByRole("list", { name: "Open issues without an epic" });
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#2" }));
    const user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, "acme/app#10 Onboarding");
    await user.keyboard("[Space]");
    await waitFor(() => expect(staged).toHaveLength(2));
    const sent = await bodies(callsTo(calls, "POST", "/api/issues/pending"));
    expect(sent).toEqual([
      expect.objectContaining({ repo: "acme/app", n: 1, source: "drag", change: expect.objectContaining({ kind: "set_parent", parent: { repo: "acme/app", n: 10 }, expects_parent: true, expected_parent: null }) }), // prettier-ignore
      expect.objectContaining({ repo: "acme/app", n: 2, source: "drag", change: expect.objectContaining({ kind: "set_parent", parent: { repo: "acme/app", n: 10 } }) }), // prettier-ignore
    ]);
  });

  it("an epic can't be dropped into its own child: the drop is refused with the reason, and the menu says so", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        ...stagingRoutes(),
        "GET /api/issues/epics": {
          epics: [epic(10, { title: "Onboarding" }), epic(11, { title: "Sign-up", parent: "acme/app#10" })],
        },
      }),
      "/issues/epics",
    );
    await screen.findByRole("list", { name: "Epics" });
    const user = await pickUp("Move acme/app#10 Onboarding");
    const refused = await moveTo(user, "acme/app#11 Sign-up");
    expect(refused).toMatch(/^Can't drop on acme\/app#11 Sign-up: acme\/app#10 contains acme\/app#11/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toMatch(/Nothing changed\.$/));
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);

    // The menu equivalent shows the same refusal, and can't pick it.
    await userEvent.click(screen.getByRole("button", { name: "Move acme/app#10 to an epic" }));
    const item = screen.getByRole("menuitem", { name: /acme\/app#11 Sign-up/ });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent(/contains acme\/app#11/);
    await userEvent.click(item);
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);
  });
});

function queueRoutes(start: Queue) {
  let current = start;
  const staged: Pending[] = [];
  return {
    staged,
    routes: issuesRoutes({
      ...stagingRoutes(staged),
      "GET /api/issues/queues": { queues: [queueSummary("q1")] },
      "GET /api/issues/queues/q1": () => json(current),
      "PATCH /api/issues/queues/q1/items/issue/acme/app/2": () => {
        current = queue("q1", [queueItem(2, { title: "Add export" }), queueItem(1, { title: "Fix login" })]);
        return json(current);
      },
      "POST /api/issues/queues/q1/items/issue/acme/app/1/band": () => {
        const made = { id: "p9", repo: "acme/app", n: 1, change: { kind: "set_dimension" as const, dimension: "priority" as const, value: "priority: high" }, source: "queue" as const, created_at: "2026-10-18T12:00:00Z" }; // prettier-ignore
        staged.push(made);
        current = queue("q1", [
          queueItem(1, { title: "Fix login", pending_priority: "priority: high" }),
          queueItem(2, { title: "Add export", priority: "priority: high" }),
        ]);
        return json({ queue: current, staged: made });
      },
      "POST /api/issues/queues/q1/items/issue/acme/app/1/done": () => {
        const made = { id: "p8", repo: "acme/app", n: 1, change: { kind: "close" as const, reason: "completed" as const }, source: "queue" as const, created_at: "2026-10-18T12:00:00Z" }; // prettier-ignore
        staged.push(made);
        current = queue("q1", [queueItem(2, { title: "Add export" })], {
          done: [queueItem(1, { title: "Fix login", done_at: "2026-10-18T12:00:00Z", done_pending: "p8" })],
        });
        return json({ queue: current, staged: made });
      },
    }),
  };
}

const TWO = queue("q1", [queueItem(1, { title: "Fix login" }), queueItem(2, { title: "Add export" })]);

describe("queues", () => {
  it("a reorder is local and immediate; nothing is staged", async () => {
    const { routes } = queueRoutes(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    const list = await screen.findByRole("list", { name: "Next up" });
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Fix login");
    const user = await pickUp("Move acme/app#2 Add export");
    await moveTo(user, /^Next up, position 1 of/);
    await user.keyboard("[Space]");
    await waitFor(() =>
      expect(callsTo(calls, "PATCH", "/api/issues/queues/q1/items/issue/acme/app/2")).toHaveLength(1),
    );
    expect(await bodies(callsTo(calls, "PATCH", "/api/issues/queues/q1/items/issue/acme/app/2"))).toEqual([{ position: 0 }]); // prettier-ignore
    await waitFor(() => expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("Add export"));
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);
  });

  it("a move into another priority band stages the priority (source queue) and shows it in the tray", async () => {
    const { routes } = queueRoutes(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("radio", { name: "By priority" }));
    await screen.findByRole("list", { name: "High priority" });
    const user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, /^High priority, position 1 of/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/queues/q1/items/issue/acme/app/1/band")).toHaveLength(1)); // prettier-ignore
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q1/items/issue/acme/app/1/band"))).toEqual([{ band: "priority: high", position: 0 }]); // prettier-ignore
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Set priority to high")).toBeInTheDocument();
  });

  it("Done moves the item to the Done group at once and stages its close", async () => {
    const { routes } = queueRoutes(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Done: acme/app#1 Fix login" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q1/items/issue/acme/app/1/done"))).toEqual([{ then: "close" }]); // prettier-ignore
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Close as completed")).toBeInTheDocument();
    const done = screen.getByRole("group", { name: /^Done/ });
    expect(within(done).getByText("Fix login")).toBeInTheDocument();
  });

  it("Copy brief puts an epic item's brief on the clipboard", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const routes = issuesRoutes({
      "GET /api/issues/queues": { queues: [queueSummary("q1")] },
      "GET /api/issues/queues/q1": queue("q1", [queueItem(10, { kind: "epic", title: "Onboarding" })]),
      "GET /api/issues/epics/acme/app/10": epicDetail(10),
    });
    renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "Copy brief for acme/app#10" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(epicDetail(10).default_brief));
    expect(await toast(/Copied the brief for acme\/app#10/)).toBe(true);
  });
});

describe("themes", () => {
  it("a theme drop is local and immediate, with Undo that takes it off again (IR22)", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        ...stagingRoutes(),
        "GET /api/issues/inbox": view([ROW_1, row(5, { title: "Other" })]),
        "GET /api/issues/themes": { themes: [theme("onboarding")] },
        "POST /api/issues/themes/onboarding/issues": () => json({ tag: "onboarding", added: [{ repo: "acme/app", n: 1 }] }, 201), // prettier-ignore
        "POST /api/issues/themes/onboarding/issues/remove": () => json({ removed: 1 }),
      }),
    );
    await screen.findByRole("list", { name: "Inbox" });
    const user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, "#onboarding");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/themes/onboarding/issues")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/themes/onboarding/issues"))).toEqual([{ issues: [{ repo: "acme/app", n: 1 }] }]); // prettier-ignore
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);
    expect(await toast(/Added #onboarding to acme\/app#1/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/themes/onboarding/issues/remove")).toHaveLength(1)); // prettier-ignore
    expect(await bodies(callsTo(calls, "POST", "/api/issues/themes/onboarding/issues/remove"))).toEqual([{ issues: [{ repo: "acme/app", n: 1 }] }]); // prettier-ignore
  });
});
