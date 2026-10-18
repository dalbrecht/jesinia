// SPDX-License-Identifier: AGPL-3.0-or-later
// An issue in the book layout (spec §6): outline, toolbar, suggestions with Accept, Edit and
// Dismiss, and Approve and Dismiss all. Approving applies at once (spec §6.2), never via the tray.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Suggestion } from "../api";
import { RUN_POLL_MS } from "../IssueView";
import { BATCH_POLL_MS } from "../Tray";
import {
  batch,
  callsTo,
  detail,
  finished,
  issuesRoutes,
  json,
  renderApp,
  S_DUP,
  S_READY,
  suggestion,
  suggestions,
  tick,
  toast,
} from "./fixtures";

const ISSUE = "/issues/acme/app/1";
const suggestionsRegion = () => screen.getByRole("region", { name: "Suggestions" });
const card = (text: string) => {
  const found = within(suggestionsRegion()).getByText(text).closest("li");
  if (!found) throw new Error(`no suggestion ${text}`);
  return within(found);
};

afterEach(() => {
  vi.useRealTimers();
});

describe("the book layout", () => {
  it("shows the outline, toolbar and summary, with untrusted text as text", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({ title: `Fix ${String.fromCodePoint(0x202e)}login` }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ title: `Fix ${String.fromCodePoint(0x202e)}login` }),
        "GET /api/issues/suggestions": suggestions([
          suggestion("s9", { reason: `<b>bold</b> ${String.fromCodePoint(0x200b)} claim` }),
        ]),
      }),
      ISSUE,
    );
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("Fix ⟪U+202E⟫login");
    const outline = screen.getByRole("navigation", { name: "Outline" });
    for (const part of ["Summary", "Timeline", "Suggestions", "Related", "Epic"]) {
      expect(
        within(outline).getByRole("link", { name: (accessible) => accessible.startsWith(part) }),
      ).toBeInTheDocument();
    }
    const toolbar = screen.getByRole("toolbar", { name: "Issue" });
    expect(within(toolbar).getByRole("combobox", { name: "Readiness" })).toHaveValue("");
    expect(within(toolbar).getByRole("combobox", { name: "Priority" })).toHaveValue("priority: medium");
    expect(within(toolbar).getByText("acme/app")).toBeInTheDocument();
    const gh = within(toolbar).getByRole("link", { name: /Open on GitHub/ });
    expect(gh).toHaveAttribute("href", "https://github.com/acme/app/issues/1");
    expect(gh).toHaveAttribute("rel", expect.stringContaining("noopener"));
    // The body is Markdown, rendered safely: emphasis kept, raw HTML never.
    expect(screen.getByText("fails").tagName).toBe("STRONG");
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByText("Seen it too.")).toBeInTheDocument();
    expect(screen.getByText(/labeled/)).toBeInTheDocument();
    // Agent text is text: markup shows as written, hidden characters as tokens.
    expect(suggestionsRegion()).toHaveTextContent("<b>bold</b> ⟪U+200B⟫ claim");
    expect(suggestionsRegion().querySelector("b")).toBeNull();
  });

  it("fetches the comments and timeline once when opened", async () => {
    const { calls } = renderApp(issuesRoutes(), ISSUE);
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/item/acme/app/1/refresh")).toHaveLength(1));
    expect(
      callsTo(calls, "POST", "/api/issues/item/acme/app/1/refresh")[0]?.headers.get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("an issue that isn't cached says so", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": () =>
          json({ error: "IssueNotCachedError", detail: "acme/app#1 is not in the cache" }, 404),
        "POST /api/issues/item/acme/app/1/refresh": () =>
          json({ error: "IssueNotCachedError", detail: "acme/app#1 is not in the cache" }, 404),
      }),
      ISSUE,
    );
    expect(await screen.findByText(/isn't cached yet/)).toBeInTheDocument();
  });

  it("related lists duplicates and the epic shows the parent", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/item/acme/app/1": detail({ parent: "acme/app#9" }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ parent: "acme/app#9" }),
      }),
      ISSUE,
    );
    const related = await screen.findByRole("region", { name: "Related" });
    expect(within(related).getByText(/acme\/app#2/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Epic" })).getByText("acme/app#9")).toBeInTheDocument();
  });
});

