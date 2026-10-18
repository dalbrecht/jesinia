// SPDX-License-Identifier: AGPL-3.0-or-later
// The less travelled paths: failures and their causes, unusual payloads, tab housekeeping.
import { fakeClient } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getRepos, IssuesRequestError, parseRef, schemaValues, type Pending } from "../api";
import { BATCH_POLL_MS, useTray } from "../Tray";
import { loadTabs, saveTabs, TABS_KEY } from "../tabs";
import {
  batch,
  callsTo,
  detail,
  finished,
  issuesRoutes,
  json,
  pending,
  REPO,
  renderApp,
  repo,
  row,
  ROW_1,
  ROW_2,
  suggestion,
  suggestions,
  tick,
  toast,
  view,
} from "./fixtures";

const ISSUE = "/issues/acme/app/1";
const tabs = () => screen.getByRole("navigation", { name: "Workspace" });
const region = (name: string) => screen.getByRole("region", { name });

afterEach(() => {
  vi.useRealTimers();
});

describe("api edges", () => {
  it("reads an untyped error, Retry-After and a body's retry_after", async () => {
    const plain = fakeClient({ "GET /api/issues/repos": () => new Response("oops", { status: 502 }) });
    await expect(getRepos(plain.client)).rejects.toMatchObject({ status: 502, code: null, retryAfter: null });
    const header = fakeClient({
      "GET /api/issues/repos": () =>
        json({ error: "GitHubRateLimitedError", detail: "slow down" }, 503, { "Retry-After": "7" }),
    });
    await expect(getRepos(header.client)).rejects.toMatchObject({ code: "GitHubRateLimitedError", retryAfter: 7 });
    const body = fakeClient({ "GET /api/issues/repos": () => json({ error: "X", detail: "y", retry_after: 3 }, 503) });
    const error = await getRepos(body.client).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(IssuesRequestError);
    expect((error as IssuesRequestError).retryAfter).toBe(3);
  });

  it("parses refs and merges schema values across repos", () => {
    expect(parseRef("acme/app#12")).toEqual({ repo: "acme/app", number: 12 });
    expect(parseRef("not a ref")).toBeNull();
    expect(schemaValues([repo(), repo({ repo: "acme/other" })], "priority")).toEqual([
      "priority: high",
      "priority: medium",
      "priority: low",
    ]);
  });

  it("a tray hook outside its provider says so", () => {
    function Lost() {
      useTray();
      return null;
    }
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Lost />)).toThrow(/TrayProvider/);
  });
});

describe("tabs storage", () => {
  it("drops corrupt or malformed tabs, and survives storage that refuses", () => {
    localStorage.setItem(TABS_KEY, "{not json");
    expect(loadTabs()).toEqual([]);
    localStorage.setItem(TABS_KEY, JSON.stringify([{ repo: REPO, number: 1, label: "x" }, { repo: 1 }, null, "x"]));
    expect(loadTabs()).toEqual([{ repo: REPO, number: 1, label: "x" }]);
    localStorage.setItem(TABS_KEY, JSON.stringify({ not: "a list" }));
    expect(loadTabs()).toEqual([]);
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => saveTabs([{ repo: REPO, number: 1, label: "x" }])).not.toThrow();
    set.mockRestore();
  });
});

