// SPDX-License-Identifier: AGPL-3.0-or-later
// Incremental re-reviews (plan Task 12, spec §4.2): Your requests, New since your last look, and the
// previous review shown beside the new one, aligned by file and hunk.
import { fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import type { PreviousReview, Rereview as RereviewData } from "../pr/model";
import { alignPrevious, newChangesLine, NewSinceLastLook, YourRequests } from "../pr/Rereview";
import { queue, routes } from "./fixtures";
import { API, ARTIFACTS, drafts, HEAD, NEWER, PATH, reviewed, XSS } from "./pr-fixtures";

// Built at run time, so this file holds no bidi character itself.
const RLO = String.fromCharCode(0x202e);
const PRORATION = "src/billing/proration.py";
const ODD = `src/a${RLO}b.py`;

const AT = { side: "RIGHT", start_line: null, resolved: false, now_start_line: null, outdated: false } as const;
const PREVIOUS = {
  source: "round",
  head_sha: "1".repeat(40),
  round_id: 2,
  comments: [
    { ...AT, ref: "c1", body: "A few notes.", path: null, line: null, side: null, now_path: null, now_line: null },
    { ...AT, ref: "c2", body: "Off by one here.", path: PRORATION, line: 15, now_path: PRORATION, now_line: 15 },
    { ...AT, ref: "c3", body: `Rename it. ${XSS}`, path: ODD, line: 3, resolved: true, now_path: ODD, now_line: 3 },
    // Written on a file since renamed, two lines further up: placed by where its text is now (I2).
    {
      ...AT,
      ref: "c4",
      body: "Moved and renamed.",
      path: "src/billing/old.py",
      line: 12,
      now_path: PRORATION,
      now_line: 14,
    },
    // Its line's text changed: outdated, shown as such, never on hunk 1 where line 2 now is.
    {
      ...AT,
      ref: "c5",
      body: "Its line changed.",
      path: PRORATION,
      line: 2,
      now_path: PRORATION,
      now_line: null,
      outdated: true,
    },
    // Written on a commit git can't read any more: unknown, which isn't "changed" (N2).
    {
      ...AT,
      ref: "c6",
      body: "On a lost commit.",
      path: PRORATION,
      line: 2,
      now_path: PRORATION,
      now_line: null,
      unplaced: "commit dddddddddddd couldn't be read: bad object",
    },
  ],
  placement_error: "commit dddddddddddd couldn't be read: bad object; the previous review's diff couldn't be rebuilt",
  // The previous round's walkthrough and findings, placed on this head (I1).
  steps: [
    { title: "Posting, last time", body: "It posted twice.", now_hunks: [{ path: PRORATION, hunk: 1 }], changed: 0 },
    { title: "A step on code since rewritten", body: "Gone.", now_hunks: [], changed: 2 },
    {
      title: "A step Jesinia couldn't match",
      body: "?",
      now_hunks: [],
      changed: 0,
      unplaced: "the previous review's diff couldn't be rebuilt",
    },
  ],
  findings: [
    {
      group: "correctness",
      severity: "important",
      path: "src/billing/old.py",
      start_line: 11,
      end_line: 12,
      explanation: "Posts **twice**.",
      now_path: PRORATION,
      now_start_line: 13,
      now_end_line: 14,
      outdated: false,
    },
    {
      group: "style",
      severity: "minor",
      path: PRORATION,
      start_line: 2,
      end_line: 2,
      explanation: "A name since rewritten.",
      now_path: PRORATION,
      now_start_line: null,
      now_end_line: null,
      outdated: true,
    },
  ],
};

const REREVIEW = {
  requests: [
    {
      comment_ref: "c2",
      status: "addressed",
      explanation: "Fixed in **the ledger** step.",
      hunks: [{ path: PRORATION, hunk: 1 }],
    },
    { comment_ref: "c3", status: "not_addressed", explanation: "Still the old name.", hunks: [] },
  ],
  unanswered: ["c1", "c4", "c5", "c6"],
  dropped_requests: 0,
  previous_round_id: 2,
  new_hunks: [{ path: PRORATION, hunk: 1 }],
  new_changes_walkthrough: {
    steps: [{ title: "The ledger fix", body: "Posts the amount once.", hunks: [{ path: PRORATION, hunk: 1 }] }],
    skipped: [],
  },
  coverage: {
    pct: 100,
    files_covered: 1,
    files_total: 1,
    not_walked: [{ path: PRORATION, hunk: 0, reason: "unchanged" }],
    not_walked_total: 1,
    nothing_to_walk: false,
  },
};

const FOLLOWUP = {
  kind: "rereview",
  differing_paths: [PRORATION],
  note: "New commits since your review",
  prev_head: "1".repeat(40),
};

function rereviewed(artifacts: Record<string, unknown> = {}): PrView {
  return reviewed({
    artifacts: {
      ...ARTIFACTS,
      followup: FOLLOWUP,
      previous_review: PREVIOUS,
      rereview: REREVIEW,
      jobs: { ...ARTIFACTS.jobs, rereview: { state: "done", error: null } },
      ...artifacts,
    },
  });
}

function openPr(view: PrView, over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const go = async (name: RegExp) => userEvent.click(within(outline()).getByRole("link", { name }));

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/review");
});

