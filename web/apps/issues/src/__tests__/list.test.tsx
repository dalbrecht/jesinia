// SPDX-License-Identifier: AGPL-3.0-or-later
// The workspace (spec §6): fixed Inbox, Backlog, Epics and Queues tabs plus one per opened
// issue; the compact list with multi-select and its bulk bar; the cache's freshness.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IDLE_POLL_MS, SYNCING_POLL_MS } from "../App";
import {
  callsTo,
  finished,
  issuesRoutes,
  json,
  renderApp,
  row,
  ROW_1,
  ROW_2,
  S_DUP,
  S_READY,
  suggestion,
  suggestions,
  tick,
  toast,
  view,
} from "./fixtures";

const tabs = () => screen.getByRole("navigation", { name: "Workspace" });

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.useRealTimers();
});

describe("workspace tabs", () => {
  it("has fixed Inbox, Backlog, Epics and Queues tabs; Backlog has its own route", async () => {
    renderApp(issuesRoutes());
    const nav = tabs();
    for (const name of ["Inbox", "Backlog", "Epics", "Queues"]) {
      expect(within(nav).getByRole("link", { name: (accessible) => accessible.startsWith(name) })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: /^Inbox/ })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("Fix login")).toBeInTheDocument();
    expect(screen.queryByText("Add export")).toBeNull();
    await userEvent.click(within(nav).getByRole("link", { name: /^Backlog/ }));
    expect(window.location.pathname).toBe("/issues/backlog");
    expect(await screen.findByText("Add export")).toBeInTheDocument();
    await userEvent.click(within(nav).getByRole("link", { name: /^Epics/ }));
    expect(screen.getByText(/Epics aren't in this version yet/)).toBeInTheDocument();
    await userEvent.click(within(nav).getByRole("link", { name: /^Queues/ }));
    expect(screen.getByText(/Queues aren't in this version yet/)).toBeInTheDocument();
  });

  it("opening an issue gives it a tab (kept across reloads); closing it goes back", async () => {
    renderApp(issuesRoutes());
    await userEvent.click(await screen.findByRole("button", { name: "Fix login" }));
    expect(window.location.pathname).toBe("/issues/acme/app/1");
    const tab = within(tabs()).getByRole("link", { name: /acme\/app#1 Fix login/ });
    expect(tab).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Fix login");
    expect(JSON.parse(localStorage.getItem("jesinia.issues.tabs") ?? "[]")).toHaveLength(1);
    await userEvent.click(within(tabs()).getByRole("link", { name: /^Inbox/ }));
    expect(screen.getByRole("button", { name: "Fix login" })).toBeVisible();
    await userEvent.click(within(tabs()).getByRole("button", { name: "Close acme/app#1" }));
    expect(within(tabs()).queryByRole("link", { name: /acme\/app#1/ })).toBeNull();
  });

  it("a linked issue route opens its tab directly; Back returns to the list", async () => {
    renderApp(issuesRoutes(), "/issues/acme/app/1");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Fix login");
    window.history.pushState(null, "", "/issues/backlog");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(await screen.findByText("Add export")).toBeInTheDocument();
  });
});

describe("the list", () => {
  it("shows each issue's ref, labels and what it is missing, with hidden characters visible", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([row(1, { title: `Sneaky${String.fromCodePoint(0x2066)} title` })]),
      }),
    );
    const item = (await screen.findByRole("button", { name: /Sneaky/ })).closest("li");
    if (!item) throw new Error("no row");
    expect(within(item).getByText("acme/app#1")).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: /Sneaky/ })).toHaveTextContent("Sneaky⟪U+2066⟫ title");
    expect(within(item).getByText("needs readiness")).toBeInTheDocument();
    expect(within(item).getByText("area:web")).toBeInTheDocument();
  });

  it("an empty Inbox says so", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/inbox": view([]) }));
    expect(await screen.findByText(/Nothing new to triage/)).toBeInTheDocument();
  });

  it("a saved filter and a repo narrow the Backlog", async () => {
    const { calls } = renderApp(issuesRoutes(), "/issues/backlog");
    await screen.findByText("Add export");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Filter" }), "incomplete");
    await waitFor(() =>
      expect(callsTo(calls, "GET", "/api/issues/backlog").some((c) => c.url.includes("filter=incomplete"))).toBe(true),
    );
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Repo" }), "acme/app");
    await waitFor(() =>
      expect(callsTo(calls, "GET", "/api/issues/backlog").some((c) => c.url.includes("repo=acme%2Fapp"))).toBe(true),
    );
  });

  it("multi-select shows the bulk bar: Suggest with Claude asks for the selected issues", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/backlog": view([ROW_1, ROW_2]),
        "POST /api/issues/suggestions/run": () => json({ runs: [] }, 202),
      }),
      "/issues/backlog",
    );
    await screen.findByText("Add export");
    expect(screen.queryByRole("toolbar", { name: "Selected issues" })).toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    const bar = screen.getByRole("toolbar", { name: "Selected issues" });
    expect(within(bar).getByText("2 selected")).toBeInTheDocument();
    await userEvent.click(within(bar).getByRole("button", { name: "Suggest with Claude" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/run")).toHaveLength(1));
    const [run] = callsTo(calls, "POST", "/api/issues/suggestions/run");
    expect(run?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await run?.clone().json()).toEqual({
      refs: [
        { repo: "acme/app", n: 1 },
        { repo: "acme/app", n: 2 },
      ],
    });
    expect(await toast(/Asked for suggestions on 2 issues/)).toBeTruthy();
    await userEvent.click(within(bar).getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("toolbar", { name: "Selected issues" })).toBeNull();
  });

  it("Approve all approves every open suggestion of the selected issues only", async () => {
    const other = suggestion("s3", { n: 2 });
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": suggestions([S_READY, S_DUP, other]),
        "POST /api/issues/suggestions/s1/approve": () => json({ suggestion: S_READY, batch: finished("b1") }, 202),
        "POST /api/issues/suggestions/s2/approve": () => json({ suggestion: S_DUP, batch: finished("b2") }, 202),
      }),
      "/issues/backlog",
    );
    await screen.findByText("Add export");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(
      within(screen.getByRole("toolbar", { name: "Selected issues" })).getByRole("button", { name: "Approve all" }),
    );
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/s2/approve")).toHaveLength(1));
    expect(callsTo(calls, "POST", "/api/issues/suggestions/s1/approve")).toHaveLength(1);
    expect(callsTo(calls, "POST", "/api/issues/suggestions/s3/approve")).toHaveLength(0);
    const read = callsTo(calls, "GET", "/api/issues/suggestions").at(-1);
    expect(new URL(read?.url ?? "").searchParams.get("repo")).toBe("acme/app");
  });

  it("Approve all with nothing to approve says so", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/suggestions": suggestions([]) }), "/issues/backlog");
    await screen.findByText("Add export");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select acme/app#2" }));
    await userEvent.click(
      within(screen.getByRole("toolbar", { name: "Selected issues" })).getByRole("button", { name: "Approve all" }),
    );
    expect(await toast(/No open suggestions to approve/)).toBeTruthy();
  });
});