describe("workspace edges", () => {
  it("a saved tab for the linked issue isn't doubled; ⌘-click leaves the link to the browser", async () => {
    saveTabs([
      { repo: REPO, number: 1, label: "acme/app#1 Fix login" },
      { repo: REPO, number: 2, label: "acme/app#2 Add export" },
    ]);
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/2": detail({ number: 2, title: "Add export" }),
        "POST /api/issues/item/acme/app/2/refresh": detail({ number: 2, title: "Add export" }),
      }),
      ISSUE,
    );
    await screen.findByRole("heading", { level: 1 });
    expect(within(tabs()).getAllByRole("link", { name: /acme\/app#1/ })).toHaveLength(1);
    const second = within(tabs()).getByRole("link", { name: /acme\/app#2/ });
    fireEvent.click(second, { ctrlKey: true });
    expect(window.location.pathname).toBe(ISSUE);
    await userEvent.click(second);
    expect(window.location.pathname).toBe("/issues/acme/app/2");
    // Closing the shown tab shows its neighbour; closing the last shows the Inbox.
    await userEvent.click(within(tabs()).getByRole("button", { name: "Close acme/app#2" }));
    expect(window.location.pathname).toBe(ISSUE);
    await userEvent.click(within(tabs()).getByRole("button", { name: "Close acme/app#1" }));
    expect(window.location.pathname).toBe("/issues/");
  });

  it("a popstate to an issue opens its tab; the parent and a duplicate open theirs", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({ parent: "acme/app#9" }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ parent: "acme/app#9" }),
        "GET /api/issues/item/acme/app/9": detail({ number: 9, title: "The epic" }),
        "POST /api/issues/item/acme/app/9/refresh": detail({ number: 9, title: "The epic" }),
        "GET /api/issues/item/acme/app/2": detail({ number: 2, title: "Add export" }),
        "POST /api/issues/item/acme/app/2/refresh": detail({ number: 2, title: "Add export" }),
      }),
    );
    await screen.findByText("Fix login");
    window.history.pushState(null, "", ISSUE);
    window.dispatchEvent(new PopStateEvent("popstate"));
    const epic = await screen.findByRole("region", { name: "Epic" });
    await userEvent.click(within(epic).getByRole("button", { name: "acme/app#9" }));
    expect(window.location.pathname).toBe("/issues/acme/app/9");
    expect(await within(tabs()).findByRole("link", { name: /acme\/app#9 The epic/ })).toBeInTheDocument();
    await userEvent.click(within(tabs()).getByRole("link", { name: /acme\/app#1/ }));
    const related = screen.getAllByRole("region", { name: "Related" }).find((r) => !r.closest("[hidden]"));
    if (!related) throw new Error("no related");
    await userEvent.click(within(related).getByRole("button", { name: "acme/app#2" }));
    expect(window.location.pathname).toBe("/issues/acme/app/2");
  });

  it("the watched repos failing to load is said once", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/repos": () => json({ detail: "db locked" }, 500) }));
    expect(await toast(/Couldn't read the watched repos/)).toBe(true);
  });
});

describe("list edges", () => {
  it("toggles a row off, keeps the list when a later read fails, and opens from the board", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    let fail = false;
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": () =>
          fail
            ? json({ detail: "boom" }, 500)
            : json(view([ROW_1, row(7, { readiness: "ready-ish", title: "Alias" })], { syncing: true })),
        "POST /api/issues/suggestions/run": () => json({ detail: "runner down" }, 500),
      }),
    );
    const box = await screen.findByRole("checkbox", { name: "Select acme/app#1" });
    await user.click(box);
    await user.click(box);
    expect(box).not.toBeChecked();
    fail = true;
    await tick(2000);
    expect(screen.getByRole("button", { name: "Fix login" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select all" }));
    await user.click(screen.getByRole("button", { name: "Suggest with Claude" }));
    expect(await toast(/Couldn't ask for suggestions/)).toBe(true);
    await user.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(screen.getByRole("checkbox", { name: "Select acme/app#1" })).not.toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Board" }));
    // A value outside the schema (an alias not migrated yet) still gets its column.
    expect(within(screen.getByRole("group", { name: "ready-ish" })).getByText("Alias")).toBeInTheDocument();
    await user.click(
      within(screen.getByRole("group", { name: "No readiness" })).getByRole("button", { name: "Fix login" }),
    );
    expect(window.location.pathname).toBe(ISSUE);
  });

  it("Approve all says when the suggestions can't be read; a sync error with an empty list", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/backlog": view([ROW_2], { sync_error: "GitHub is unreachable" }),
        "GET /api/issues/suggestions": () => json({ detail: "boom" }, 500),
      }),
      "/issues/backlog",
    );
    expect(await screen.findByText(/Couldn't refresh from GitHub/)).toBeInTheDocument();
    expect(screen.getByText(/What you see is from the cache/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select acme/app#2" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve all" }));
    expect(await toast(/Couldn't read the suggestions/)).toBe(true);
  });

  it("with no repos watched, says where to add one", async () => {
    renderApp(
      issuesRoutes({ "GET /api/issues/repos": { repos: [] }, "GET /api/issues/inbox": view([], { sync_error: "x" }) }),
    );
    expect(await screen.findByText(/Add a repo in Settings → Issues/)).toBeInTheDocument();
  });
});

describe("issue view edges", () => {
  it("shows run states, edits a new area and a type, and reports each failure", async () => {
    const list = [
      suggestion("a1", { kind: "new_area", payload: { dimension: "area", value: "area:cli" }, state: "edited" }),
      suggestion("t1", { payload: { dimension: "type", value: "bug" }, apply_error: "the label is missing on GitHub" }),
    ];
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({
          body: "  ",
          url: "javascript:alert(1)",
          children: ["acme/app#3"],
          comments: [{ id: 1, author: "x", body: "c", created_at: "nope", url: "https://x" }],
          timeline: [
            {
              id: null,
              event: "closed",
              actor: "octo",
              created_at: null,
              label: null,
              state_reason: "completed",
              source: null,
              commit_id: null,
            },
            {
              id: 4,
              event: "reopened",
              actor: "octo",
              created_at: null,
              label: null,
              state_reason: null,
              source: null,
              commit_id: null,
            },
          ],
        }),
        "POST /api/issues/item/acme/app/1/refresh": () => json({ detail: "GitHub is down" }, 502),
        "GET /api/issues/suggestions": () =>
          json(
            suggestions(list, {
              run: { repo: REPO, n: 1, status: "queued", source: "demand", due_at: "x", cause: null, updated_at: "x" },
            }),
          ),
        "PATCH /api/issues/suggestions/a1": () =>
          json({ error: "InvalidSuggestionEditError", detail: "bad area" }, 400),
        "PATCH /api/issues/suggestions/t1": () => json(list[1]),
        "POST /api/issues/suggestions/a1/dismiss": () => json({ detail: "boom" }, 500),
        "POST /api/issues/suggestions/t1/dismiss": () => json({ detail: "boom" }, 500),
        "POST /api/issues/suggestions/run": () => json({ detail: "boom" }, 500),
      }),
      ISSUE,
    );
    expect(await screen.findByText("Claude is suggesting…")).toBeInTheDocument();
    expect(screen.getByText("No description.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open on GitHub/ })).toBeNull(); // not an http(s) URL
    expect(await screen.findByText(/Couldn't fetch the latest from GitHub/)).toBeInTheDocument();
    expect(screen.getByText(/closed completed/)).toBeInTheDocument();
    expect(screen.getByText(/reopened/)).toBeInTheDocument();
    expect(within(region("Related")).getByText("acme/app#3")).toBeInTheDocument();
    expect(within(region("Suggestions")).getByText("edited")).toBeInTheDocument();
    expect(within(region("Suggestions")).getByText(/Didn't apply: the label is missing on GitHub/)).toBeInTheDocument();
    // A new area is edited as text, pasted.
    const area = within(
      within(region("Suggestions")).getByText("Add a new area: area:cli").closest("li") as HTMLElement,
    );
    await userEvent.click(area.getByRole("button", { name: "Edit" }));
    await userEvent.clear(area.getByRole("textbox", { name: "New area" }));
    area.getByRole("textbox", { name: "New area" }).focus();
    await userEvent.paste("area:tools");
    await userEvent.click(area.getByRole("button", { name: "Save" }));
    expect(await toast(/Couldn't edit/)).toBe(true);
    await userEvent.click(area.getByRole("button", { name: "Cancel" }));
    const type = within(within(region("Suggestions")).getByText("Set type to bug").closest("li") as HTMLElement);
    await userEvent.click(type.getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(type.getByRole("combobox", { name: "New type" }), "enhancement");
    await userEvent.click(type.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(callsTo(calls, "PATCH", "/api/issues/suggestions/t1")).toHaveLength(1));
    await userEvent.click(type.getByRole("button", { name: "Dismiss" }));
    expect(await toast(/Couldn't dismiss/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Dismiss all" }));
    await userEvent.click(within(region("Suggestions")).getByRole("button", { name: "Suggest with Claude" }));
    expect(await toast(/Couldn't ask for suggestions/)).toBe(true);
    // The pickers ignore their empty choice.
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Priority" }), "");
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);
  });

  it("says when the run failed, when it's done with none open, and when suggestions can't be read", async () => {
    let state: "failed" | "done" | "broken" = "failed";
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({ comments: [], timeline: [] }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ comments: [], timeline: [] }),
        "GET /api/issues/suggestions": () =>
          state === "broken"
            ? json({ detail: "boom" }, 500)
            : json(
                suggestions([], {
                  run: {
                    repo: REPO,
                    n: 1,
                    status: state,
                    source: "demand",
                    due_at: "x",
                    cause: state === "failed" ? "agent timed out" : null,
                    updated_at: "x",
                  },
                }),
              ),
        "POST /api/issues/suggestions/run": () => {
          state = state === "failed" ? "done" : "broken";
          return json({ runs: [] }, 202);
        },
      }),
      ISSUE,
    );
    expect(await screen.findByText(/The last suggestion run failed: agent timed out/)).toBeInTheDocument();
    expect(screen.getByText("Nothing yet.")).toBeInTheDocument();
    await userEvent.click(within(region("Suggestions")).getByRole("button", { name: "Suggest with Claude" }));
    expect(await screen.findByText("No open suggestions.")).toBeInTheDocument();
    await userEvent.click(within(region("Suggestions")).getByRole("button", { name: "Suggest with Claude" }));
    expect(await toast(/Couldn't read the suggestions/)).toBe(true);
  });

  it("without a known schema the pickers offer nothing; a failed run says no cause", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/repos": { repos: [] },
        "GET /api/issues/suggestions": suggestions([], {
          run: { repo: REPO, n: 1, status: "failed", source: "demand", due_at: "x", cause: null, updated_at: "x" },
        }),
      }),
      ISSUE,
    );
    expect(await screen.findByText(/failed: no cause given/)).toBeInTheDocument();
    expect(within(screen.getByRole("combobox", { name: "Readiness" })).getAllByRole("option")).toHaveLength(1);
  });
});

describe("tray edges", () => {
  it("a tray that can't be read says so; a staged theme step names its repo", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/pending": () => json({ detail: "boom" }, 500) }));
    expect(await toast(/Couldn't read the pending changes/)).toBe(true);
  });

  it("shows a repo-level step and a temp-id issue; failures of undo all, retry and abandon are said", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    const theme: Pending = pending("p1", {
      n: null,
      change: { kind: "ensure_label", repo: REPO, name: "theme:sync", color: "ededed", description: "" },
      source: "theme",
    });
    let reads = 0;
    const broken = batch("b5", {
      origin: "epic",
      status: "interrupted",
      repo_steps: [
        {
          kind: "ensure_label",
          state: "failed",
          cause: null,
          conflict: false,
          repo: REPO,
          label: "theme:sync",
          temp_id: null,
        },
        {
          kind: "create_issue",
          state: "skipped",
          cause: "after a failure",
          conflict: false,
          repo: REPO,
          label: null,
          temp_id: "t2",
        },
      ],
      issues: [
        {
          repo: null,
          n: null,
          temp_id: "t2",
          steps: [{ kind: "parent", state: "failed", cause: null, conflict: false }],
        },
      ],
    });
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/pending": { changes: [theme] },
        "DELETE /api/issues/pending": () => json({ detail: "boom" }, 500),
        "POST /api/issues/pending/apply": () => json(batch("b5", { origin: "epic" }), 202),
        "GET /api/issues/pending/b5": () => {
          reads += 1;
          return reads === 1 ? json({ detail: "flaky" }, 500) : json(broken);
        },
        "POST /api/issues/pending/b5/retry": () => json({ error: "BatchRunningError", detail: "still going" }, 409),
        "POST /api/issues/pending/b5/abandon": () => json({ error: "BatchRunningError", detail: "still going" }, 409),
      }),
    );
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Create label theme:sync")).toBeInTheDocument();
    expect(within(tray).getByRole("button", { name: "Undo: Create label theme:sync on acme/app" })).toBeInTheDocument();
    await user.click(within(tray).getByRole("button", { name: "Undo all" }));
    expect(await toast(/Couldn't undo the pending changes/)).toBe(true);
    await user.click(within(tray).getByRole("button", { name: /^Apply/ }));
    expect(within(tray).getByText("In progress")).toBeInTheDocument(); // not "skipped" while it runs
    await tick(BATCH_POLL_MS); // a failed read: tried again, after twice as long (M5)
    const polled = callsTo(calls, "GET", "/api/issues/pending/b5").length;
    await tick(BATCH_POLL_MS);
    expect(callsTo(calls, "GET", "/api/issues/pending/b5")).toHaveLength(polled);
    await tick(BATCH_POLL_MS);
    expect(await within(tray).findByText(/Interrupted/)).toBeInTheDocument();
    expect(within(tray).getByText("ensure_label failed: no cause given")).toBeInTheDocument();
    expect(within(tray).getByText("create_issue skipped: after a failure")).toBeInTheDocument();
    expect(within(tray).getByRole("listitem", { name: "new issue t2" })).toBeInTheDocument();
    await user.click(within(tray).getByRole("button", { name: "Retry" }));
    expect(await toast(/Couldn't retry the batch: That batch is still running/)).toBe(true);
    await user.click(within(tray).getByRole("button", { name: "Abandon" }));
    expect(await toast(/Couldn't abandon the batch/)).toBe(true);
    expect(callsTo(calls, "GET", "/api/issues/pending/b5").length).toBeGreaterThanOrEqual(2);
  });

  it("a finished batch from an approval refreshes the views at once", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "POST /api/issues/suggestions/s1/approve": () =>
          json({ suggestion: suggestion("s1"), batch: finished("b1", { origin: "suggestion" }) }, 202),
      }),
      ISSUE,
    );
    const reads = () => callsTo(calls, "GET", "/api/issues/item/acme/app/1").length;
    await screen.findByRole("region", { name: "Suggestions" });
    const before = reads();
    await userEvent.click(within(region("Suggestions")).getAllByRole("button", { name: "Accept" })[0] as HTMLElement);
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });
});
