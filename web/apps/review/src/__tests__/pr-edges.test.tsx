// SPDX-License-Identifier: AGPL-3.0-or-later
// The PR view's other paths: failures and their next steps, chat, pending pages, odd diffs, and
// the rest of the posting dialog's server states.
import { fakeClient, type Routes } from "@jesinia/shell";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes, UP_TO_DATE } from "./fixtures";
import { API, ARTIFACTS, drafts, FILES, PATH, preview, reviewed, ROUND } from "./pr-fixtures";

function setup(view: PrView = reviewed(), over: Routes = {}) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr]), {
      [`GET ${API}`]: () => Response.json(view),
      [`GET ${API}/drafts`]: () => Response.json(drafts()),
      [`PUT ${API}/drafts`]: async (req: Request) => {
        const body = (await req.clone().json()) as ReturnType<typeof drafts>;
        return Response.json(drafts({ summary: body.summary, drafts: body.drafts }));
      },
      [`POST ${API}/post/preview`]: async (req: Request) => {
        const { event } = (await req.clone().json()) as { event: "APPROVE" | "COMMENT" | "REQUEST_CHANGES" };
        return Response.json(preview({ event }));
      },
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const toolbar = () => screen.getByRole("group", { name: "Review tools" });
const actions = () => screen.getByRole("group", { name: "Page actions" });
const go = async (name: RegExp) => userEvent.click(within(outline()).getByRole("link", { name }));
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const toast = async (text: RegExp) => expect((await screen.findAllByText(text)).length).toBeGreaterThan(0);
const error = (status: number, error: string, detail: string, more: object = {}) =>
  Response.json({ error, detail, ...more }, { status });

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("opening", () => {
  it("a failed open says why and tries again", async () => {
    let attempt = 0;
    const { calls } = setup(reviewed(), {
      [`POST ${API}/open`]: () => {
        attempt += 1;
        return attempt === 1 ? error(503, "GitHubUnavailable", "GitHub is down") : Response.json(UP_TO_DATE);
      },
    });
    expect(await screen.findByText("Couldn't start the review: GitHub is down.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByText(/Couldn't start the review/)).toBeNull());
    const keys = sent(calls, "POST", `${API}/open`).map((r) => r.headers.get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]); // the outcome of a 503 is unknown: the same action, the same key
  });

  it("an open already running, and a retry or Review anyway that fails", async () => {
    setup(reviewed({ round: null, artifacts: {}, files: null }, { recommendation: null }), {
      [`POST ${API}/open`]: () =>
        Response.json({ state: "waiting_ci", reason: "CI is still running", round_id: null, jobs: [] }),
      [`POST ${API}/review-anyway`]: () => error(409, "ReviewNotAllowedError", "reload it first"),
      [`GET ${API}/drafts`]: () => error(404, "NoReviewRound", "no round"),
    });
    expect(await screen.findByText("CI is still running")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review anyway" }));
    await toast(/Couldn't review it anyway: reload it first\./);
    expect(actions().querySelector("button[disabled]")).not.toBeNull(); // no round: nothing to post
    await go(/Your comments/);
    expect(screen.getByText("Drafts start once the review has a round.")).toBeInTheDocument();
    await go(/Recommendation/);
    expect(screen.getByText(/No verdict yet: the pre-review hasn't finished/)).toBeInTheDocument();
  });

  it("says a head is being reviewed, and pages say what is coming", async () => {
    const jobs = { overview: { state: "running", error: null } };
    setup(reviewed({ artifacts: { jobs } }), {
      [`POST ${API}/open`]: () =>
        Response.json({ state: "running", reason: "this head is being reviewed", round_id: 3, jobs: [] }),
      [`POST ${API}/retry`]: () => error(500, "ReviewError", "workspace is locked"),
    });
    expect(await screen.findByText(/This head is being reviewed/)).toBeInTheDocument();
    expect(screen.getByText("Overview is being generated: it shows here when it finishes.")).toBeInTheDocument();
    await go(/Background/);
    expect(screen.getByText("Background hasn't been generated for this review yet.")).toBeInTheDocument();
    await go(/Walkthrough/);
    expect(screen.getByRole("heading", { level: 2, name: "Walkthrough" })).toBeInTheDocument();
    await go(/Findings/);
    expect(screen.getByRole("heading", { level: 2, name: "Findings" })).toBeInTheDocument();
  });

  it("Retry that fails says why", async () => {
    const jobs = { ...ARTIFACTS.jobs, quiz: { state: "failed", error: null } };
    const { quiz, ...rest } = ARTIFACTS;
    void quiz;
    setup(reviewed({ artifacts: { ...rest, jobs } }), {
      [`POST ${API}/retry`]: () => error(409, "PrNotOpenError", "the PR is merged"),
    });
    await ready();
    await go(/Quiz.*failed/);
    expect(screen.getByText("Quiz failed: no cause given.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await toast(/Couldn't retry: the PR is merged\./);
  });
});

describe("the toolbar and outline", () => {
  it("a depth switch or a walk request that fails says why", async () => {
    let opens = 0;
    setup(reviewed(), {
      [`POST ${API}/open`]: () => {
        opens += 1;
        if (opens === 1) return Response.json(UP_TO_DATE);
        if (opens === 2) return error(409, "PrNotOpenError", "the PR is closed");
        if (opens === 3) return Response.json({ state: "running", reason: "running", round_id: 3, jobs: [] });
        return error(409, "PrNotOpenError", "the PR is closed");
      },
    });
    await ready();
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Switch to quick" }));
    await toast(/Couldn't switch to a quick review: the PR is closed\./);
    await go(/Not walked through/);
    await userEvent.click(screen.getByRole("button", { name: "Walk everything" }));
    expect(await screen.findByText(/The walkthrough is running now/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Walk everything" }));
    expect(await screen.findByText(/Couldn't ask for the rest of the walk: the PR is closed\./)).toBeInTheDocument();
  });

  it("Mark read toggles; read ticks survive a corrupt store; Contents opens the outline", async () => {
    localStorage.setItem("jesinia.review.read.acme/web#3@3", "{not json");
    setup();
    await ready();
    await go(/Recommendation/);
    await userEvent.click(within(actions()).getByRole("button", { name: "Mark read" }));
    expect(within(outline()).getByText(/Read 1 of 10/)).toBeInTheDocument();
    await userEvent.click(within(actions()).getByRole("button", { name: "Mark unread" }));
    expect(within(outline()).getByText(/Read 0 of 10/)).toBeInTheDocument();
    const contents = within(toolbar()).getByRole("button", { name: "Contents" });
    await userEvent.click(contents);
    expect(contents).toHaveAttribute("aria-expanded", "true");
    expect(outline()).toHaveAttribute("data-open", "true");
  });

  it("a review that can't be stored as read still ticks for this visit", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    setup();
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Mark read" }));
    expect(within(outline()).getByText(/Read 1 of 10/)).toBeInTheDocument();
  });

  it("Done without posting finishes the round; a failure says why", async () => {
    let attempt = 0;
    const { calls } = setup(reviewed(), {
      [`POST ${API}/done`]: () => {
        attempt += 1;
        return attempt === 1
          ? error(404, "NoReviewRound", "no round to finish")
          : Response.json({ ...ROUND, done: true });
      },
    });
    await ready();
    await go(/Recommendation/);
    await userEvent.click(screen.getByRole("button", { name: "Done without posting" }));
    await toast(/Couldn't mark it done: no round to finish\./);
    await userEvent.click(screen.getByRole("button", { name: "Done without posting" }));
    await toast(/Acme\/web#3 is done; nothing was posted\./);
    expect(sent(calls, "POST", `${API}/done`)).toHaveLength(2);
  });

  it("your own PR's verdict reads as ready to request review", async () => {
    setup(
      reviewed({ artifacts: { ...ARTIFACTS, verdict: { verdict: "approve", reasons: [] } } }, { author_kind: "you" }),
    );
    await ready();
    expect(within(toolbar()).getByText("✓ Ready to request review")).toBeInTheDocument();
  });
});

describe("chat", () => {
  it("⌘J toggles it; Close closes it; a session that can't start says why; no runner is fine", async () => {
    setup(reviewed(), {
      "GET /api/agents/runners": () => error(500, "x", "down"),
      "POST /api/agents/sessions": () => error(400, "x", "unknown runner"),
    });
    await ready();
    fireEvent.keyDown(window, { key: "j", metaKey: true });
    const rail = await screen.findByRole("complementary", { name: "Ask about this PR" });
    expect(await within(rail).findByText(/No agent is available/)).toBeInTheDocument();
    await userEvent.click(within(rail).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("complementary", { name: "Ask about this PR" })).toBeNull();
    fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    expect(await screen.findByRole("complementary", { name: "Ask about this PR" })).toBeInTheDocument();
  });

  it("a session that can't start says why", async () => {
    setup(reviewed(), { "POST /api/agents/sessions": () => error(400, "x", "unknown runner") });
    await ready();
    await userEvent.click(within(toolbar()).getByRole("button", { name: /Ask/ }));
    const rail = await screen.findByRole("complementary", { name: "Ask about this PR" });
    await userEvent.type(within(rail).getByRole("textbox", { name: "Message" }), "Hi");
    await userEvent.click(within(rail).getByRole("button", { name: "Send" }));
    await toast(/Couldn't start the chat/);
  });
});

describe("odd diffs and lists", () => {
  it("a file GitHub doesn't show, a file it doesn't list, and a not-walked list cut short", async () => {
    const files = [{ ...FILES[0], patch: null }, FILES[1]] as PrView["files"];
    const walkthrough = {
      steps: [
        { title: "Binary", body: "b", hunks: [{ path: "src/billing/proration.py", hunk: 0 }] },
        { title: "Missing", body: "b", hunks: [{ path: "nowhere.py", hunk: 0 }] },
      ],
      skipped: [],
    };
    const walk = { ...ARTIFACTS.coverage.walkthrough, not_walked_total: 7 };
    setup(
      reviewed({
        files,
        artifacts: { ...ARTIFACTS, walkthrough, coverage: { ...ARTIFACTS.coverage, walkthrough: walk } },
      }),
    );
    await ready();
    await go(/1\. Binary/);
    expect(screen.getByText(/GitHub doesn't show this file's diff/)).toBeInTheDocument();
    await go(/2\. Missing/);
    expect(screen.getByText("GitHub's file list has no nowhere.py.")).toBeInTheDocument();
    await go(/Not walked through/);
    expect(screen.getByText("…and 5 more not listed here.")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Show" })[1] as HTMLElement);
    expect(screen.getByText("GitHub's file list has no docs/billing.md.")).toBeInTheDocument();
  });

  it("a not-walked file whose diff isn't cached says so", async () => {
    setup(reviewed({ files: null }));
    await ready();
    await go(/Not walked through/);
    await userEvent.click(screen.getAllByRole("button", { name: "Show" })[0] as HTMLElement);
    expect(screen.getByText(/The diff isn't cached yet/)).toBeInTheDocument();
  });

  it("says when nothing was left out", async () => {
    const walk = { ...ARTIFACTS.coverage.walkthrough, not_walked: [], not_walked_total: 0, pct: 100 };
    setup(reviewed({ artifacts: { ...ARTIFACTS, coverage: { ...ARTIFACTS.coverage, walkthrough: walk } } }));
    await ready();
    await go(/Not walked through/);
    expect(screen.getByText("Nothing was left out.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Walk everything" })).toBeNull();
  });

  it("a background with nothing new, an overview without sources, a critical finding", async () => {
    const artifacts = {
      ...ARTIFACTS,
      overview: { ...ARTIFACTS.overview, sources: [] },
      background: { sections: [{ title: "Only prose", body: "x", code: [], kb: [] }] },
      findings: { findings: [{ ...ARTIFACTS.findings.findings[0], severity: "critical" }] },
      evidence: { ...ARTIFACTS.evidence, tests: null, correctness: null, correctness_coverage: null, findings: {} },
      recommendation: undefined,
    };
    setup(reviewed({ artifacts }));
    await ready();
    expect(screen.queryByRole("heading", { name: "Drawn from" })).toBeNull();
    await go(/Background/);
    expect(screen.queryByRole("list", { name: "Code areas" })).toBeNull();
    await go(/Findings/);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    await go(/Recommendation/);
    expect(screen.getByText("Tests: none found by the agent")).toBeInTheDocument();
    expect(screen.getByText("Correctness pass: not run")).toBeInTheDocument();
    expect(screen.getByText("Findings: none")).toBeInTheDocument();
  });

  it("an empty background says so", async () => {
    setup(reviewed({ artifacts: { ...ARTIFACTS, background: { sections: [] } } }));
    await ready();
    await go(/Background/);
    expect(screen.getByText(/Nothing to explain/)).toBeInTheDocument();
  });
});

describe("drafts and posting, other paths", () => {
  it("drafts that can't be read say why; a refused summary is named; in-body drafts say so", async () => {
    let reads = 0;
    setup(reviewed(), {
      [`GET ${API}/drafts`]: () => {
        reads += 1;
        return reads === 1
          ? error(500, "ReviewError", "locked")
          : Response.json(
              drafts({ drafts: drafts().drafts.map((d) => ({ ...d, in_body: true, origin_finding: null })) }),
            );
      },
      [`PUT ${API}/drafts`]: () =>
        error(422, "InvalidDraftContent", "the summary contains Jesinia's posting marker text", { field: "summary" }),
    });
    await ready();
    await go(/Your comments/);
    expect(await screen.findByText("Couldn't read the drafts: locked.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Read the drafts again" }));
    expect(await screen.findByText(/Posts in the review body/)).toBeInTheDocument();
    const summary = screen.getByRole("textbox", { name: "Review summary" });
    await userEvent.type(summary, "x");
    fireEvent.blur(summary);
    expect(await screen.findByText(/The summary can't be saved: the summary contains/)).toBeInTheDocument();
    expect(summary).toHaveAttribute("aria-invalid", "true");
  });

  it("a one-line finding drafts a one-line comment, on the recommendation page of a quick review", async () => {
    const { background, metrics, quiz, ...rest } = ARTIFACTS;
    void background;
    void metrics;
    void quiz;
    const { calls } = setup(reviewed({ round: { ...ROUND, depth: "quick" }, artifacts: rest }, { depth: "quick" }));
    await screen.findByRole("heading", { level: 2, name: "Explanation" });
    await go(/1\. The new prorate function/);
    await userEvent.click(within(actions()).getByRole("button", { name: "Draft comment on this finding" }));
    expect(screen.getByRole("heading", { level: 2, name: "Recommendation" })).toBeInTheDocument();
    const own = await screen.findByRole("textbox", { name: "Comment on src/billing/proration.py:1 (new)" });
    await userEvent.type(own, "Rename?");
    fireEvent.blur(own);
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1));
    const body = (await sent(calls, "PUT", `${API}/drafts`)[0]?.clone().json()) as {
      drafts: Record<string, unknown>[];
    };
    expect(body.drafts[1]).toMatchObject({ line: 1, start_line: null, start_side: null });
  });

  it("the dialog: Cancel and Escape close it; a preview that fails tries again; Edit drafts goes there", async () => {
    let previews = 0;
    setup(reviewed(), {
      [`POST ${API}/post/preview`]: () => {
        previews += 1;
        return previews === 1
          ? error(502, "GitHubError", "GitHub said 502")
          : Response.json(preview({ event: "COMMENT", body: "" }));
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    let box = await screen.findByRole("dialog", { name: /on GitHub/ });
    expect(await within(box).findByText("Couldn't read what would be posted: GitHub said 502.")).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "Try again" }));
    expect(await within(box).findByText("(none)")).toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    box = await screen.findByRole("dialog", { name: /on GitHub/ });
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    box = await screen.findByRole("dialog", { name: /on GitHub/ });
    await within(box).findByText("(none)");
    await userEvent.click(within(box).getByRole("button", { name: "Edit drafts" }));
    expect(screen.getByRole("heading", { level: 2, name: "Your comments" })).toBeInTheDocument();
  });

  it("a failed post keeps the drafts and retries with the same key; a changed preview is read again", async () => {
    let posts = 0;
    const { calls } = setup(reviewed(), {
      [`POST ${API}/post`]: () => {
        posts += 1;
        if (posts === 1) return error(502, "PostingFailed", "GitHub didn't confirm the review");
        if (posts === 2) return error(409, "PreviewOutdated", "the review changed since its preview");
        if (posts === 3) return error(409, "CommentOnlyOnOwnPr", "on your own PR, only a comment can be posted");
        return Response.json({ review_url: "javascript:alert(1)" });
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Request changes…" }));
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    const go = async (name: string) => {
      const button = await within(box).findByRole("button", { name });
      await waitFor(() => expect(button).toBeEnabled());
      await userEvent.click(button);
    };
    await go("Request changes");
    expect(
      await within(box).findByText(/Couldn't post: GitHub didn't confirm the review\. Your drafts are kept\./),
    ).toBeInTheDocument();
    await go("Request changes");
    expect(await within(box).findByText(/What would be posted changed since this preview/)).toBeInTheDocument();
    await go("Request changes");
    expect(await within(box).findByRole("heading", { name: "Comment on Acme/web#3 on GitHub" })).toBeInTheDocument();
    const keys = sent(calls, "POST", `${API}/post`).map((r) => r.headers.get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]); // a 502: the same post again
    expect(keys[2]).not.toBe(keys[1]); // answered: a new action
  });

  it("approving against the evidence shows the server's evidence; BLOCKING in the body is named", async () => {
    setup(reviewed({ artifacts: { ...ARTIFACTS, verdict: undefined } }), {
      [`POST ${API}/post/preview`]: () =>
        Response.json(
          preview({
            body: "Blocking: see below",
            comments: [],
            warnings: ["blocking_comment", "approve_against_evidence"],
            notes: ["Approving goes against the evidence: CI: missing"],
          }),
        ),
      [`POST ${API}/post`]: () =>
        error(409, "EarlierReviewPosted", "an earlier version is on GitHub", {
          review_url: "javascript:alert(1)",
          event: "COMMENT",
        }),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    const evidence = await within(box).findByRole("group", { name: "Jesinia's verdict isn't Approve" });
    expect(evidence).toHaveTextContent("Approving goes against the evidence: CI: missing.");
    expect(within(box).getByRole("group", { name: /says BLOCKING/ })).toHaveTextContent(/in the review body/);
    for (const tick of within(box).getAllByRole("checkbox")) await userEvent.click(tick);
    await userEvent.click(within(box).getByRole("button", { name: "Approve" }));
    expect(await within(box).findByText(/your newer edits were not sent/)).toBeInTheDocument();
    expect(within(box).queryByRole("link", { name: "the earlier review" })).toBeNull(); // not an http(s) link
    expect(within(box).getByRole("button", { name: "Approve" })).toBeDisabled();
    await userEvent.click(within(box).getAllByRole("checkbox")[0] as HTMLElement);
    await userEvent.click(within(box).getAllByRole("button", { name: "Request changes instead" })[0] as HTMLElement);
    expect(
      await within(box).findByRole("heading", { name: "Request changes on Acme/web#3 on GitHub" }),
    ).toBeInTheDocument();
  });
});