describe("suggestions", () => {
  it("Accept approves at once: a keyed 202, polled to its result, never staged", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    let current = batch("b1", { origin: "suggestion" });
    let open: Suggestion[] = [S_READY, S_DUP];
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": () => json(suggestions(open)),
        "POST /api/issues/suggestions/s1/approve": () =>
          json({ suggestion: S_READY, batch: current }, 202, { Location: "/api/issues/pending/b1" }),
        "GET /api/issues/pending/b1": () => json(current),
      }),
      ISSUE,
    );
    await screen.findByRole("region", { name: "Suggestions" });
    await user.click(card("Set readiness to agent-ready").getByRole("button", { name: "Accept" }));
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(await within(tray).findByText(/Applying/)).toBeInTheDocument();
    expect(within(tray).getByText(/Approved: Set readiness to agent-ready on acme\/app#1/)).toBeInTheDocument();
    expect(
      callsTo(calls, "POST", "/api/issues/suggestions/s1/approve")[0]?.headers.get("Idempotency-Key"),
    ).toBeTruthy();
    expect(callsTo(calls, "POST", "/api/issues/pending")).toHaveLength(0);

    current = finished("b1", { origin: "suggestion" });
    open = [S_DUP];
    await tick(BATCH_POLL_MS);
    expect(await within(tray).findByText("All changes applied")).toBeInTheDocument();
    // The issue and its suggestions are read again once the batch is done.
    await waitFor(() => expect(within(suggestionsRegion()).queryByText("Set readiness to agent-ready")).toBeNull());
  });

  it("a stale approval says GitHub changed, and nothing is tracked", async () => {
    renderApp(
      issuesRoutes({
        "POST /api/issues/suggestions/s1/approve": () =>
          json({ error: "SuggestionStaleError", detail: "readiness is already set on GitHub" }, 409),
      }),
      ISSUE,
    );
    await screen.findByRole("region", { name: "Suggestions" });
    await userEvent.click(card("Set readiness to agent-ready").getByRole("button", { name: "Accept" }));
    expect(await toast(/GitHub changed since this suggestion was made/)).toBeTruthy();
    expect(await toast(/readiness is already set on GitHub/)).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Pending changes" })).toBeNull();
  });

  it("Edit changes a label suggestion's value (keyed PATCH); Dismiss dismisses one", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "PATCH /api/issues/suggestions/s1": () =>
          json(suggestion("s1", { state: "edited", payload: { dimension: "readiness", value: "blocked" } })),
        "POST /api/issues/suggestions/s2/dismiss": () => json({ ...S_DUP, state: "dismissed" }),
      }),
      ISSUE,
    );
    await screen.findByRole("region", { name: "Suggestions" });
    const ready = card("Set readiness to agent-ready");
    await userEvent.click(ready.getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(ready.getByRole("combobox", { name: "New readiness" }), "blocked");
    await userEvent.click(ready.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(callsTo(calls, "PATCH", "/api/issues/suggestions/s1")).toHaveLength(1));
    const [patch] = callsTo(calls, "PATCH", "/api/issues/suggestions/s1");
    expect(patch?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await patch?.clone().json()).toEqual({ payload: { dimension: "readiness", value: "blocked" } });

    expect(card("Duplicate of acme/app#2").queryByRole("button", { name: "Edit" })).toBeNull();
    await userEvent.click(card("Duplicate of acme/app#2").getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/s2/dismiss")).toHaveLength(1));
  });

  it("Approve approves every open suggestion; Dismiss all dismisses every one", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "POST /api/issues/suggestions/s1/approve": () => json({ suggestion: S_READY, batch: finished("b1") }, 202),
        "POST /api/issues/suggestions/s2/approve": () => json({ suggestion: S_DUP, batch: finished("b2") }, 202),
        "POST /api/issues/suggestions/s1/dismiss": () => json({ ...S_READY, state: "dismissed" }),
        "POST /api/issues/suggestions/s2/dismiss": () => json({ ...S_DUP, state: "dismissed" }),
      }),
      ISSUE,
    );
    const actions = await screen.findByRole("group", { name: "Decide" });
    await userEvent.click(within(actions).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/s2/approve")).toHaveLength(1));
    expect(callsTo(calls, "POST", "/api/issues/suggestions/s1/approve")).toHaveLength(1);
    await userEvent.click(within(actions).getByRole("button", { name: "Dismiss all" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/s2/dismiss")).toHaveLength(1));
    expect(callsTo(calls, "POST", "/api/issues/suggestions/s1/dismiss")).toHaveLength(1);
  });

  it("without suggestions, says why and offers to ask; describes every kind", async () => {
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": suggestions([], {
          run: {
            repo: "acme/app",
            n: 1,
            status: "waiting",
            source: "inbox",
            due_at: "2026-10-18T12:00:00Z",
            cause: null,
            updated_at: "2026-10-18T12:00:00Z",
          },
        }),
        "POST /api/issues/suggestions/run": () => json({ runs: [] }, 202),
      }),
      ISSUE,
    );
    expect(await screen.findByText(/waiting for an agent runner/)).toBeInTheDocument();
    await userEvent.click(within(suggestionsRegion()).getByRole("button", { name: "Suggest with Claude" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/suggestions/run")).toHaveLength(1));
    expect(await callsTo(calls, "POST", "/api/issues/suggestions/run")[0]?.clone().json()).toEqual({
      refs: [{ repo: "acme/app", n: 1 }],
    });
  });

  it("reads the suggestions again while Claude is suggesting, until the run is done", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: "running" | "done" = "running";
    const run = () => ({
      repo: "acme/app",
      n: 1,
      status,
      source: "demand" as const,
      due_at: "2026-10-18T12:00:00Z",
      cause: null,
      updated_at: "2026-10-18T12:00:00Z",
    });
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/suggestions": () => json(suggestions(status === "done" ? [S_READY] : [], { run: run() })),
      }),
      ISSUE,
    );
    expect(await screen.findByText("Claude is suggesting…")).toBeInTheDocument();
    const reads = () => callsTo(calls, "GET", "/api/issues/suggestions").length;
    status = "done";
    await tick(RUN_POLL_MS);
    expect(await within(suggestionsRegion()).findByText("Set readiness to agent-ready")).toBeInTheDocument();
    const after = reads();
    await tick(RUN_POLL_MS * 3);
    expect(reads()).toBe(after);
  });
});