describe("a re-review", () => {
  it("lists Your requests, then New since your last look, before the walkthrough", async () => {
    openPr(rereviewed());
    await ready();
    const links = within(outline())
      .getAllByRole("link")
      .map((a) => a.textContent ?? "");
    const requests = links.findIndex((t) => t.includes("Your requests"));
    const fresh = links.findIndex((t) => t.includes("New since your last look"));
    const walk = links.findIndex((t) => t.includes("The new prorate function"));
    expect(requests).toBeGreaterThan(-1);
    expect(fresh).toBe(requests + 1);
    expect(walk).toBeGreaterThan(fresh);
  });

  it("counts comments without a status as open in the outline (M1)", async () => {
    openPr(rereviewed());
    await ready();
    // c3 isn't addressed, and c1, c4, c5 and c6 have no status: none of them is known to be done.
    expect(within(outline()).getByRole("link", { name: /Your requests.*5 open/ })).toBeInTheDocument();
  });

  it("gives each of your requests its status, explanation and new hunks", async () => {
    openPr(rereviewed());
    await ready();
    await go(/Your requests/);
    const list = screen.getByRole("list", { name: "Your requests" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(6);
    const [general, offByOne, rename] = items as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(general).getByText("No status given")).toBeInTheDocument();
    expect(within(general).getByText("A few notes.")).toBeInTheDocument();
    expect(within(offByOne).getByText("Addressed")).toBeInTheDocument();
    expect(within(offByOne).getByText("the ledger").tagName).toBe("STRONG"); // safe Markdown
    expect(within(offByOne).getByText(`${PRORATION} · hunk 2`)).toBeInTheDocument(); // shown 1-based, as elsewhere
    expect(within(rename).getByText("Not addressed")).toBeInTheDocument();
    expect(within(rename).getByText("⟪U+202E⟫")).toBeInTheDocument(); // R62: the path's bidi made visible
    expect(within(rename).getByText(/Rename it\./).textContent).toContain(XSS); // shown as text, never HTML
    expect(document.querySelector("img[src=x]")).toBeNull();
  });

  it("shows the previous review aligned by file and hunk", async () => {
    openPr(rereviewed());
    await ready();
    await go(/Your requests/);
    const aligned = screen.getByRole("region", { name: "Previous review, aligned by file and hunk" });
    const file = within(aligned).getByRole("group", { name: PRORATION });
    const hunk1 = within(file).getByRole("row", { name: /hunk 2/ }); // index 1, shown 1-based
    const [before, now] = within(hunk1).getAllByRole("cell") as [HTMLElement, HTMLElement];
    expect(within(before).getByText("Off by one here.")).toBeInTheDocument();
    expect(within(now).getByText("Addressed")).toBeInTheDocument();
    expect(within(now).getByText("New since your last look")).toBeInTheDocument();
    // The first hunk is as reviewed: nothing new, nothing commented, no row.
    expect(within(file).queryByRole("row", { name: /hunk 1\b/ })).toBeNull();
    // A comment written on a renamed file, lines ago, sits where its text is now (I2).
    expect(within(hunk1).getByText("Moved and renamed.")).toBeInTheDocument();
    // The previous round's walkthrough step and finding sit on the same hunk (I1).
    expect(within(before).getByText("Posting, last time")).toBeInTheDocument();
    expect(within(before).getByText("twice").tagName).toBe("STRONG");
    // What changed since is outdated in its file, never placed on the hunk its old line falls in.
    const changed = within(file).getByRole("row", { name: /changed since your last review/ });
    expect(within(changed).getByText("Its line changed.")).toBeInTheDocument();
    expect(within(changed).getByText("A name since rewritten.")).toBeInTheDocument();
    // A step whose every hunk changed is listed apart, with how many.
    const rewritten = within(aligned).getByRole("group", { name: "Steps on code that changed since" });
    expect(within(rewritten).getByText("A step on code since rewritten")).toBeInTheDocument();
    expect(within(rewritten).getByText("2 of its hunks changed")).toBeInTheDocument();
    // What couldn't be placed says why, apart from what changed (N2).
    expect(within(changed).queryByText("On a lost commit.")).toBeNull();
    const unplaced = within(file).getByRole("row", { name: /couldn't place on this head/ });
    expect(within(unplaced).getByText("On a lost commit.")).toBeInTheDocument();
    expect(
      within(unplaced).getByText(/Couldn't place this on this head: commit dddddddddddd couldn't be read/),
    ).toBeInTheDocument();
    expect(within(changed).getAllByText("Outdated: its lines changed").length).toBeGreaterThan(0);
    const unmatched = within(aligned).getByRole("group", { name: "Steps Jesinia couldn't place" });
    expect(within(unmatched).getByText("A step Jesinia couldn't match")).toBeInTheDocument();
    expect(within(unmatched).getByText("the previous review's diff couldn't be rebuilt")).toBeInTheDocument();
    expect(within(aligned).getByRole("status")).toHaveTextContent(
      "Some of your last review couldn't be placed on this head: commit dddddddddddd",
    );
    // A comment on a file this head no longer changes still has its row.
    const odd = within(aligned).getByRole("group", { name: `src/a⟪U+202E⟫b.py` });
    expect(within(odd).getByText(/Rename it\./)).toBeInTheDocument();
    expect(within(odd).getByText("Not addressed")).toBeInTheDocument();
    // The review's general comment sits with the review, not a file.
    const general = within(aligned).getByRole("group", { name: "The review" });
    expect(within(general).getByText("A few notes.")).toBeInTheDocument();
  });

  it("walks the new changes, with their own coverage", async () => {
    openPr(rereviewed());
    await ready();
    await go(/New since your last look/);
    const page = screen.getByRole("article", { name: "New since your last look" });
    expect(within(page).getByRole("heading", { name: "The ledger fix" })).toBeInTheDocument();
    expect(within(page).getByText("Covers 100% of the new changes · 1 of 1 files")).toBeInTheDocument();
    expect(within(page).getByText(`${PRORATION} · hunk 1`)).toBeInTheDocument();
    expect(within(page).getByText("unchanged since your last review")).toBeInTheDocument();
  });

  it("says the re-review is running, and offers Retry when it failed", async () => {
    openPr(
      rereviewed({ rereview: undefined, jobs: { ...ARTIFACTS.jobs, rereview: { state: "running", error: null } } }),
    );
    await ready();
    expect(within(outline()).getByRole("link", { name: /Your requests.*running…/ })).toBeInTheDocument();
    await go(/Your requests/);
    expect(screen.getByText(/Your requests is being generated/)).toBeInTheDocument();
    // The previous review is shown while the agent works: it is Jesinia's record.
    expect(screen.getByRole("region", { name: "Previous review, aligned by file and hunk" })).toBeInTheDocument();
  });

  it("shows no re-review pages on a first review", async () => {
    openPr(reviewed());
    await ready();
    expect(within(outline()).queryByRole("link", { name: /Your requests/ })).toBeNull();
  });
});

describe("Review the new head", () => {
  const calls = (all: Request[], what: string) =>
    all.filter((c) => c.method === "POST" && new URL(c.url).pathname === `${API}/${what}`);

  it("fetches the PR again first, then opens the head it found", async () => {
    let view = reviewed({ stale: true }, { head_sha: NEWER });
    const env = openPr(view, {
      [`GET ${API}`]: () => Response.json(view),
      [`POST ${API}/refresh`]: () => {
        view = reviewed({ round: null, artifacts: {} }, { head_sha: NEWER });
        return Response.json(view.pr);
      },
      [`POST ${API}/open`]: () =>
        Response.json({ state: "started", reason: "reviewing", round_id: 4, jobs: ["rereview"] }),
    });
    await ready();
    await waitFor(() => expect(calls(env.calls, "open")).toHaveLength(1));
    await userEvent.click(screen.getByRole("button", { name: "Review the new head" }));
    await waitFor(() => expect(calls(env.calls, "refresh")).toHaveLength(1));
    expect(calls(env.calls, "refresh")[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(calls(env.calls, "open")).toHaveLength(2));
    const [refresh] = calls(env.calls, "refresh");
    const second = calls(env.calls, "open")[1];
    expect(env.calls.indexOf(refresh as Request)).toBeLessThan(env.calls.indexOf(second as Request));
  });

  it("says when GitHub has nothing newer, and why a fetch failed", async () => {
    const env = openPr(reviewed({ stale: true }), {
      [`POST ${API}/refresh`]: () => Response.json(reviewed().pr),
    });
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Review the new head" }));
    expect(await screen.findByText(/GitHub shows no newer commits/)).toBeInTheDocument();
    expect(calls(env.calls, "refresh")).toHaveLength(1);
    expect(reviewed().pr.head_sha).toBe(HEAD);
  });

  it("keeps the review and names the cause when the fetch fails", async () => {
    openPr(reviewed({ stale: true }), {
      [`POST ${API}/refresh`]: () =>
        Response.json({ error: "GitHubUnavailable", detail: "GitHub is down" }, { status: 503 }),
    });
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Review the new head" }));
    expect(await screen.findByText(/Couldn't fetch the new head: GitHub is down/)).toBeInTheDocument();
  });
});

describe("the re-review's own rules", () => {
  const base = { side: "RIGHT" as const, start_line: null, resolved: false, now_start_line: null, outdated: false };
  const previous: PreviousReview = {
    source: "github",
    head_sha: "2".repeat(40),
    round_id: null,
    comments: [
      {
        ...base,
        ref: "c1",
        body: "On the old side.",
        path: PRORATION,
        line: 11,
        side: "LEFT",
        now_path: PRORATION,
        now_line: 11,
      },
      {
        ...base,
        ref: "c2",
        body: "A range.",
        path: PRORATION,
        line: 3,
        start_line: 1,
        now_path: PRORATION,
        now_line: 3,
      },
      { ...base, ref: "c3", body: "Whole file.", path: PRORATION, line: null, now_path: PRORATION, now_line: null },
      {
        ...base,
        ref: "c4",
        body: "Cites a hunk the patch lacks.",
        path: PRORATION,
        line: 400,
        now_path: PRORATION,
        now_line: 400,
      },
    ],
    steps: [],
    findings: [],
    placement_error: null,
  };
  const result: RereviewData = {
    requests: [
      { comment_ref: "c1", status: "partly", explanation: "Half.", hunks: [] },
      { comment_ref: "c4", status: "addressed", explanation: "x", hunks: [{ path: PRORATION, hunk: 9 }] },
    ],
    unanswered: ["c2", "c3"],
    new_hunks: [{ path: "src/new.py", hunk: 0 }],
    steps: [],
    coverage: { pct: 0, files_covered: 0, files_total: 0, not_walked: [], not_walked_total: 0, nothing_to_walk: true },
  };

  it("places each comment on the hunk holding its line, on the side it was written", () => {
    const files = reviewed().files;
    const groups = alignPrevious(previous, result, files);
    const proration = groups.find((g) => g.name === PRORATION);
    expect(proration?.rows.map((r) => [r.label, r.comments.map((c) => c.ref)])).toEqual([
      ["hunk 1", ["c2"]], // new lines 1–3
      ["hunk 2", ["c1"]], // old line 11
      ["elsewhere in the file", ["c3", "c4"]],
    ]);
    // A new hunk of a file GitHub didn't list has no row to put it in.
    expect(groups.find((g) => g.name === "src/new.py")?.rows).toEqual([]);
    expect(groups.some((g) => g.name === "The review")).toBe(false);
  });

  it("shows partly addressed, ranges and old sides, and an empty new walk", () => {
    render(
      <>
        <YourRequests previous={previous} rereview={result} files={reviewed().files} status={null} />
        <NewSinceLastLook rereview={result} files={null} layout="unified" status={null} />
      </>,
    );
    expect(screen.getAllByText("Partly addressed").length).toBeGreaterThan(0);
    expect(screen.getAllByText(`${PRORATION}:11 (old)`).length).toBeGreaterThan(0);
    expect(screen.getAllByText(`${PRORATION}:1–3`).length).toBeGreaterThan(0);
    expect(screen.getByText(/From your review of 2222222, on GitHub/)).toBeInTheDocument();
    expect(screen.getByText("Nothing new to walk")).toBeInTheDocument();
    expect(screen.getByText("No new change was walked through.")).toBeInTheDocument();
    expect(newChangesLine(null)).toBeNull();
  });

  it("names a step's file even when GitHub's list lacks it, and says when nothing was commented", () => {
    const walked: RereviewData = {
      ...result,
      requests: [],
      steps: [{ title: "Elsewhere", body: "b", hunks: [{ path: "src/gone.py", hunk: 0 }] }],
      coverage: {
        ...result.coverage,
        nothing_to_walk: false,
        pct: 50,
        not_walked: [{ path: "x.lock", hunk: null, reason: "lockfile" }],
      } as RereviewData["coverage"],
    };
    render(
      <>
        <YourRequests previous={{ ...previous, comments: [] }} rereview={null} files={null} status={<p>waiting</p>} />
        <NewSinceLastLook rereview={walked} files={null} layout="unified" status={null} />
      </>,
    );
    expect(screen.getByText("waiting")).toBeInTheDocument();
    expect(screen.getByText("Your previous review left no comments.")).toBeInTheDocument();
    expect(screen.getByText("src/gone.py")).toBeInTheDocument();
    expect(screen.getByText("x.lock")).toBeInTheDocument();
    expect(screen.getByText("lockfile")).toBeInTheDocument();
  });
});
