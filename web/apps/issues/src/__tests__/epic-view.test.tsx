// SPDX-License-Identifier: AGPL-3.0-or-later
// An epic's page (spec §4, §6, rulings IR8, IR39, IR48, IR56): its children in order (a reorder,
// a drop and Remove stage changes), the brief editor (posted through the brief endpoints; a brief
// edited on GitHub asks to refresh or overwrite), Copy brief, the checklist conversion as a
// draft diff, and the epic's proposals.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EpicDetail, Pending } from "../api";
import { ASK_POLL_MS } from "../drafts";
import {
  batch,
  callsTo,
  epic,
  epicDetail,
  issuesRoutes,
  json,
  member,
  moveTo,
  pickUp,
  queue,
  queueSummary,
  readiness,
  renderApp,
  settleDrag,
  stagingRoutes,
  suggestion,
  suggestions,
  tick,
  toast,
} from "./fixtures";

afterEach(async () => {
  vi.useRealTimers();
  await settleDrag();
});

const bodies = async (calls: Request[]) => Promise.all(calls.map((c) => c.clone().json() as Promise<unknown>));
const EPIC = "/api/issues/epics/acme/app/10";

function epicRoutes(detail: EpicDetail = epicDetail(10, { epic: epic(10, { title: "Onboarding" }) }), over = {}) {
  const staged: Pending[] = [];
  return {
    staged,
    routes: issuesRoutes({
      ...stagingRoutes(staged),
      "GET /api/issues/epics": { epics: [epic(10, { title: "Onboarding" }), epic(20, { title: "Other" })] },
      [`GET ${EPIC}`]: () => json(detail),
      ...over,
    }),
  };
}

async function openEpic() {
  await userEvent.click(await screen.findByRole("button", { name: "Onboarding" }));
  return screen.findByRole("region", { name: "Children" });
}

describe("the children", () => {
  it("lists the children in order, in the outline too, with what blocks each", async () => {
    const detail = epicDetail(10, {
      epic: epic(10, { title: "Onboarding" }),
      children: [
        member(3, { title: "First" }),
        member(4, { title: "Second", readiness: null, blocker: "needs readiness", warning: "won't fit one PR" }),
        member(5, { cached: false, title: null, state: null, readiness: null }),
        member(6, { title: "Done one", state: "closed" }),
      ],
    });
    renderApp(epicRoutes(detail).routes, "/issues/epics");
    const children = await openEpic();
    const rows = within(children).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("First"),
      expect.stringContaining("Blocks: needs readiness"),
      expect.stringContaining("Not cached"),
      expect.stringContaining("closed"),
    ]);
    expect(rows[1]).toHaveTextContent("won't fit one PR");
    const outline = screen.getByRole("navigation", { name: "Outline" });
    expect(
      within(outline)
        .getAllByRole("link")
        .map((a) => a.textContent),
    ).toEqual(["acme/app#3 First", "acme/app#4 Second", "acme/app#5", "acme/app#6 Done one", "Brief", "Proposals"]);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Onboarding");
    expect(within(screen.getByRole("article")).getByText(/1 of 3 done/)).toBeInTheDocument();
    await userEvent.click(within(children).getByRole("button", { name: "First" }));
    await waitFor(() => expect(window.location.pathname).toBe("/issues/acme/app/3"));
  });

  it("a keyboard reorder stages the sub-issue order, and Move up and Move down are its menu equivalents", async () => {
    const { routes } = epicRoutes();
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    const user = await pickUp("Move acme/app#4 Child 4");
    await moveTo(user, /^Children of acme\/app#10, position 1 of/);
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(1));
    await userEvent.click(screen.getByRole("button", { name: "Move acme/app#3 down" }));
    await userEvent.click(screen.getByRole("button", { name: "Move acme/app#4 up" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(3));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/pending"))).toEqual([
      { repo: "acme/app", n: 10, source: "drag", change: { kind: "reorder_child", child: { repo: "acme/app", n: 4 }, after: null } }, // prettier-ignore
      { repo: "acme/app", n: 10, source: "drag", change: { kind: "reorder_child", child: { repo: "acme/app", n: 3 }, after: { repo: "acme/app", n: 4 } } }, // prettier-ignore
      { repo: "acme/app", n: 10, source: "drag", change: { kind: "reorder_child", child: { repo: "acme/app", n: 4 }, after: null } }, // prettier-ignore
    ]);
  });

  it("Remove (menu or the Remove zone) stages taking a child out, against this epic", async () => {
    const { routes } = epicRoutes();
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    await userEvent.click(screen.getByRole("button", { name: "Remove acme/app#3 from this epic" }));
    const user = await pickUp("Move acme/app#4 Child 4");
    await moveTo(user, "Remove from acme/app#10");
    await user.keyboard("[Space]");
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(2));
    const sent = await bodies(callsTo(calls, "POST", "/api/issues/pending"));
    expect(sent).toEqual([
      expect.objectContaining({ n: 3, source: "menu", change: { kind: "set_parent", parent: null, expects_parent: true, expected_parent: { repo: "acme/app", n: 10 } } }), // prettier-ignore
      expect.objectContaining({ n: 4, source: "drag", change: expect.objectContaining({ parent: null }) }),
    ]);
  });

  it("children never fetched offer a refresh; ← All epics goes back", async () => {
    const { routes } = epicRoutes(epicDetail(10, { epic: epic(10, { title: "Onboarding" }), children: null }), {
      "POST /api/issues/item/acme/app/10/refresh": () => json({ error: "X", detail: "offline" }, 503),
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    expect(screen.getByText(/haven't been fetched yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await toast(/Couldn't refresh acme\/app#10: offline/)).toBe(true);
    expect(callsTo(calls, "POST", "/api/issues/item/acme/app/10/refresh")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "← All epics" }));
    expect(await screen.findByRole("region", { name: "Issues without an epic" })).toBeInTheDocument();
  });
});

