// SPDX-License-Identifier: AGPL-3.0-or-later
// The board (spec §6.1, §6.2): readiness and priority columns take drops, which stage the label
// in the tray; status columns are derived and refuse drops with the reason. Driven by keyboard,
// the drag primitives' equivalent of a pointer drag.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { Pending } from "../api";
import { callsTo, issuesRoutes, json, pending, renderApp, row, ROW_1, ROW_2, view } from "./fixtures";

const said = () => document.querySelector("[data-dnd-announcer]")?.textContent ?? "";
const column = (name: string) => screen.getByRole("group", { name });

beforeAll(() => {
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(async () => {
  // Never leave the keyboard sensor mid-drag for the next test.
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape", code: "Escape" });
});

function stagingRoutes(rows = [ROW_1, ROW_2]) {
  const staged: Pending[] = [];
  return {
    staged,
    routes: issuesRoutes({
      "GET /api/issues/inbox": view(rows),
      "GET /api/issues/pending": () => json({ changes: staged }),
      "POST /api/issues/pending": async (req: Request) => {
        const body = (await req.json()) as Pick<Pending, "repo" | "n" | "change" | "source">;
        const made = pending(`p${staged.length + 1}`, body);
        staged.push(made);
        return json(made, 201);
      },
    }),
  };
}

async function openBoard(grouping: "Readiness" | "Priority" | "Status" = "Readiness") {
  await userEvent.click(await screen.findByRole("radio", { name: "Board" }));
  await userEvent.click(screen.getByRole("radio", { name: grouping }));
}

async function pickUp(name: string): Promise<UserEvent> {
  const user = userEvent.setup();
  screen.getByRole("button", { name }).focus();
  await user.keyboard("[Space]");
  await waitFor(() => expect(said()).toMatch(/^Picked up/));
  return user;
}

/** Arrow through the targets until the announcer says we are over (or refused by) `target`. */
async function moveTo(user: UserEvent, target: string) {
  const reached = () => said() === `Over ${target}.` || said().startsWith(`Can't drop on ${target}:`);
  for (const key of ["[ArrowDown]", "[ArrowUp]"]) {
    for (let i = 0; i < 20; i += 1) {
      await user.keyboard(key);
      if (reached()) return said();
    }
  }
  throw new Error(`never reached ${target}; last said: ${said()}`);
}

describe("readiness and priority columns", () => {
  it("lays issues out in columns by readiness, with a column for none", async () => {
    renderApp(stagingRoutes().routes);
    await openBoard();
    expect(within(column("No readiness")).getByText("Fix login")).toBeInTheDocument();
    expect(within(column("agent-ready")).getByText("Add export")).toBeInTheDocument();
    expect(within(column("blocked")).queryByText("Fix login")).toBeNull();
  });

  it("a drop on a readiness column stages that label (source drag), and nothing is written", async () => {
    const { staged, routes } = stagingRoutes();
    const { calls } = renderApp(routes);
    await openBoard();
    const user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, "blocked");
    await user.keyboard("[Space]");
    await waitFor(() => expect(staged).toHaveLength(1));
    expect(staged[0]).toMatchObject({
      repo: "acme/app",
      n: 1,
      change: { kind: "set_dimension", dimension: "readiness", value: "blocked" },
      source: "drag",
    });
    expect(callsTo(calls, "POST", "/api/issues/pending")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Set readiness to blocked")).toBeInTheDocument();
    expect(callsTo(calls, "POST", "/api/issues/pending/apply")).toHaveLength(0);
  });

  it("a drop on a priority column stages the priority", async () => {
    const { staged, routes } = stagingRoutes();
    renderApp(routes);
    await openBoard("Priority");
    expect(within(column("priority: medium")).getByText("Fix login")).toBeInTheDocument();
    const user = await pickUp("Move acme/app#1 Fix login");
    await moveTo(user, "priority: low");
    await user.keyboard("[Space]");
    await waitFor(() => expect(staged).toHaveLength(1));
    expect(staged[0]?.change).toEqual({ kind: "set_dimension", dimension: "priority", value: "priority: low" });
  });

  it("a multi-selection drags together, and only issues that change are staged", async () => {
    const { staged, routes } = stagingRoutes([
      ROW_1,
      ROW_2,
      row(3, { title: "Already blocked", readiness: "blocked" }),
    ]);
    renderApp(routes);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select acme/app#2" })); // already agent-ready
    await userEvent.click(screen.getByRole("checkbox", { name: "Select acme/app#3" }));
    await openBoard();
    const user = await pickUp("Move acme/app#1 Fix login");
    expect(said()).toMatch(/^Picked up 3 issues/);
    await moveTo(user, "agent-ready");
    await user.keyboard("[Space]");
    await waitFor(() => expect(staged).toHaveLength(2));
    expect(staged.map((p) => p.n).sort()).toEqual([1, 3]);
  });

  it("refuses the none column, a value the issue already has, and a value its schema lacks", async () => {
    const other = row(4, { repo: "acme/other", title: "Elsewhere" });
    const { staged, routes } = stagingRoutes([ROW_1, ROW_2, other]);
    renderApp(routes);
    await openBoard();
    let user = await pickUp("Move acme/app#2 Add export");
    expect(await moveTo(user, "No readiness")).toMatch(/Pick a value/);
    await user.keyboard("[Escape]");
    user = await pickUp("Move acme/app#2 Add export");
    expect(await moveTo(user, "agent-ready")).toMatch(/Already agent-ready/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toMatch(/Nothing changed/));
    // acme/other isn't in Settings → Issues here, so it has no known schema values.
    user = await pickUp("Move acme/other#4 Elsewhere");
    expect(await moveTo(user, "blocked")).toMatch(/acme\/other's label schema has no blocked/);
    await user.keyboard("[Escape]");
    expect(staged).toHaveLength(0);
  });
});

describe("status columns", () => {
  it("are derived from GitHub and refuse drops with the reason", async () => {
    const { staged, routes } = stagingRoutes([
      ROW_1,
      ROW_2,
      row(5, { title: "Being built", complete: true, missing: [], readiness: "agent-ready", linked_pr_state: "open" }),
      row(6, { title: "Shipped", complete: true, missing: [], state: "closed" }),
    ]);
    renderApp(routes);
    await openBoard("Status");
    expect(within(column("Triage")).getByText("Fix login")).toBeInTheDocument();
    expect(within(column("Backlog")).getByText("Add export")).toBeInTheDocument();
    expect(within(column("In progress")).getByText("Being built")).toBeInTheDocument();
    expect(within(column("Done")).getByText("Shipped")).toBeInTheDocument();
    expect(column("In review")).toBeInTheDocument();
    const user = await pickUp("Move acme/app#1 Fix login");
    expect(await moveTo(user, "Backlog")).toMatch(/Status comes from GitHub/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(said()).toMatch(/Nothing changed/));
    expect(staged).toHaveLength(0);
  });
});
