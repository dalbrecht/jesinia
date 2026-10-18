// SPDX-License-Identifier: AGPL-3.0-or-later
// The Epics tab (spec §4, §6, rulings IR17, IR39, IR56–IR60): the list with progress and
// readiness, the issues without an epic, Move to epic… with the drop's refusals, and the agent's
// group proposals approved through the engine, with the creation guards' refusals as actions.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Pending } from "../api";
import { ASK_POLL_MS } from "../drafts";
import {
  batch,
  callsTo,
  epic,
  issuesRoutes,
  json,
  moveTo,
  pickUp,
  readiness,
  renderApp,
  ROW_1,
  ROW_2,
  row,
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
const menuFor = async (name: string) => {
  await userEvent.click(screen.getByRole("button", { name }));
  return screen.findByRole("menu", { name });
};

describe("the epic list", () => {
  it("shows progress, readiness, initiatives, a missing label and the won't-fit warning", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/epics": {
          epics: [
            epic(10, { title: "Onboarding", done: 2, total: 3 }),
            epic(11, {
              title: "Platform",
              is_initiative: true,
              readiness: readiness({ ready: true }),
              lacks_label: true,
            }),
            epic(12, {
              title: "Sync",
              readiness: readiness({ note: "it has no children yet", warning: "won't fit one PR" }),
            }),
          ],
        },
      }),
      "/issues/epics",
    );
    const list = await screen.findByRole("list", { name: "Epics" });
    const onboarding = within(list).getByRole("group", { name: "acme/app#10 Onboarding" });
    expect(onboarding).toHaveTextContent("2 of 3 done");
    expect(onboarding).toHaveTextContent("1 blocking");
    const platform = within(list).getByRole("group", { name: "acme/app#11 Platform" });
    expect(platform).toHaveTextContent("initiative");
    expect(platform).toHaveTextContent("Ready");
    expect(platform).toHaveTextContent("Has sub-issues but no epic label.");
    const sync = within(list).getByRole("group", { name: "acme/app#12 Sync" });
    expect(sync).toHaveTextContent("it has no children yet");
    expect(sync).toHaveTextContent("won't fit one PR");
  });

  it("says when there are no epics, filters by repo, and reports a failed read", async () => {
    let fail = false;
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/epics": () => (fail ? json({ error: "X", detail: "boom" }, 500) : json({ epics: [] })),
        "GET /api/issues/backlog": view([]),
        "GET /api/issues/inbox": view([]),
      }),
      "/issues/epics",
    );
    expect(await screen.findByText(/No open epics/)).toBeInTheDocument();
    expect(screen.getByText("Every open issue is in an epic.")).toBeInTheDocument();
    fail = true;
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Repo" }), "acme/app");
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/issues/epics?repo=acme%2Fapp"))).toBe(true));
    expect(await toast(/Couldn't read the epics: boom/)).toBe(true);
  });

  it("lists only open issues without an epic, opens one, and clears the selection", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [epic(10)] },
        "GET /api/issues/inbox": view([ROW_1, row(7, { title: "Has parent", parent: "acme/app#10" })]),
        "GET /api/issues/backlog": view([ROW_1, ROW_2, row(8, { is_epic: true }), row(9, { state: "closed" })]),
      }),
      "/issues/epics",
    );
    const pool = await screen.findByRole("list", { name: "Issues without an epic" });
    expect(within(pool).getAllByRole("listitem")).toHaveLength(2);
    expect(within(pool).getByText("agent-ready")).toBeInTheDocument();
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#1" }));
    expect(screen.getByRole("toolbar", { name: "Selected issues" })).toHaveTextContent("1 selected");
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#2" }));
    await userEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("toolbar", { name: "Selected issues" })).toBeNull();
    await userEvent.click(within(pool).getByRole("button", { name: "Fix login" }));
    await waitFor(() => expect(window.location.pathname).toBe("/issues/acme/app/1"));
  });
});