describe("the brief", () => {
  it("starts from the composed brief, posts the edit, and Copy brief copies what is in the editor", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { routes } = epicRoutes(undefined, {
      [`POST ${EPIC}/brief`]: () => json({ comment_id: 1, url: "https://github.com/acme/app/issues/10#c1", text: "x", state: "posted", posted_at: "2026-10-18T12:00:00Z", updated_at: "2026-10-18T12:00:00Z" }), // prettier-ignore
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    const editor = screen.getByRole("textbox", { name: "Brief text" });
    expect(editor).toHaveValue(epicDetail(10).default_brief);
    await userEvent.clear(editor);
    await userEvent.click(editor);
    await userEvent.paste("Ship onboarding.");
    await userEvent.click(screen.getByRole("button", { name: "Copy brief" }));
    expect(writeText).toHaveBeenCalledWith("Ship onboarding.");
    await userEvent.click(screen.getByRole("button", { name: "Post the brief" }));
    expect(await bodies(callsTo(calls, "POST", `${EPIC}/brief`))).toEqual([
      { text: "Ship onboarding.", suggestion_id: null, overwrite: false },
    ]);
    expect(await toast(/Posted the brief on acme\/app#10/)).toBe(true);
  });

  it("a brief edited on GitHub asks to refresh or overwrite; Overwrite sends an explicit overwrite", async () => {
    let answers = 0;
    const { routes } = epicRoutes(
      epicDetail(10, {
        epic: epic(10, { title: "Onboarding" }),
        brief: { comment_id: 1, url: "https://github.com/acme/app/issues/10#c1", text: "Old brief", state: "posted", posted_at: "2026-10-18T12:00:00Z", updated_at: "2026-10-18T12:00:00Z" }, // prettier-ignore
      }),
      {
        [`POST ${EPIC}/brief`]: () =>
          answers++ === 0
            ? json({ error: "BriefEditedOnGitHubError", detail: "acme/app#10's brief was edited on GitHub" }, 409)
            : json({ comment_id: 1, url: null, text: "Old brief", state: "posted", posted_at: null, updated_at: "2026-10-18T12:00:00Z" }), // prettier-ignore
        "POST /api/issues/item/acme/app/10/refresh": () => json({}),
      },
    );
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    expect(screen.getByRole("link", { name: "View it on GitHub" })).toHaveAttribute("href", "https://github.com/acme/app/issues/10#c1"); // prettier-ignore
    expect(screen.getByRole("textbox", { name: "Brief text" })).toHaveValue("Old brief");
    await userEvent.click(screen.getByRole("button", { name: "Post the edit" }));
    const alert = await screen.findByText(/edited on GitHub since it was posted\. Refresh/);
    expect(await toast(/refresh, or overwrite it/)).toBe(true);
    await userEvent.click(
      within(alert.closest("[role=alert]") as HTMLElement).getByRole("button", { name: "Overwrite" }),
    );
    await waitFor(() => expect(callsTo(calls, "POST", `${EPIC}/brief`)).toHaveLength(2));
    expect((await bodies(callsTo(calls, "POST", `${EPIC}/brief`)))[1]).toEqual({ text: "Old brief", suggestion_id: null, overwrite: true }); // prettier-ignore
  });

  it("Refresh re-reads the epic and starts over from the stored brief", async () => {
    let edited = true;
    const { routes } = epicRoutes(undefined, {
      [`POST ${EPIC}/brief`]: () => json({ error: "BriefEditedOnGitHubError", detail: "edited on GitHub" }, 409),
      "POST /api/issues/item/acme/app/10/refresh": () => {
        edited = false;
        return json({});
      },
    });
    renderApp(routes, "/issues/epics");
    await openEpic();
    const editor = screen.getByRole("textbox", { name: "Brief text" });
    await userEvent.click(editor);
    await userEvent.paste(" more");
    await userEvent.click(screen.getByRole("button", { name: "Post the brief" }));
    const alert = (await screen.findByText(/since it was posted\. Refresh to start/)).closest(
      "[role=alert]",
    ) as HTMLElement;
    await userEvent.click(within(alert).getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(edited).toBe(false));
    await waitFor(() => expect(editor).toHaveValue(epicDetail(10).default_brief));
    expect(screen.queryByText(/since it was posted\. Refresh to start/)).toBeNull();
  });

  it("a cut-off post says so; hidden characters show as tokens; a refused clipboard says so", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("denied")) },
      configurable: true,
    });
    const { routes } = epicRoutes(
      epicDetail(10, {
        epic: epic(10, { title: "Onboarding" }),
        brief: { comment_id: null, url: null, text: `Brief${String.fromCodePoint(0x202e)} text`, state: "in_flight", posted_at: null, updated_at: "2026-10-18T12:00:00Z" }, // prettier-ignore
      }),
    );
    renderApp(routes, "/issues/epics");
    await openEpic();
    expect(screen.getByText(/A post of the brief was cut off/)).toBeInTheDocument();
    expect(screen.getByText(/The brief has hidden characters/)).toBeInTheDocument();
    expect(screen.getByText(/Brief⟪U\+202E⟫ text/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy brief" }));
    expect(await toast(/the browser refused the clipboard/)).toBe(true);
  });

  it("asks Claude for a draft, polls, and posts the draft it uses with its suggestion id", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let done = false;
    const DRAFT = suggestion("b1", { repo: null, n: null, scope: "brief:acme/app#10", kind: "brief", payload: { repo: "acme/app", n: 10, text: "Claude's brief" } }); // prettier-ignore
    const run = (status: "running" | "done") => ({ scope: "brief:acme/app#10", status, cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: status === "done" ? "b1" : null }); // prettier-ignore
    const { routes } = epicRoutes(undefined, {
      [`POST ${EPIC}/brief/draft`]: () => json(run("running"), 202),
      "GET /api/issues/suggestions": (req: Request) =>
        json(
          new URL(req.url).searchParams.get("scope") === "brief:acme/app#10" && done
            ? suggestions([DRAFT], { ask_run: run("done") })
            : suggestions([], { ask_run: run("running") }),
        ),
      "POST /api/issues/suggestions/b1/dismiss": () => json(DRAFT),
      [`POST ${EPIC}/brief`]: () => json({ comment_id: 1, url: null, text: "x", state: "posted", posted_at: null, updated_at: "2026-10-18T12:00:00Z" }), // prettier-ignore
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    await userEvent.click(screen.getByRole("button", { name: "Ask Claude for a draft" }));
    expect(await screen.findAllByText("Claude is working on it…")).not.toHaveLength(0);
    done = true;
    await tick(ASK_POLL_MS);
    await userEvent.click(await screen.findByRole("button", { name: "Use Claude's draft" }));
    expect(screen.getByRole("textbox", { name: "Brief text" })).toHaveValue("Claude's brief");
    await userEvent.click(screen.getByRole("button", { name: "Post the brief" }));
    expect(await bodies(callsTo(calls, "POST", `${EPIC}/brief`))).toEqual([
      { text: "Claude's brief", suggestion_id: "b1", overwrite: false },
    ]);
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss the draft" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/b1/dismiss")).toHaveLength(1));
  });
});

