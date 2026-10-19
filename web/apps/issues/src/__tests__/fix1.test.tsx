// SPDX-License-Identifier: AGPL-3.0-or-later
// Task 12 review, fix round 1: line endings in untrusted Markdown (C1), focus (I1), answers
// arriving out of order (I2), unfinished batches after a reload (I3), Approve all outcomes (I4),
// mixed-case repos (I5), per-tab section ids (I6), and the minor findings.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { errorCause, IssuesRequestError, shortValue, type Pending } from "../api";
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
  S_DUP,
  S_READY,
  suggestion,
  suggestions,
  toast,
  view,
} from "./fixtures";

const ISSUE = "/issues/acme/app/1";
const tabs = () => screen.getByRole("navigation", { name: "Workspace" });

/** A response held until `release` is called. */
function held<T>(body: T) {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { route: async () => (await gate, json(body)), release: () => release() };
}

describe("C1: line endings are text, hidden characters still show", () => {
  it("keeps a CRLF soft break and a code block's lines, and tokens a real bidi control", async () => {
    const body = `First line\r\nsecond line ${String.fromCodePoint(0x202e)}flip\n\n\`\`\`\nalpha\nbeta\n\`\`\``;
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({ body }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ body }),
      }),
      ISSUE,
    );
    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(summary.textContent).not.toContain("U+000A");
    expect(summary.textContent).not.toContain("U+000D");
    expect(summary.textContent).toContain("second line ⟪U+202E⟫flip");
    expect(summary.querySelector("code")?.textContent).toBe("alpha\nbeta\n");
  });
});

