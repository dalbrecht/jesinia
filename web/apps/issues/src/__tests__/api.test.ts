// SPDX-License-Identifier: AGPL-3.0-or-later
// The Issues app's pure rules: error copy, idempotency keys, how changes, suggestions, results
// and statuses read, and the routes.
import { describe, expect, it } from "vitest";
import {
  ActionKeys,
  batchHeadline,
  describeChange,
  describeSuggestion,
  errorCause,
  freshness,
  issuePath,
  issueResult,
  IssuesRequestError,
  nextPollMs,
  refOf,
  routeOf,
  statusOf,
  syncedAgo,
  type Suggestion,
} from "../api";
import { batch, finished, REPO, row, suggestion, view } from "./fixtures";

describe("errors", () => {
  it("leads typed 409s with what they mean, then the server's cause", () => {
    const cases: [string, RegExp][] = [
      ["SuggestionStaleError", /^GitHub changed since this suggestion was made\. why\.$/],
      ["SuggestionStateError", /already approved or dismissed/],
      ["EarlierBatchError", /An earlier batch for this hasn't finished/],
      ["PendingHeldError", /A queue item's Done holds this change/],
      ["TrayChangeStagedError", /already staged a change for this in Pending changes/],
      ["NothingPendingError", /Nothing is staged to apply/],
      ["BatchRunningError", /still running/],
      ["BatchAbandonedError", /was abandoned/],
      ["DuplicateNotCachedError", /original issue isn't cached yet/],
      ["IssueNotCachedError", /isn't cached yet/],
      ["ContainmentRefusedError", /can't hold that issue/],
      ["PendingNotFoundError", /no longer staged/],
    ];
    for (const [code, copy] of cases) {
      expect(errorCause(new IssuesRequestError("why", 409, code)), code).toMatch(copy);
    }
    expect(errorCause(new IssuesRequestError("Plain cause", 400, "OtherError"))).toBe("Plain cause.");
    expect(errorCause(new IssuesRequestError("Already a sentence!", 400, null))).toBe("Already a sentence!");
    expect(errorCause(new TypeError("Failed to fetch"))).toMatch(/^Jesinia didn't answer \(Failed to fetch\)/);
    expect(errorCause("odd")).toMatch(/didn't answer \(odd\)/);
  });

  it("is transient for 5xx and Retry-After answers", () => {
    expect(new IssuesRequestError("x", 503, null).isTransient).toBe(true);
    expect(new IssuesRequestError("x", 409, "EarlierBatchError").isTransient).toBe(false);
    expect(new IssuesRequestError("x", 429, null, 3).isTransient).toBe(true);
  });

  it("keeps an action's key while its outcome is unknown, and drops it once answered", () => {
    const keys = new ActionKeys();
    const first = keys.keyFor("apply");
    expect(keys.keyFor("apply")).toBe(first);
    keys.settle("apply", new TypeError("network"));
    expect(keys.keyFor("apply")).toBe(first);
    keys.settle("apply", new IssuesRequestError("x", 500, null));
    expect(keys.keyFor("apply")).toBe(first);
    keys.settle("apply", new IssuesRequestError("x", 409, "NothingPendingError"));
    const second = keys.keyFor("apply");
    expect(second).not.toBe(first);
    keys.settle("apply");
    expect(keys.keyFor("apply")).not.toBe(second);
  });
});

describe("how things read", () => {
  it("describes every staged change", () => {
    const target = { repo: REPO, n: 3 };
    expect(describeChange({ kind: "set_dimension", dimension: "type", value: "bug" })).toBe("Set type to bug");
    expect(describeChange({ kind: "add_area", value: "area:web" })).toBe("Add area area:web");
    expect(describeChange({ kind: "remove_area", value: "area:web" })).toBe("Remove area area:web");
    expect(describeChange({ kind: "add_label", name: "theme:sync" })).toBe("Add label theme:sync");
    expect(
      describeChange({ kind: "ensure_label", repo: REPO, name: "theme:sync", color: "ededed", description: "" }),
    ).toBe("Create label theme:sync");
    expect(describeChange({ kind: "set_parent", parent: target, expects_parent: false, expected_parent: null })).toBe(
      "Move under acme/app#3",
    );
    expect(describeChange({ kind: "set_parent", parent: null, expects_parent: true, expected_parent: target })).toBe(
      "Remove from its epic",
    );
    expect(describeChange({ kind: "reorder_child", child: target, after: null })).toBe("Move acme/app#3 to the top");
    expect(describeChange({ kind: "reorder_child", child: target, after: { repo: REPO, n: 4 } })).toBe(
      "Move acme/app#3 after acme/app#4",
    );
    expect(describeChange({ kind: "close", reason: "not_planned", duplicate_of: null, comment: null })).toBe(
      "Close as not planned",
    );
    expect(describeChange({ kind: "close", reason: "duplicate", duplicate_of: target, comment: null })).toBe(
      "Close as a duplicate of acme/app#3",
    );
    expect(describeChange({ kind: "comment", body: "Thanks!" })).toBe("Comment");
  });

  it("describes every suggestion kind", () => {
    const s = (kind: Suggestion["kind"], payload: Record<string, unknown>) =>
      describeSuggestion(suggestion("x", { kind, payload: payload as never }));
    expect(s("label", { dimension: "area", value: "area:web" })).toBe("Set area to area:web");
    expect(s("new_area", { dimension: "area", value: "area:cli" })).toBe("Add a new area: area:cli");
    expect(s("duplicate", { repo: REPO, n: 2, similarity: 0.5 })).toBe("Duplicate of acme/app#2");
    expect(s("parent", { repo: REPO, n: 9 })).toBe("Belongs to epic acme/app#9");
    expect(s("readiness_gap", { ready: true, missing: [] })).toBe("Could be agent-ready");
    expect(s("readiness_gap", { ready: false, missing: ["steps", "logs"] })).toBe(
      "Not agent-ready yet: missing steps, logs",
    );
    expect(s("priority", { items: [] })).toBe("priority");
  });

  it("sums up a batch and each issue's steps", () => {
    expect(batchHeadline(batch("b"))).toBe("Applying…");
    expect(batchHeadline(finished("b"))).toBe("All changes applied");
    expect(batchHeadline(finished("b", { complete: false }))).toBe("Some changes didn't apply");
    expect(batchHeadline(finished("b", { complete: false, stopped: "token rejected" }))).toBe(
      "GitHub stopped this batch: token rejected",
    );
    expect(batchHeadline(batch("b", { status: "interrupted" }))).toMatch(/^Interrupted/);
    expect(batchHeadline(batch("b", { status: "abandoned" }))).toBe("Abandoned");

    const step = (state: "done" | "failed" | "skipped", cause: string | null = null, conflict = false) => ({
      kind: "labels" as const,
      state,
      cause,
      conflict,
    });
    const issue = (steps: ReturnType<typeof step>[]) => ({ repo: REPO, n: 1, temp_id: null, steps });
    expect(issueResult(issue([step("done")]))).toEqual({ state: "applied", text: "Applied", conflict: false });
    expect(issueResult(issue([step("done"), step("failed", "boom", true)]))).toEqual({
      state: "failed",
      text: "labels failed: boom",
      conflict: true,
    });
    expect(issueResult(issue([step("skipped", "not run yet")]))).toEqual({
      state: "skipped",
      text: "labels skipped: not run yet",
      conflict: false,
    });
    expect(issueResult(issue([]))).toEqual({ state: "applied", text: "Applied", conflict: false });
  });

  it("derives the status column from GitHub's state, the linked PRs and completeness", () => {
    expect(statusOf(row(1))).toBe("triage");
    expect(statusOf(row(1, { complete: true }))).toBe("backlog");
    expect(statusOf(row(1, { complete: true, linked_pr_state: "open" }))).toBe("in_progress");
    expect(statusOf(row(1, { complete: false, linked_pr_state: "open" }))).toBe("in_progress");
    expect(statusOf(row(1, { linked_pr_state: "merged" }))).toBe("done");
    expect(statusOf(row(1, { state: "closed" }))).toBe("done");
  });

  it("names refs and routes", () => {
    expect(refOf({ repo: REPO, number: 4 })).toBe("acme/app#4");
    expect(issuePath(REPO, 4)).toBe("/issues/acme/app/4");
    expect(routeOf("/issues/acme/app/4")).toEqual({ kind: "issue", repo: REPO, number: 4 });
    expect(routeOf("/issues/Acme/App/4/")).toEqual({ kind: "issue", repo: REPO, number: 4 });
    expect(routeOf("/issues/")).toEqual({ kind: "inbox" });
    expect(routeOf("/issues")).toEqual({ kind: "inbox" });
    expect(routeOf("/issues/backlog")).toEqual({ kind: "backlog" });
    expect(routeOf("/issues/epics")).toEqual({ kind: "epics" });
    expect(routeOf("/issues/queues/")).toEqual({ kind: "queues" });
    expect(routeOf("/issues/nope/x")).toEqual({ kind: "inbox" });
  });
});

describe("freshness and polling", () => {
  const now = new Date("2026-10-18T12:00:00Z");
  it("says how long ago, for every range", () => {
    expect(syncedAgo("2026-10-18T11:59:30Z", now)).toBe("just now");
    expect(syncedAgo("2026-10-18T11:30:00Z", now)).toBe("30 min ago");
    expect(syncedAgo("2026-10-18T09:00:00Z", now)).toBe("3 h ago");
    expect(syncedAgo("2026-10-17T09:00:00Z", now)).toBe("1 day ago");
    expect(syncedAgo("2026-10-15T09:00:00Z", now)).toBe("3 days ago");
    expect(syncedAgo("2026-09-01T09:00:00Z", now)).toMatch(/^on /);
    expect(syncedAgo("nope", now)).toBeNull();
    expect(freshness(view([], { synced_at: null }), now)).toBe("Not updated yet");
    expect(freshness(view([], { synced_at: "2026-10-18T11:30:00Z", syncing: true }), now)).toBe(
      "Updated 30 min ago · refreshing…",
    );
  });

  it("reads often while syncing or before the first sync, and backs off on failures", () => {
    expect(nextPollMs(null, 0)).toBe(2000);
    expect(nextPollMs(view([], { syncing: true }), 0)).toBe(2000);
    expect(nextPollMs(view([], { synced_at: null }), 0)).toBe(2000);
    expect(nextPollMs(view(), 0)).toBe(60_000);
    expect(nextPollMs(view(), 1)).toBe(2000);
    expect(nextPollMs(view(), 3)).toBe(8000);
    expect(nextPollMs(view(), 20)).toBe(60_000);
  });
});