describe("the checklist and proposals", () => {
  const PLAN = {
    repo: "acme/app",
    n: 10,
    digest: "a".repeat(64),
    children: [
      { item: "Fix #3", repo: "acme/app", n: 3, temp_id: null, title: "Fix it", is_new: false, warning: null },
      { item: "Docs", repo: null, n: null, temp_id: "item-2", title: "Write docs", is_new: true, warning: "won't fit one PR" }, // prettier-ignore
    ],
    skipped: [{ item: "Old", reason: "already converted" }],
  };

  it("previews the conversion as a diff and approves it as one batch, followed in the tray", async () => {
    let outdated = true;
    const { routes } = epicRoutes(epicDetail(10, { epic: epic(10, { title: "Onboarding" }), checklist_items: 3 }), {
      [`GET ${EPIC}/conversion`]: () => json(PLAN),
      [`POST ${EPIC}/conversion`]: () =>
        outdated
          ? json({ error: "ConversionOutdatedError", detail: "the plan changed" }, 409)
          : json(batch("c1", { origin: "epic" }), 202),
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    expect(screen.getByText(/The body has 3 checklist items/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Preview the conversion" }));
    const plan = await screen.findByRole("region", { name: "Convert the checklist of acme/app#10" });
    expect(plan).toHaveTextContent("Link acme/app#3 Fix it");
    expect(plan).toHaveTextContent("New issue Write docs");
    expect(plan).toHaveTextContent("won't fit one PR");
    expect(plan).toHaveTextContent("Skipped Old: already converted");
    await userEvent.click(within(plan).getByRole("button", { name: "Approve" }));
    expect(await within(plan).findByRole("alert")).toHaveTextContent(/The checklist changed since the preview/);
    outdated = false;
    await userEvent.click(within(plan).getByRole("button", { name: "Preview again" }));
    const again = await screen.findByRole("region", { name: "Convert the checklist of acme/app#10" });
    await userEvent.click(within(again).getByRole("button", { name: "Approve" }));
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Checklist of acme/app#10 to sub-issues")).toBeInTheDocument();
    expect(await bodies(callsTo(calls, "POST", `${EPIC}/conversion`))).toEqual([{ digest: PLAN.digest }, { digest: PLAN.digest }]); // prettier-ignore
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Convert the checklist of acme/app#10" })).toBeNull(),
    );
  });

  it("a conversion that can't be planned says why", async () => {
    const { routes } = epicRoutes(epicDetail(10, { epic: epic(10, { title: "Onboarding" }), checklist_items: 1 }), {
      [`GET ${EPIC}/conversion`]: () => json({ error: "NothingToConvertError", detail: "nothing left" }, 409),
    });
    renderApp(routes, "/issues/epics");
    await openEpic();
    await userEvent.click(screen.getByRole("button", { name: "Preview the conversion" }));
    expect(await screen.findByText(/Nothing in the checklist is left to convert/)).toBeInTheDocument();
  });

  it("lists additions and splits for this epic, asks for more, and proposes a split of a child", async () => {
    const ADD = suggestion("a1", { repo: null, n: null, kind: "group", payload: { epic: { repo: "acme/app", n: 10 }, issues: [{ repo: "acme/app", n: 7 }] }, reason: "Same area." }); // prettier-ignore
    const ELSEWHERE = suggestion("a2", { repo: null, n: null, kind: "group", payload: { epic: { repo: "acme/app", n: 99 }, issues: [{ repo: "acme/app", n: 7 }] } }); // prettier-ignore
    const SPLIT = suggestion("s9", { repo: null, n: null, kind: "split", payload: { issue: { repo: "acme/app", n: 3 }, parts: [{ title: "Part A", body: "" }, { title: "Part B", body: "" }] }, reason: "Too big." }); // prettier-ignore
    const running = {
      scope: "x",
      status: "running",
      cause: null,
      updated_at: "2026-10-18T12:00:00Z",
      suggestion_id: null,
    };
    const { routes } = epicRoutes(undefined, {
      "GET /api/issues/suggestions": (req: Request) => {
        const kind = new URL(req.url).searchParams.get("kind");
        return json(suggestions(kind === "group" ? [ADD, ELSEWHERE] : kind === "split" ? [SPLIT] : []));
      },
      "POST /api/issues/epics/proposals": () => json({ ...running, status: "waiting" }, 202),
      "POST /api/issues/suggestions/split": () => json({ ...running, status: "failed", cause: "inputs changed, ask again" }, 202), // prettier-ignore
      "POST /api/issues/suggestions/s9/dismiss": () => json(SPLIT),
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    const proposals = screen.getByRole("region", { name: "Proposals" });
    expect(await within(proposals).findByRole("listitem", { name: "Add 1 issue to acme/app#10" })).toHaveTextContent("Link acme/app#7 under acme/app#10"); // prettier-ignore
    expect(within(proposals).queryByRole("listitem", { name: "Add 1 issue to acme/app#99" })).toBeNull();
    const split = within(proposals).getByRole("listitem", { name: "Split acme/app#3 into 2 parts" });
    expect(split).toHaveTextContent("New part Part A");
    await userEvent.click(within(proposals).getByRole("button", { name: "Ask Claude for additions" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/epics/proposals"))).toEqual([{ repo: "acme/app" }]);
    expect(await screen.findByText(/No agent runner is set up/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Propose a split of acme/app#3" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/suggestions/split"))).toEqual([{ repo: "acme/app", n: 3 }]);
    expect(await screen.findByText("The ask failed: inputs changed, ask again")).toBeInTheDocument();
    await userEvent.click(within(split).getByRole("button", { name: "Dismiss: Split acme/app#3 into 2 parts" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/s9/dismiss")).toHaveLength(1));
  });
});

describe("fix round 1: hidden characters, names, and queueing an epic", () => {
  it("readiness, blockers and warnings built from label names show hidden characters as tokens (M1)", async () => {
    const detail = epicDetail(10, {
      epic: epic(10, { title: "Onboarding", readiness: readiness({ note: "held​", warning: "odd​" }) }),
      children: [member(3, { blocker: "conflicting labels: a​", warning: "w​" })],
    });
    renderApp(epicRoutes(detail).routes, "/issues/epics");
    const children = await openEpic();
    const page = screen.getByRole("heading", { level: 1 }).parentElement as HTMLElement;
    expect(page).toHaveTextContent("held⟪U+200B⟫");
    expect(page).toHaveTextContent("odd⟪U+200B⟫");
    expect(children).toHaveTextContent("Blocks: conflicting labels: a⟪U+200B⟫");
    expect(children).toHaveTextContent("w⟪U+200B⟫");
  });

  it("the brief's region and its editor have different names (M11)", async () => {
    renderApp(epicRoutes().routes, "/issues/epics");
    await openEpic();
    expect(screen.getByRole("region", { name: "Delivery brief" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Brief text" })).toBeInTheDocument();
  });

  it("the open epic can be added to a queue, as an epic (M3)", async () => {
    const { routes } = epicRoutes(undefined, {
      "GET /api/issues/queues": { queues: [queueSummary("q1")] },
      "POST /api/issues/queues/q1/items": () => json(queue("q1", []), 201),
    });
    const { calls } = renderApp(routes, "/issues/epics");
    await openEpic();
    const bar = screen.getByRole("toolbar", { name: "Epic" });
    await userEvent.click(within(bar).getByRole("button", { name: "Add acme/app#10 to a queue" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Next up" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/queues/q1/items")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/queues/q1/items"))).toEqual([{ kind: "epic", repo: "acme/app", n: 10 }]); // prettier-ignore
  });
});