describe("Move to epic…", () => {
  it("stages one parent link per selected issue (source menu) and warns about another repo", async () => {
    const staged: Pending[] = [];
    const other = row(4, { repo: "acme/other", title: "Elsewhere" });
    const { calls } = renderApp(
      issuesRoutes({
        ...stagingRoutes(staged),
        "GET /api/issues/epics": { epics: [epic(10, { title: "Onboarding" })] },
        "GET /api/issues/backlog": view([ROW_1, other]),
      }),
      "/issues/epics",
    );
    const pool = await screen.findByRole("list", { name: "Issues without an epic" });
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(within(pool).getByRole("checkbox", { name: "Select acme/other#4" }));
    const menu = await menuFor("Move the selected issues to an epic");
    const item = await within(menu).findByRole("menuitem", { name: /acme\/app#10 Onboarding/ });
    expect(item).toHaveTextContent("won't fit one PR");
    await userEvent.click(item);
    await waitFor(() => expect(staged).toHaveLength(2));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/pending"))).toEqual([
      expect.objectContaining({ n: 1, source: "menu", change: expect.objectContaining({ parent: { repo: "acme/app", n: 10 } }) }), // prettier-ignore
      expect.objectContaining({ repo: "acme/other", n: 4, source: "menu" }),
    ]);
    expect(await toast(/Won't fit one PR: acme\/other#4 is in another repo than acme\/app#10/)).toBe(true);
  });

  it("refuses an initiative into an epic and another owner's issue, by drag and by menu", async () => {
    renderApp(
      issuesRoutes({
        ...stagingRoutes(),
        "GET /api/issues/epics": {
          epics: [epic(10, { title: "Onboarding" }), epic(20, { title: "Platform", is_initiative: true })],
        },
        "GET /api/issues/backlog": view([row(5, { repo: "other/x", title: "Foreign" })]),
      }),
      "/issues/epics",
    );
    await screen.findByRole("list", { name: "Epics" });
    const user = await pickUp("Move acme/app#20 Platform");
    expect(await moveTo(user, "acme/app#10 Onboarding")).toMatch(/An initiative sits above epics/);
    await user.keyboard("[Escape]");
    await settleDrag();
    const menu = await menuFor("Move acme/app#20 to an epic");
    expect(await within(menu).findByRole("menuitem", { name: /acme\/app#10 Onboarding/ })).toHaveTextContent(
      /An initiative sits above epics/,
    );
    expect(within(menu).getByRole("menuitem", { name: /acme\/app#20 Platform/ })).toHaveTextContent(
      /An epic can't contain itself/,
    );
    await userEvent.keyboard("{Escape}");
    const drag = await pickUp("Move other/x#5 Foreign");
    expect(await moveTo(drag, "acme/app#20 Platform")).toMatch(/same owner/);
  });

  it("says an epic that already holds the issue has nothing to do, and the menu's load can fail", async () => {
    let fail = false;
    renderApp(
      issuesRoutes({
        "GET /api/issues/epics": () =>
          fail ? json({ error: "X", detail: "down" }, 500) : json({ epics: [epic(10), epic(11, { parent: "acme/app#10" })] }), // prettier-ignore
      }),
      "/issues/epics",
    );
    await screen.findByRole("list", { name: "Epics" });
    const menu = await menuFor("Move acme/app#11 to an epic");
    expect(await within(menu).findByRole("menuitem", { name: /acme\/app#10/ })).toHaveTextContent(
      "Already in this epic",
    );
    await userEvent.keyboard("{Escape}");
    fail = true;
    const again = await menuFor("Move acme/app#11 to an epic");
    expect(await within(again).findByText(/Couldn't load the choices/)).toBeInTheDocument();
  });
});

describe("group proposals", () => {
  const GROUP = suggestion("g1", {
    repo: null,
    n: null,
    scope: "groups:*",
    kind: "group",
    payload: {
      repo: "acme/app",
      title: "Login polish",
      scope: "Make login smooth.",
      issues: [
        { repo: "acme/app", n: 1 },
        { repo: "acme/app", n: 2 },
      ],
      cross_repo: true,
    },
    reason: "Both touch login.",
  });

  it("asks Claude for groups, polls the ask, and shows the draft as a diff", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let done = false;
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [] },
        "POST /api/issues/epics/proposals": () =>
          json({ scope: "groups:*", status: "running", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null }, 202), // prettier-ignore
        "GET /api/issues/suggestions": () =>
          json(
            done
              ? suggestions([GROUP], { ask_run: { scope: "groups:*", status: "done", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: "g1" } }) // prettier-ignore
              : suggestions([], { ask_run: { scope: "groups:*", status: "running", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null } }), // prettier-ignore
          ),
      }),
      "/issues/epics",
    );
    expect(await screen.findByText(/No proposals/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Ask Claude for groups" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/epics/proposals"))).toEqual([{ repo: null }]);
    expect(await screen.findByText("Claude is working on it…")).toBeInTheDocument();
    done = true;
    await tick(ASK_POLL_MS);
    const card = await screen.findByRole("listitem", { name: "A new epic for 2 issues" });
    expect(card).toHaveTextContent("Create the epic Login polish");
    expect(card).toHaveTextContent("won't fit one PR");
    expect(card).toHaveTextContent("Make login smooth.");
    expect(card).toHaveTextContent("Link acme/app#1 under it");
    expect(card).toHaveTextContent("Both touch login.");
  });

  it("approves through the engine (followed in the tray); an existing epic offers to add the issues to it", async () => {
    const staged: Pending[] = [];
    let refuse = true;
    const { calls } = renderApp(
      issuesRoutes({
        ...stagingRoutes(staged),
        "GET /api/issues/epics": { epics: [epic(30, { title: "Existing" })] },
        "GET /api/issues/epics/acme/app/30": () => json({ error: "X", detail: "x" }, 500),
        "GET /api/issues/suggestions": suggestions([GROUP]),
        "POST /api/issues/suggestions/g1/approve": () =>
          refuse
            ? json({ error: "GroupEpicExistsError", detail: "an epic for those issues already exists; add the issues to it", epic: "acme/app#30" }, 409) // prettier-ignore
            : json({ suggestion: GROUP, batch: batch("b1", { origin: "suggestion" }) }, 202),
      }),
      "/issues/epics",
    );
    const card = await screen.findByRole("listitem", { name: "A new epic for 2 issues" });
    await userEvent.click(within(card).getByRole("button", { name: "Approve: A new epic for 2 issues" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(/An epic for these issues already exists/);
    await userEvent.click(within(card).getByRole("button", { name: "Add them to acme/app#30 instead" }));
    await waitFor(() => expect(staged).toHaveLength(2));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/pending"))).toEqual([
      expect.objectContaining({ n: 1, source: "menu", change: expect.objectContaining({ parent: { repo: "acme/app", n: 30 } }) }), // prettier-ignore
      expect.objectContaining({ n: 2 }),
    ]);
    refuse = false;
    await userEvent.click(within(card).getByRole("button", { name: "Approve: A new epic for 2 issues" }));
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(await within(tray).findByText("Approved: A new epic for 2 issues")).toBeInTheDocument();
  });

  it("an existing epic can be opened instead", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [] },
        "GET /api/issues/epics/acme/app/30": () => json({ error: "NotAnEpicError", detail: "gone" }, 404),
        "GET /api/issues/suggestions": suggestions([GROUP]),
        "POST /api/issues/suggestions/g1/approve": () =>
          json(
            { error: "GroupEpicExistsError", detail: "already exists; add the issues to it", epic: "acme/app#30" },
            409,
          ),
      }),
      "/issues/epics",
    );
    const card = await screen.findByRole("listitem", { name: "A new epic for 2 issues" });
    await userEvent.click(within(card).getByRole("button", { name: "Approve: A new epic for 2 issues" }));
    await userEvent.click(await within(card).findByRole("button", { name: "Open acme/app#30" }));
    expect(await screen.findByText(/Couldn't open acme\/app#30: gone/)).toBeInTheDocument();
  });

  it("an earlier unfinished batch refuses the approval with its Retry and Abandon in the tray", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [] },
        "GET /api/issues/pending/b7": batch("b7", {
          origin: "suggestion",
          status: "interrupted",
          repo_steps: [{ kind: "create_issue", state: "skipped", cause: "not run yet", conflict: false, repo: "acme/app", temp_id: "epic", label: "Login polish" }], // prettier-ignore
        }),
        "GET /api/issues/suggestions": suggestions([GROUP]),
        "POST /api/issues/suggestions/g1/dismiss": GROUP,
        "POST /api/issues/suggestions/g1/approve": () =>
          json({ error: "EarlierBatchError", detail: "retry batch b7, or abandon it", batch_id: "b7" }, 409),
      }),
      "/issues/epics",
    );
    const card = await screen.findByRole("listitem", { name: "A new epic for 2 issues" });
    await userEvent.click(within(card).getByRole("button", { name: "Approve: A new epic for 2 issues" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(/retry or abandon it in Pending changes\. retry batch b7/); // prettier-ignore
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(tray).getByRole("button", { name: "Abandon" })).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Dismiss: A new epic for 2 issues" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/g1/dismiss")).toHaveLength(1));
  });
});
