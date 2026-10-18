// SPDX-License-Identifier: AGPL-3.0-or-later
// Work queues (spec §5.2, rulings IR15, IR52, the Task 9 N1 band-move ruling): section tabs with
// counts, making a queue, Add to queue…, Done (checkbox, `x`, the Done zone, with a comment, or
// agent-ready), Undo, the band-move refusal's copy, Copy brief, and the agent's proposals as a
// draft diff to approve.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Pending, Queue, QueueItem } from "../api";
import { ASK_POLL_MS } from "../drafts";
import {
  callsTo,
  issuesRoutes,
  json,
  milestoneDetail,
  moveTo,
  pickUp,
  queue,
  queueItem,
  queueSummary,
  readiness,
  renderApp,
  ROW_1,
  said,
  settleDrag,
  stagingRoutes,
  suggestion,
  suggestions,
  tick,
  toast,
  view,
} from "./fixtures";

afterEach(async () => {
  vi.useRealTimers();
  await settleDrag();
});

const bodies = async (calls: Request[]) => Promise.all(calls.map((c) => c.clone().json() as Promise<unknown>));
const ITEM = (n: number) => `/api/issues/queues/q1/items/issue/acme/app/${n}`;
const staged = (id: string): Pending => ({ id, repo: "acme/app", n: 1, change: { kind: "close", reason: "completed" }, source: "queue", created_at: "2026-10-18T12:00:00Z" }); // prettier-ignore

function routesFor(start: Queue, over = {}) {
  let current = start;
  const tray: Pending[] = [];
  const answer = (q: Queue) => {
    current = q;
    return json(q);
  };
  return {
    tray,
    set: (q: Queue) => (current = q),
    routes: issuesRoutes({
      ...stagingRoutes(tray),
      "GET /api/issues/queues": () => json({ queues: [queueSummary("q1", { warnings: 1 }), queueSummary("q2", { name: "Mine", open: 0 })] }), // prettier-ignore
      "GET /api/issues/queues/q1": () => json(current),
      "GET /api/issues/queues/q2": () => json(queue("q2", [], { name: "Mine" })),
      [`PATCH ${ITEM(1)}`]: () => answer(current),
      [`PATCH ${ITEM(2)}`]: () => answer(current),
      [`DELETE ${ITEM(1)}`]: () =>
        answer(
          queue(
            "q1",
            current.items.filter((i) => i.n !== 1),
          ),
        ),
      [`POST ${ITEM(1)}/done`]: () => {
        tray.push(staged("p1"));
        const done = { ...(current.items[0] as QueueItem), done_at: "2026-10-18T12:00:00Z", done_pending: "p1" };
        current = queue("q1", current.items.slice(1), { done: [done] });
        return json({ queue: current, staged: tray[0] });
      },
      [`POST ${ITEM(1)}/undo`]: () => answer(queue("q1", [queueItem(1, { title: "Fix login" }), ...current.items])),
      ...over,
    }),
  };
}

const TWO = queue("q1", [queueItem(1, { title: "Fix login" }), queueItem(2, { title: "Add export" })]);