describe("freshness", () => {
  it("re-reads often while syncing, then at the idle pace, showing how fresh it is", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-10-18T12:05:00Z"));
    let syncing = true;
    const { calls } = renderApp(issuesRoutes({ "GET /api/issues/inbox": () => json(view([ROW_1], { syncing })) }));
    expect(await screen.findByText("Updated 5 min ago · refreshing…")).toBeInTheDocument();
    const reads = () => callsTo(calls, "GET", "/api/issues/inbox").length;
    const first = reads();
    syncing = false;
    await tick(SYNCING_POLL_MS);
    await waitFor(() => expect(reads()).toBe(first + 1));
    expect(await screen.findByText("Updated 5 min ago")).toBeInTheDocument();
    await tick(SYNCING_POLL_MS * 3);
    expect(reads()).toBe(first + 1);
    await tick(IDLE_POLL_MS);
    expect(reads()).toBe(first + 2);
  });

  it("keeps the cached list under a banner when GitHub can't be read", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([ROW_1], { sync_error: "acme/app: GitHub auth missing; run `gh auth login`" }),
      }),
    );
    expect(await screen.findByText("Fix login")).toBeInTheDocument();
    expect(screen.getByText(/GitHub auth missing/)).toBeInTheDocument();
    expect(screen.getByText("gh auth login").tagName).toBe("CODE");
  });

  it("an unreadable list says why", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/inbox": () => json({ detail: "boom" }, 500) }));
    expect(await screen.findByText(/Couldn't read the Inbox/)).toBeInTheDocument();
  });
});