describe("I1: focus is never dropped", () => {
  it("opening an issue focuses its tab; closing focuses the neighbour, then the Inbox tab", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([ROW_1, row(2, { title: "Second" })]),
        "GET /api/issues/item/acme/app/2": detail({ number: 2, title: "Second" }),
        "POST /api/issues/item/acme/app/2/refresh": detail({ number: 2, title: "Second" }),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Fix login" }));
    await waitFor(() => expect(document.activeElement).toBe(within(tabs()).getByRole("link", { name: /acme\/app#1/ })));
    await userEvent.click(within(tabs()).getByRole("link", { name: /^Inbox/ }));
    await userEvent.click(screen.getByRole("button", { name: "Second" }));
    await waitFor(() => expect(document.activeElement).toBe(within(tabs()).getByRole("link", { name: /acme\/app#2/ })));
    await userEvent.click(within(tabs()).getByRole("button", { name: "Close acme/app#2" }));
    await waitFor(() => expect(document.activeElement).toBe(within(tabs()).getByRole("link", { name: /acme\/app#1/ })));
    await userEvent.click(within(tabs()).getByRole("button", { name: "Close acme/app#1" }));
    await waitFor(() => expect(document.activeElement).toBe(within(tabs()).getByRole("link", { name: /^Inbox/ })));
  });

  it("undoing the last change hands focus back to the shown tab", async () => {
    let changes: Pending[] = [pending("p1")];
    renderApp(
      issuesRoutes({
        "GET /api/issues/pending": () => json({ changes }),
        "DELETE /api/issues/pending/p1": () => {
          changes = [];
          return json(null, 204);
        },
      }),
    );
    const undo = await screen.findByRole("button", { name: /^Undo: / });
    await userEvent.click(undo);
    await waitFor(() => expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(within(tabs()).getByRole("link", { name: /^Inbox/ })));
  });

  it("dismissing a suggestion focuses the next one; the last leaves focus on the heading", async () => {
    let open = [S_READY, S_DUP];
    renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": () => json(suggestions(open)),
        "POST /api/issues/suggestions/s1/dismiss": () => {
          open = [S_DUP];
          return json({ ...S_READY, state: "dismissed" });
        },
        "POST /api/issues/suggestions/s2/dismiss": () => {
          open = [];
          return json({ ...S_DUP, state: "dismissed" });
        },
      }),
      ISSUE,
    );
    const region = await screen.findByRole("region", { name: "Suggestions" });
    await userEvent.click(within(region).getAllByRole("button", { name: "Dismiss" })[0] as HTMLElement);
    await waitFor(() => expect(document.activeElement).toBe(within(region).getByRole("button", { name: "Accept" })));
    await userEvent.click(within(region).getByRole("button", { name: "Dismiss" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(within(region).getByRole("heading", { name: "Suggestions" })),
    );
  });
});

describe("I2: an older answer never replaces a newer one", () => {
  it("the tray keeps a staged change when the mount read resolves last", async () => {
    const slow = held({ changes: [] });
    let first = true;
    let changes: Pending[] = [];
    renderApp(
      issuesRoutes({
        "GET /api/issues/pending": () => {
          if (first) {
            first = false;
            return slow.route();
          }
          return json({ changes });
        },
        "POST /api/issues/pending": () => {
          changes = [pending("p1")];
          return json(changes[0], 201);
        },
      }),
      ISSUE,
    );
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Priority" }), "priority: high");
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    slow.release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(tray).getByText("Set priority to high")).toBeInTheDocument();
  });

  it("the list keeps the newer filter's rows when an older read lands last", async () => {
    const slow = held(view([row(1, { title: "Stale row" })]));
    renderApp(
      issuesRoutes({
        "GET /api/issues/backlog": (req: Request) =>
          new URL(req.url).searchParams.get("filter") ? json(view([row(2, { title: "Fresh row" })])) : slow.route(),
      }),
      "/issues/backlog",
    );
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Filter" }), "incomplete");
    expect(await screen.findByText("Fresh row")).toBeInTheDocument();
    slow.release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("Stale row")).toBeNull();
  });

  it("a slow cache read never replaces the refreshed comments", async () => {
    const slow = held(detail({ comments: [] }));
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": () => slow.route(),
        "POST /api/issues/item/acme/app/1/refresh": detail(),
      }),
      ISSUE,
    );
    expect(await screen.findByText("Seen it too.")).toBeInTheDocument();
    slow.release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("Seen it too.")).toBeInTheDocument();
  });
});

describe("I3: unfinished batches are reachable after a reload", () => {
  it("shows the server's unfinished batches with Retry", async () => {
    const stuck = finished("b7", {
      complete: false,
      issues: [
        {
          repo: REPO,
          n: 1,
          temp_id: null,
          steps: [{ kind: "labels", state: "failed", cause: "boom", conflict: false }],
        },
      ],
    });
    renderApp(issuesRoutes({ "GET /api/issues/batches": { batches: [stuck] } }));
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Unfinished: staged changes")).toBeInTheDocument();
    expect(within(tray).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("an EarlierBatchError's batch is followed, so Retry is where the copy says", async () => {
    const earlier = batch("b8", { origin: "epic", status: "interrupted" });
    renderApp(
      issuesRoutes({
        "POST /api/issues/suggestions/s1/approve": () =>
          json({ error: "EarlierBatchError", detail: "retry batch b8", batch_id: "b8" }, 409),
        "GET /api/issues/pending/b8": earlier,
      }),
      ISSUE,
    );
    const region = await screen.findByRole("region", { name: "Suggestions" });
    await userEvent.click(within(region).getAllByRole("button", { name: "Accept" })[0] as HTMLElement);
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(await within(tray).findByText("Earlier batch: retry or abandon it first")).toBeInTheDocument();
    expect(within(tray).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("I4: Approve all reports every outcome", () => {
  it("names an issue whose suggestions couldn't be read, and counts what failed", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/backlog": view([ROW_1, row(2, { title: "Other" })]),
        "GET /api/issues/suggestions": (req: Request) =>
          new URL(req.url).searchParams.get("n") === "2"
            ? json({ detail: "boom" }, 500)
            : json(suggestions([S_READY, S_DUP])),
        "POST /api/issues/suggestions/s1/approve": () => json({ suggestion: S_READY, batch: finished("b1") }, 202),
        "POST /api/issues/suggestions/s2/approve": () => json({ error: "SuggestionStaleError", detail: "gone" }, 409),
      }),
      "/issues/backlog",
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select all" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve all" }));
    expect(await toast(/Approved 1 of 2 suggestions/)).toBe(true);
    expect(await toast(/1 couldn't be approved/)).toBe(true);
    expect(await toast(/Suggestions of acme\/app#2 couldn't be read/)).toBe(true);
  });
});

describe("I5 and I6: repo case and section ids", () => {
  it("a mixed-case route for an open issue doesn't open a second tab", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([row(1, { repo: "Acme/App", title: "Fix login" })]),
        "GET /api/issues/item/Acme/App/1": detail({ repo: "Acme/App" }),
        "POST /api/issues/item/Acme/App/1/refresh": detail({ repo: "Acme/App" }),
        "GET /api/issues/item/acme/app/1": detail({ repo: "Acme/App" }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ repo: "Acme/App" }),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Fix login" }));
    window.history.pushState(null, "", "/issues/acme/app/1");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await new Promise((resolve) => setTimeout(resolve, 20)); // let the popstate render
    expect(within(tabs()).getAllByRole("link", { name: /#1/i })).toHaveLength(1);
    expect(screen.getAllByRole("heading", { level: 1, hidden: true })).toHaveLength(1);
  });

  it("each open issue's outline points into its own pane", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/repos": { repos: [repo(), repo({ repo: "acme/web" })] },
        "GET /api/issues/item/acme/web/1": detail({ repo: "acme/web", title: "Web one" }),
        "POST /api/issues/item/acme/web/1/refresh": detail({ repo: "acme/web", title: "Web one" }),
      }),
      ISSUE,
    );
    await screen.findByRole("heading", { level: 1 });
    window.history.pushState(null, "", "/issues/acme/web/1");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await screen.findByText("Web one");
    const outlines = screen.getAllByRole("navigation", { name: "Outline", hidden: true });
    expect(outlines).toHaveLength(2);
    for (const outline of outlines) {
      const pane = outline.parentElement as HTMLElement;
      for (const link of within(outline).getAllByRole("link", { hidden: true })) {
        const id = (link.getAttribute("href") ?? "").slice(1);
        expect(pane.querySelector(`[id="${id}"]`), id).not.toBeNull();
      }
    }
    const ids = [...document.querySelectorAll("[id]")].map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("minor findings", () => {
  it("M1: staging is announced; an empty picker choice can't be picked", async () => {
    renderApp(issuesRoutes({ "POST /api/issues/pending": () => json(pending("p1"), 201) }), ISSUE);
    const picker = await screen.findByRole("combobox", { name: "Priority" });
    expect(within(picker).getByRole("option", { name: "No priority" })).toBeDisabled();
    await userEvent.selectOptions(picker, "priority: high");
    expect(await toast(/Staged: Set priority to high/)).toBe(true);
  });

  it("M3 and M15: unsafe agent refs aren't links; sub-issues open; column titles show hidden characters", async () => {
    const bad = suggestion("s9", { kind: "duplicate", payload: { repo: "javascript:alert(1)", n: 2, similarity: 1 } });
    renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": suggestions([bad]),
        "GET /api/issues/item/acme/app/1": detail({ children: ["acme/app#5"] }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ children: ["acme/app#5"] }),
        "GET /api/issues/item/acme/app/5": detail({ number: 5, title: "Child" }),
        "POST /api/issues/item/acme/app/5/refresh": detail({ number: 5, title: "Child" }),
      }),
      ISSUE,
    );
    const related = await screen.findByRole("region", { name: "Related" });
    expect(within(related).queryByText(/javascript/)).toBeNull();
    await userEvent.click(within(related).getByRole("button", { name: "acme/app#5" }));
    expect(window.location.pathname).toBe("/issues/acme/app/5");
  });

  it("M3: a schema value with a hidden character shows it in its column title", async () => {
    const odd = `blocked${String.fromCodePoint(0x200b)}`;
    const schema = repo();
    schema.label_schema.readiness = [{ name: odd, aliases: [] }];
    renderApp(issuesRoutes({ "GET /api/issues/repos": { repos: [schema] } }));
    await userEvent.click(await screen.findByRole("radio", { name: "Board" }));
    expect(screen.getByRole("group", { name: "blocked⟪U+200B⟫" })).toBeInTheDocument();
  });

  it("M4: Accept shows it's busy while the approval is sent", async () => {
    const slow = held({ suggestion: S_READY, batch: finished("b1") });
    renderApp(issuesRoutes({ "POST /api/issues/suggestions/s1/approve": () => slow.route() }), ISSUE);
    const region = await screen.findByRole("region", { name: "Suggestions" });
    const accept = within(region).getAllByRole("button", { name: "Accept" })[0] as HTMLElement;
    await userEvent.click(accept);
    expect(accept).toHaveAttribute("aria-busy", "true");
    slow.release();
    await waitFor(() => expect(accept).not.toHaveAttribute("aria-busy"));
  });

  it("M6: ⌘↵ typed in a text field doesn't apply the tray", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/pending": { changes: [pending("p1")] },
        "GET /api/issues/suggestions": suggestions([
          suggestion("a1", { kind: "new_area", payload: { dimension: "area", value: "area:cli" } }),
        ]),
      }),
      ISSUE,
    );
    const region = await screen.findByRole("region", { name: "Suggestions" });
    await userEvent.click(within(region).getByRole("button", { name: "Edit" }));
    within(region).getByRole("textbox", { name: "New area" }).focus();
    await userEvent.keyboard("{Meta>}{Enter}{/Meta}");
    expect(callsTo(calls, "POST", "/api/issues/pending/apply")).toHaveLength(0);
  });

  it("M8: a rate-limited batch says when GitHub resets", async () => {
    const limited = finished("b3", {
      complete: false,
      stopped: "GitHub's rate limit",
      rate_limited: true,
      rate_limited_until: "2026-10-18T12:30:00Z",
    });
    renderApp(issuesRoutes({ "GET /api/issues/batches": { batches: [limited] } }));
    expect(await screen.findByText(/GitHub's rate limit resets at/)).toBeInTheDocument();
  });

  it("M12 and M16: the earlier-batch and running copy, and short values", () => {
    expect(errorCause(new IssuesRequestError("x", 409, "BatchRunningError"))).toMatch(/still running/);
    expect(errorCause(new IssuesRequestError("x", 409, "EarlierBatchError", null, "b1"))).toMatch(/retry or abandon/);
    expect(shortValue("priority", "priority: high")).toBe("high");
    expect(shortValue("readiness", "blocked")).toBe("blocked");
    expect(shortValue("priority", "priority:")).toBe("priority:");
  });

  it("M7: a step not reached while the batch runs reads in progress, not skipped", async () => {
    renderApp(issuesRoutes({ "GET /api/issues/batches": { batches: [batch("b4")] } }));
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(
      within(within(tray).getByRole("listitem", { name: "acme/app#1" })).getByText("In progress"),
    ).toBeInTheDocument();
  });
});