describe("the queue tabs", () => {
  it("shows one tab per queue with counts and warnings; arrows switch tabs", async () => {
    renderApp(routesFor(TWO).routes, "/issues/queues");
    const tabs = await screen.findByRole("tablist", { name: "Queues" });
    const first = within(tabs).getByRole("tab", { name: /Next up/ });
    expect(first).toHaveAttribute("aria-selected", "true");
    expect(first).toHaveTextContent("2 · 1 ⚠");
    first.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(within(tabs).getByRole("tab", { name: /Mine/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText(/Nothing open/)).toBeInTheDocument();
    await userEvent.keyboard("{ArrowLeft}");
    expect(first).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{Home}");
    expect(first).toHaveAttribute("aria-selected", "true");
  });

  it("makes a queue from a pasted name; a taken name says so", async () => {
    let taken = false;
    const { routes } = routesFor(TWO, {
      "POST /api/issues/queues": () =>
        taken
          ? json({ error: "QueueNameTakenError", detail: "taken" }, 409)
          : json(queue("q3", [], { name: "Launch" }), 201),
    });
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("textbox", { name: "New queue" }));
    await userEvent.paste("Launch");
    await userEvent.click(screen.getByRole("button", { name: "Make queue" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues"))).toEqual([{ name: "Launch", description: "" }]);
    taken = true;
    await userEvent.click(screen.getByRole("textbox", { name: "New queue" }));
    await userEvent.paste("Launch");
    await userEvent.click(screen.getByRole("button", { name: "Make queue" }));
    expect(await toast(/A queue with that name exists already/)).toBe(true);
  });

  it("an item dropped on another queue's tab is added to it; its own tab refuses", async () => {
    const { routes } = routesFor(TWO, { "POST /api/issues/queues/q2/items": () => json(queue("q2", []), 201) });
    const { calls } = renderApp(routes, "/issues/queues");
    await screen.findByRole("list", { name: "Next up" });
    const user = await pickUp("Move acme/app#1 Fix login");
    expect(await moveTo(user, "Next up")).toMatch(/in this queue already/);
    await moveTo(user, "Mine");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/queues/q2/items")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q2/items"))).toEqual([{ kind: "issue", repo: "acme/app", n: 1 }]); // prettier-ignore
    expect(await toast(/Added 1 item to Mine/)).toBe(true);
  });

  it("says so when there are no queues, or they can't be read", async () => {
    renderApp(
      issuesRoutes({ "GET /api/issues/queues": () => json({ error: "X", detail: "down" }, 500) }),
      "/issues/queues",
    );
    expect(await toast(/Couldn't read the queues: down/)).toBe(true);
  });

  it("Add to queue… from the Inbox adds the selected issues, saying which failed", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([ROW_1]),
        "GET /api/issues/queues": { queues: [queueSummary("q1")] },
        "POST /api/issues/queues/q1/items": () => json({ error: "QueueItemExistsError", detail: "exists" }, 409),
      }),
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add the selected issues to a queue" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Next up" }));
    expect(await toast(/Couldn't add acme\/app#1 to Next up: That's in the queue already/)).toBe(true);
    expect(callsTo(calls, "POST", "/api/issues/queues/q1/items")).toHaveLength(1);
  });

  it("Split across queues asks Claude and approves the draft diff, saying what was skipped", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let done = false;
    const SPLIT = suggestion("qs", { repo: null, n: null, scope: "queue_split:q1,q2", kind: "queue_split", payload: { queues: ["q1", "q2"], items: [{ kind: "issue", repo: "acme/app", n: 2, from: "q1", to: "q2", reason: "Yours." }] }, reason: "Balance." }); // prettier-ignore
    const run = (status: "running" | "done") => ({ scope: "queue_split:q1,q2", status, cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null }); // prettier-ignore
    const { routes } = routesFor(TWO, {
      "POST /api/issues/queues/suggestions/split": () => json(run("running"), 202),
      "GET /api/issues/suggestions": (req: Request) =>
        json(
          new URL(req.url).searchParams.get("scope") === "queue_split:q1,q2" && done
            ? suggestions([SPLIT], { ask_run: run("done") })
            : suggestions([], { ask_run: run("running") }),
        ),
      "POST /api/issues/queues/suggestions/qs/approve": () => json({ suggestion: SPLIT, queues: [], skipped: ["acme/app#9"] }), // prettier-ignore
    });
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "Split across queues" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/suggestions/split"))).toEqual([{ queue_ids: ["q1", "q2"] }]); // prettier-ignore
    done = true;
    await tick(ASK_POLL_MS);
    const draft = await screen.findByRole("listitem", { name: "Split across queues" });
    expect(draft).toHaveTextContent("Next up → Mine");
    expect(draft).toHaveTextContent("Yours.");
    await userEvent.click(within(draft).getByRole("button", { name: "Approve: Split across queues" }));
    expect(await toast(/skipped because they changed since: acme\/app#9/)).toBe(true);
  });
});

describe("one queue", () => {
  it("shows readiness, warnings, staged priorities, notes, kinds and uncached items", async () => {
    const q = queue("q1", [
      queueItem(1, { title: "Fix login", readiness: readiness({ blockers: [{ repo: "acme/app", n: 1, reason: "x" }] }), warning: "needs readiness", note: "after lunch", pending_priority: "priority: high" }), // prettier-ignore
      queueItem(5, { kind: "milestone", title: "v1", readiness: null }),
      queueItem(6, { title: null }),
    ]);
    renderApp(routesFor(q).routes, "/issues/queues");
    const list = await screen.findByRole("list", { name: "Next up" });
    const [first, second, third] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("1 blocking");
    expect(first).toHaveTextContent("⚠ needs readiness");
    expect(first).toHaveTextContent("high priority staged");
    expect(first).toHaveTextContent("after lunch");
    expect(second).toHaveTextContent("milestone");
    expect(third).toHaveTextContent("Not cached");
    expect(screen.getByText("⚠ 1 not ready: see each warning")).toBeInTheDocument();
  });

  it("the x key marks the focused item done; Undo in the Done group takes it back", async () => {
    const { routes, tray } = routesFor(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    (await screen.findByRole("checkbox", { name: "Done: acme/app#1 Fix login" })).focus();
    await userEvent.keyboard("x");
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(1)}/done`)).toHaveLength(1));
    expect(await toast(/Closing it is staged in Pending changes/)).toBe(true);
    const done = screen.getByRole("group", { name: "Done (1)" });
    expect(done).toHaveTextContent("its change waits in Pending changes");
    tray.length = 0;
    await userEvent.click(within(done).getByRole("button", { name: "Undo Done: acme/app#1" }));
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(1)}/undo`)).toHaveLength(1));
    expect(await screen.findByRole("group", { name: "Done (0)" })).toHaveTextContent("Nothing done yet.");
  });

  it("Done with a comment, and Done to hand to an agent, from the item's menu", async () => {
    const { routes } = routesFor(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "More for acme/app#1" }));
    await userEvent.click(screen.getByRole("textbox", { name: "Done, with a comment on acme/app#1" }));
    await userEvent.paste("Fixed in the release.");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(1)}/done`)).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", `${ITEM(1)}/done`))).toEqual([{ then: "close", comment: "Fixed in the release." }]); // prettier-ignore
  });

  it("agent-ready Done, Move down, Remove and a band move are in the item's menu", async () => {
    const { routes } = routesFor(TWO, {
      [`POST ${ITEM(2)}/done`]: () => json({ error: "X", detail: "nope" }, 409),
      [`POST ${ITEM(1)}/band`]: () => json({ queue: TWO, staged: null }),
    });
    const { calls } = renderApp(routes, "/issues/queues");
    const pick = async (ref: string, choice: string) => {
      await userEvent.click(await screen.findByRole("button", { name: `More for ${ref}` }));
      await userEvent.click(await screen.findByRole("menuitem", { name: choice }));
    };
    await pick("acme/app#2", "Done: hand it to an agent (agent-ready)");
    expect(await toast(/Couldn't mark acme\/app#2 done: nope/)).toBe(true);
    expect(await bodies(callsTo(calls, "POST", `${ITEM(2)}/done`))).toEqual([{ then: "agent_ready" }]);
    await pick("acme/app#1", "Move down");
    await waitFor(() => expect(callsTo(calls, "PATCH", ITEM(1))).toHaveLength(1));
    expect(await bodies(callsTo(calls, "PATCH", ITEM(1)))).toEqual([{ position: 1 }]);
    await pick("acme/app#2", "Move up");
    await waitFor(() => expect(callsTo(calls, "PATCH", ITEM(2))).toHaveLength(1));
    await pick("acme/app#1", "Move to high priority");
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(1)}/band`)).toHaveLength(1));
    await pick("acme/app#1", "Remove from Next up");
    await waitFor(() => expect(callsTo(calls, "DELETE", ITEM(1))).toHaveLength(1));
  });

  it("a reorder offers Undo, which moves it back", async () => {
    const { routes } = routesFor(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "More for acme/app#1" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Move down" }));
    expect(await toast(/Moved acme\/app#1 to position 2 in Next up/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(callsTo(calls, "PATCH", ITEM(1))).toHaveLength(2));
    expect((await bodies(callsTo(calls, "PATCH", ITEM(1))))[1]).toEqual({ position: 0 });
  });

  it("the Done and Remove zones take dropped items", async () => {
    const { routes } = routesFor(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    await screen.findByRole("list", { name: "Next up" });
    let user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, "Mark done in Next up");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(1)}/done`)).toHaveLength(1));
    await settleDrag();
    user = await pickUp("Move acme/app#2 Add export");
    await moveTo(user, "Remove from Next up");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "DELETE", ITEM(2))).toHaveLength(1));
  });

  it("a band move while the tray holds a priority change says so; within a band it reorders", async () => {
    const q = queue("q1", [
      queueItem(1, { title: "Fix login", priority: "priority: high" }),
      queueItem(2, { title: "Add export", priority: "priority: high" }),
      queueItem(3, { title: "Low one", priority: "priority: low" }),
    ]);
    const { routes } = routesFor(q, {
      [`POST ${ITEM(3)}/band`]: () =>
        json({ error: "TrayChangeStagedError", detail: "a priority change is staged" }, 409),
      [`PATCH ${ITEM(2)}`]: () => json(q),
    });
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("radio", { name: "By priority" }));
    let user = await pickUp("Move acme/app#3 Low one");
    expect(await moveTo(user, "No priority")).toMatch(/Taking the priority off/);
    await moveTo(user, /^High priority, position 1 of/);
    await user.keyboard("[Space]");
    expect(await toast(/already has a priority change staged from the tray.*Reordering within its band still works/)).toBe(true); // prettier-ignore
    await settleDrag();
    user = await pickUp("Move acme/app#2 Add export");
    await moveTo(user, /^High priority, position 1 of/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "PATCH", ITEM(2))).toHaveLength(1));
    expect(await bodies(callsTo(calls, "PATCH", ITEM(2)))).toEqual([{ position: 0 }]);
  });

  it("Copy brief: a milestone's brief, an issue's own lines, and a failed read", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const q = queue("q1", [queueItem(5, { kind: "milestone", title: "v1" }), queueItem(1, { title: "Fix login" }), queueItem(9, { kind: "epic" })]); // prettier-ignore
    renderApp(
      routesFor(q, {
        "GET /api/issues/milestones/acme/app/5": milestoneDetail("Milestone brief"),
        "GET /api/issues/epics/acme/app/9": () => json({ error: "X", detail: "gone" }, 404),
      }).routes,
      "/issues/queues",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Copy brief for acme/app#5" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Milestone brief"));
    await userEvent.click(screen.getByRole("button", { name: "Copy brief for acme/app#1" }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("# Fix login\n\nacme/app#1\nhttps://github.com/acme/app/issues/1\n")); // prettier-ignore
    await userEvent.click(screen.getByRole("button", { name: "Copy brief for acme/app#9" }));
    expect(await toast(/Couldn't read the brief for acme\/app#9: gone/)).toBe(true);
  });

  it("asks for what to add next and a new order, shows each as a diff, approves and dismisses", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const NEXT = suggestion("n1", { repo: null, n: null, scope: "queue_next:q1", kind: "queue_next", payload: { queue: "q1", items: [{ kind: "issue", repo: "acme/app", n: 7, reason: "Ready and high." }] }, reason: "Add 1 items" }); // prettier-ignore
    const ORDER = suggestion("o1", { repo: null, n: null, scope: "queue_order:q1", kind: "queue_order", payload: { queue: "q1", items: [{ kind: "issue", repo: "acme/app", n: 2 }, { kind: "issue", repo: "acme/app", n: 1 }] }, reason: "Blocked first." }); // prettier-ignore
    let asked = false;
    const run = (scope: string) => ({ scope, status: asked ? "done" : "running", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null }) as const; // prettier-ignore
    const { routes } = routesFor(TWO, {
      "POST /api/issues/queues/q1/suggestions/next": () => json({ ...run("queue_next:q1"), status: "running" }, 202),
      "POST /api/issues/queues/q1/suggestions/order": () => json({ ...run("queue_order:q1"), status: "running" }, 202),
      "GET /api/issues/suggestions": (req: Request) => {
        const scope = new URL(req.url).searchParams.get("scope") ?? "";
        const found = !asked ? [] : scope === "queue_next:q1" ? [NEXT] : scope === "queue_order:q1" ? [ORDER] : [];
        return json(suggestions(found, { ask_run: run(scope) }));
      },
      "POST /api/issues/queues/suggestions/n1/approve": () => json({ suggestion: NEXT, queues: [TWO], skipped: [] }),
      "POST /api/issues/queues/suggestions/o1/approve": () => json({ error: "X", detail: "stale" }, 409),
      "POST /api/issues/suggestions/o1/dismiss": () => json(ORDER),
    });
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "Suggest next" }));
    await userEvent.click(screen.getByRole("button", { name: "Order this queue" }));
    asked = true;
    await tick(ASK_POLL_MS);
    const next = await screen.findByRole("listitem", { name: "Add next" });
    expect(next).toHaveTextContent("+ Add acme/app#7 Ready and high.");
    const order = await screen.findByRole("listitem", { name: "A new order" });
    expect(order).toHaveTextContent("2 → 1");
    expect(order).toHaveTextContent("1 → 2");
    await userEvent.click(within(next).getByRole("button", { name: "Approve: Add next" }));
    expect(await toast(/Approved: Next up changed here only/)).toBe(true);
    await userEvent.click(within(order).getByRole("button", { name: "Approve: A new order" }));
    expect(await toast(/Couldn't approve the proposal: stale/)).toBe(true);
    await userEvent.click(within(order).getByRole("button", { name: "Dismiss: A new order" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/o1/dismiss")).toHaveLength(1));
  });

  it("a queue that can't be read says why", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/queues": { queues: [queueSummary("q1")] },
        "GET /api/issues/queues/q1": () => json({ error: "QueueNotFoundError", detail: "gone" }, 404),
      }),
      "/issues/queues",
    );
    expect(await screen.findByText("Couldn't open the queue: gone.")).toBeInTheDocument();
  });
});

describe("fix round 1: kinds, Undo for adds and removes, focus after Done", () => {
  const doneRoute = (routes: { set: (q: Queue) => void }, get: () => Queue, n: number) => () => {
    const q = get();
    const item = q.items.find((i) => i.n === n) as QueueItem;
    const next = queue("q1", q.items.filter((i) => i.n !== n), { done: [{ ...item, done_at: "2026-10-18T12:00:00Z", done_pending: null }] }); // prettier-ignore
    routes.set(next);
    return json({ queue: next, staged: null });
  };

  function liveRoutes(start: Queue, over: (get: () => Queue) => Record<string, unknown> = () => ({})) {
    let current = start;
    const get = () => current;
    const holder = { set: (q: Queue) => (current = q) };
    const made = routesFor(start, {
      "GET /api/issues/queues/q1": () => json(current),
      [`POST ${ITEM(1)}/done`]: doneRoute(holder, get, 1),
      [`POST ${ITEM(2)}/done`]: doneRoute(holder, get, 2),
      [`POST ${ITEM(1)}/undo`]: () => {
        current = queue("q1", [queueItem(1, { title: "Fix login" }), ...current.items]);
        return json(current);
      },
      [`DELETE ${ITEM(1)}`]: () => {
        current = queue("q1", current.items.filter((i) => i.n !== 1)); // prettier-ignore
        return json(current);
      },
      ...over(get),
    });
    return made;
  }

  it("a milestone dropped on another queue's tab stays a milestone, and the add has Undo (I3, I5)", async () => {
    const q = queue("q1", [queueItem(5, { kind: "milestone", title: "v1" }), queueItem(1, { title: "Fix login" })]);
    const { routes } = routesFor(q, {
      "POST /api/issues/queues/q2/items": () => json(queue("q2", []), 201),
      "DELETE /api/issues/queues/q2/items/milestone/acme/app/5": () => json(queue("q2", [])),
    });
    const { calls } = renderApp(routes, "/issues/queues");
    await screen.findByRole("list", { name: "Next up" });
    const user = await pickUp("Move acme/app#5 v1");
    await moveTo(user, "Mine");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/queues/q2/items")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q2/items"))).toEqual([{ kind: "milestone", repo: "acme/app", n: 5 }]); // prettier-ignore
    expect(await toast(/Added 1 item to Mine/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(callsTo(calls, "DELETE", "/api/issues/queues/q2/items/milestone/acme/app/5")).toHaveLength(1)); // prettier-ignore
    expect(await toast(/Took 1 item back out of Mine/)).toBe(true);
  });

  it("Remove has Undo, which puts the item back at its place with its note (I5)", async () => {
    const q = queue("q1", [queueItem(1, { title: "Fix login", note: "after lunch" }), queueItem(2, { title: "Add export" })]); // prettier-ignore
    const { routes } = liveRoutes(q, (get) => ({
      "POST /api/issues/queues/q1/items": () => json(queue("q1", [queueItem(1, { title: "Fix login", note: "after lunch" }), ...get().items]), 201), // prettier-ignore
    }));
    const { calls } = renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "More for acme/app#1" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove from Next up" }));
    expect(await toast(/Removed acme\/app#1 from Next up/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/queues/q1/items")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q1/items"))).toEqual([{ kind: "issue", repo: "acme/app", n: 1, position: 0, note: "after lunch" }]); // prettier-ignore
    expect(await screen.findByRole("checkbox", { name: "Done: acme/app#1 Fix login" })).toBeInTheDocument();
  });

  it("after x, focus moves to the next item, so a second x marks that one done (I4)", async () => {
    const { routes } = liveRoutes(TWO);
    const { calls } = renderApp(routes, "/issues/queues");
    (await screen.findByRole("checkbox", { name: "Done: acme/app#1 Fix login" })).focus();
    await userEvent.keyboard("x");
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Done: acme/app#2 Add export" })).toHaveFocus());
    await userEvent.keyboard("x");
    await waitFor(() => expect(callsTo(calls, "POST", `${ITEM(2)}/done`)).toHaveLength(1));
    // The last one gone, focus stays in the queue rather than falling to the page.
    await waitFor(() => expect(document.activeElement).not.toBe(document.body));
  });

  it("after Remove from the menu, or Undo in the Done group, focus lands on an item, not the page (I4)", async () => {
    const { routes } = liveRoutes(TWO);
    renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "More for acme/app#1" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove from Next up" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Done: acme/app#2 Add export" })).toHaveFocus());
    await userEvent.click(screen.getByRole("checkbox", { name: "Done: acme/app#2 Add export" }));
    const group = await screen.findByRole("group", { name: "Done (1)" });
    expect(document.activeElement).not.toBe(document.body);
    await userEvent.click(within(group).getByRole("button", { name: "Undo Done: acme/app#2" }));
    await waitFor(() => expect(document.activeElement).not.toBe(document.body));
  });

  it("a milestone has no Done (the server refuses it), and x inside an open menu doesn't mark done (M2, M10)", async () => {
    const q = queue("q1", [queueItem(5, { kind: "milestone", title: "v1" }), queueItem(1, { title: "Fix login" })]);
    const { calls } = renderApp(routesFor(q).routes, "/issues/queues");
    await screen.findByRole("list", { name: "Next up" });
    expect(screen.queryByRole("checkbox", { name: "Done: acme/app#5 v1" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "More for acme/app#5" }));
    expect(screen.queryByRole("menuitem", { name: /Done/ })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /Done, with a comment/ })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "More for acme/app#1" }));
    await waitFor(() => expect(screen.getAllByRole("menuitem")[0]).toHaveFocus());
    await userEvent.keyboard("x");
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(callsTo(calls, "POST", `${ITEM(1)}/done`)).toHaveLength(0);
  });

  it("a readiness note built from label names shows hidden characters as tokens (M1)", async () => {
    const q = queue("q1", [queueItem(1, { title: "Fix login", readiness: readiness({ note: "held\u200b" }) })]);
    renderApp(routesFor(q).routes, "/issues/queues");
    expect(await screen.findByRole("list", { name: "Next up" })).toHaveTextContent("held⟪U+200B⟫");
  });

  it("an Undo that fails says why: taking an add back out, and putting a removed item back (I5)", async () => {
    const { routes } = routesFor(TWO, {
      "GET /api/issues/inbox": view([ROW_1]),
      "POST /api/issues/queues/q2/items": () => json(queue("q2", []), 201),
      "DELETE /api/issues/queues/q2/items/issue/acme/app/1": () => json({ error: "X", detail: "locked" }, 503),
    });
    renderApp(routes);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add the selected issues to a queue" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Mine" }));
    expect(await toast(/Added 1 item to Mine/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(await toast(/Couldn't take acme\/app#1 back out of Mine: locked/)).toBe(true);
  });

  it("putting a removed item back can fail, and says why (I5)", async () => {
    const { routes } = routesFor(TWO, {
      "POST /api/issues/queues/q1/items": () => json({ error: "X", detail: "full" }, 409),
    });
    renderApp(routes, "/issues/queues");
    await userEvent.click(await screen.findByRole("button", { name: "More for acme/app#1" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove from Next up" }));
    expect(await toast(/Removed acme\/app#1 from Next up/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(await toast(/Couldn't put acme\/app#1 back: full/)).toBe(true);
  });
});

describe("Codex round 1 on PR #72", () => {
  it("a done item says what is so: waiting in the tray, closed on GitHub, or done here", async () => {
    const q = queue("q1", [queueItem(3, { title: "Open one" })], {
      done: [
        queueItem(1, { title: "Waiting", done_at: "2026-10-18T12:00:00Z", done_pending: "p1" }),
        queueItem(2, { title: "Closed", state: "closed", done_at: "2026-10-18T12:00:00Z" }),
        queueItem(4, { title: "Handed over", state: "open", done_at: "2026-10-18T12:00:00Z" }),
      ],
    });
    renderApp(routesFor(q).routes, "/issues/queues");
    const done = await screen.findByRole("group", { name: "Done (3)" });
    const rows = within(done).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("its change waits in Pending changes");
    expect(rows[1]).toHaveTextContent("closed on GitHub");
    expect(rows[2]).toHaveTextContent("done here");
    expect(rows[2]).not.toHaveTextContent("closed on GitHub");
  });

  it("the Done zone refuses a milestone, with the reason, and never says it dropped", async () => {
    const q = queue("q1", [queueItem(5, { kind: "milestone", title: "v1" }), queueItem(1, { title: "Fix login" })]);
    const { calls } = renderApp(routesFor(q).routes, "/issues/queues");
    await screen.findByRole("list", { name: "Next up" });
    const user = await pickUp("Move acme/app#5 v1");
    expect(await moveTo(user, "Mark done in Next up")).toMatch(
      /^Can't drop on Mark done in Next up: A milestone isn't yours to close/,
    );
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toMatch(/Nothing changed\.$/));
    expect(said()).not.toMatch(/^Dropped/);
    expect(calls.filter((c) => new URL(c.url).pathname.endsWith("/done"))).toHaveLength(0);
  });
});
