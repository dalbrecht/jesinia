// SPDX-License-Identifier: AGPL-3.0-or-later
// Drafts and posting (spec §7, design pass `followups.html`): the drafts editor, and the
// confirmation that shows exactly what will be posted with a confirm box per warning.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes } from "./fixtures";
import { API, drafts, HEAD, NEWER, PATH, preview, reviewed, ROUND } from "./pr-fixtures";

type Body = Record<string, unknown>;

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
      [`POST ${API}/post`]: () =>
        Response.json({ review_url: "https://github.com/Acme/web/pull/3#pullrequestreview-9" }),
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const actions = () => screen.getByRole("group", { name: "Page actions" });
const go = async (name: RegExp) => userEvent.click(within(outline()).getByRole("link", { name }));
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const draftBox = () => screen.getByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
const dialog = () => screen.findByRole("dialog", { name: /on GitHub/ });

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("drafts", () => {
  it("editing a draft persists it, keyed; a retry of the same edit reuses its key, a new edit gets a new one", async () => {
    let attempt = 0;
    const { calls } = setup(reviewed(), {
      [`PUT ${API}/drafts`]: async (req: Request) => {
        attempt += 1;
        if (attempt === 1)
          return Response.json({ error: "ReviewError", detail: "database is locked" }, { status: 503 });
        const body = (await req.clone().json()) as ReturnType<typeof drafts>;
        return Response.json(drafts({ summary: body.summary, drafts: body.drafts }));
      },
    });
    await ready();
    await go(/Your comments/);
    await userEvent.clear(draftBox());
    await userEvent.type(draftBox(), "Round per side, as invoices do.");
    fireEvent.blur(draftBox());
    expect(await screen.findByText(/Couldn't save the drafts: database is locked/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save again" }));
    expect(await screen.findByText("Drafts saved.")).toBeInTheDocument();
    const puts = sent(calls, "PUT", `${API}/drafts`);
    expect(puts).toHaveLength(2);
    const body = (await puts[1]?.clone().json()) as { summary: string; drafts: Body[] };
    expect(body.summary).toBe("Looks good overall.");
    expect(body.drafts[0]).toMatchObject({ id: 1, body: "Round per side, as invoices do.", line: 15, start_line: 14 });
    expect(body.drafts[0]).not.toHaveProperty("in_body");
    expect(puts[1]?.headers.get("Idempotency-Key")).toBe(puts[0]?.headers.get("Idempotency-Key"));
    await userEvent.type(screen.getByRole("textbox", { name: "Review summary" }), " Thanks!");
    fireEvent.blur(screen.getByRole("textbox", { name: "Review summary" }));
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(3));
    expect(sent(calls, "PUT", `${API}/drafts`)[2]?.headers.get("Idempotency-Key")).not.toBe(
      puts[0]?.headers.get("Idempotency-Key"),
    );
  });

  it("deletes a draft, and writes one of its own on a finding", async () => {
    const { calls } = setup();
    await ready();
    await go(/2\. Posting to the ledger/);
    await userEvent.click(within(actions()).getByRole("button", { name: "Draft comment on this finding" }));
    const own = await screen.findByRole("textbox", { name: "Comment on src/billing/proration.py:14–15 (new)" });
    expect(own).toHaveFocus();
    await userEvent.type(own, "Is this covered?");
    fireEvent.blur(own);
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1));
    const added = (await sent(calls, "PUT", `${API}/drafts`)[0]?.clone().json()) as { drafts: Body[] };
    expect(added.drafts).toHaveLength(2);
    expect(added.drafts[1]).toMatchObject({
      path: "src/billing/proration.py",
      line: 15,
      side: "RIGHT",
      start_line: 14,
      body: "Is this covered?",
    });
    expect(added.drafts[1]).not.toHaveProperty("id");
    await userEvent.click(screen.getAllByRole("button", { name: /^Delete comment on/ })[0] as HTMLElement);
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(2));
    const kept = (await sent(calls, "PUT", `${API}/drafts`)[1]?.clone().json()) as { drafts: Body[] };
    expect(kept.drafts.map((d) => d.body)).toEqual(["Is this covered?"]);
  });

  it("says findings are still running and where drafts post isn't known yet", async () => {
    setup(reviewed(), {
      [`GET ${API}/drafts`]: () =>
        Response.json(drafts({ findings_pending: true, files_pending: true, drafts: drafts().drafts.map((d) => ({ ...d, in_body: null, outdated: true })) })),
    }); // prettier-ignore
    await ready();
    await go(/Your comments/);
    expect(screen.getByText(/Findings are still running: their drafts are added next to yours/)).toBeInTheDocument();
    expect(screen.getByText(/GitHub's file list for this head isn't cached yet/)).toBeInTheDocument();
    expect(screen.getByText("Outdated")).toBeInTheDocument();
  });

  it("names the draft the server refuses", async () => {
    setup(reviewed(), {
      [`PUT ${API}/drafts`]: () =>
        Response.json(
          { error: "InvalidDraftContent", detail: "draft 1 contains Jesinia's posting marker text; remove it" },
          { status: 422 },
        ),
    });
    await ready();
    await go(/Your comments/);
    await userEvent.type(draftBox(), " <!-- jesinia-posting:1 -->");
    fireEvent.blur(draftBox());
    expect(
      await screen.findByText(
        "Draft 1 (src/billing/proration.py:14–15) can't be saved: draft 1 contains Jesinia's posting marker text; remove it.",
      ),
    ).toBeInTheDocument();
    expect(draftBox()).toHaveAttribute("aria-invalid", "true");
  });
});

describe("posting", () => {
  it("shows exactly what will be posted; a BLOCKING warning blocks Approve until confirmed", async () => {
    const { calls } = setup(reviewed(), {
      [`POST ${API}/post/preview`]: async (req: Request) => {
        const { event } = (await req.clone().json()) as { event: "APPROVE" | "REQUEST_CHANGES" };
        return Response.json(
          preview({
            event,
            warnings: event === "APPROVE" ? ["blocking_comment"] : [],
            notes: ["Jesinia adds an invisible marker"],
          }),
        );
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await dialog();
    expect(within(box).getByRole("heading", { name: "Approve Acme/web#3 on GitHub" })).toBeInTheDocument();
    expect(within(box).getByText("Looks good overall.")).toBeInTheDocument();
    expect(within(box).getByText("src/billing/proration.py:14–15")).toBeInTheDocument();
    expect(within(box).getByText("nonblocking: rounding")).toBeInTheDocument();
    expect(within(box).getByText("Jesinia adds an invisible marker.")).toBeInTheDocument();
    const warning = within(box).getByRole("group", { name: "You're approving, but the review says BLOCKING" });
    expect(warning).toHaveTextContent(
      /in any case, anywhere in the text, even inside a longer word such as “nonblocking”/,
    );
    expect(warning).toHaveTextContent(/src\/billing\/proration\.py:14–15/);
    const approve = within(box).getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    await userEvent.click(within(warning).getByRole("checkbox", { name: "Approve anyway" }));
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    await waitFor(() => expect(sent(calls, "POST", `${API}/post`)).toHaveLength(1));
    const post = sent(calls, "POST", `${API}/post`)[0];
    expect(await post?.clone().json()).toEqual({ event: "APPROVE", confirm: ["blocking_comment"], digest: "digest-1" });
    expect(post?.headers.get("Idempotency-Key")).toBeTruthy();
    expect((await screen.findAllByText(/Posted to GitHub/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Request changes instead switches the action and previews it again", async () => {
    const { calls } = setup(reviewed(), {
      [`POST ${API}/post/preview`]: async (req: Request) => {
        const { event } = (await req.clone().json()) as { event: "APPROVE" | "REQUEST_CHANGES" };
        return Response.json(preview({ event, warnings: event === "APPROVE" ? ["blocking_comment"] : [] }));
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await dialog();
    await userEvent.click(within(box).getByRole("button", { name: "Request changes instead" }));
    expect(
      await within(box).findByRole("heading", { name: "Request changes on Acme/web#3 on GitHub" }),
    ).toBeInTheDocument();
    expect(within(box).getByRole("button", { name: "Request changes" })).toBeEnabled();
    expect(sent(calls, "POST", `${API}/post/preview`)).toHaveLength(2);
  });

  it("warns of newer commits, with Review the new commits first", async () => {
    const view = reviewed({ stale: true }, { head_sha: NEWER });
    const { calls } = setup(view, {
      [`POST ${API}/post/preview`]: () => Response.json(preview({ event: "COMMENT", warnings: ["newer_head"] })),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await dialog();
    const warning = within(box).getByRole("group", { name: "There are newer commits" });
    expect(warning).toHaveTextContent(
      `This review covers ${HEAD.slice(0, 7)}; the PR head is now ${NEWER.slice(0, 7)}.`,
    );
    expect(within(box).getByRole("button", { name: "Comment" })).toBeDisabled();
    await userEvent.click(within(warning).getByRole("checkbox", { name: "Comment anyway" }));
    expect(within(box).getByRole("button", { name: "Comment" })).toBeEnabled();
    await userEvent.click(within(warning).getByRole("button", { name: "Review the new commits first" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(sent(calls, "POST", `${API}/open`).length).toBeGreaterThanOrEqual(2));
  });

  it("on your own PR only Comment is offered", async () => {
    const own = reviewed({}, { author: "me", author_kind: "you" });
    setup(own, {
      [`POST ${API}/post/preview`]: () =>
        Response.json(preview({ event: "COMMENT", allowed_events: ["COMMENT"], warnings: [] })),
    });
    await ready();
    expect(within(actions()).queryByRole("button", { name: "Approve…" })).toBeNull();
    expect(within(actions()).queryByRole("button", { name: "Request changes…" })).toBeNull();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await dialog();
    expect(
      within(box).getByText(/GitHub doesn't let you approve or request changes on your own PR/),
    ).toBeInTheDocument();
    expect(within(box).getByRole("button", { name: "Comment" })).toBeEnabled();
  });

  it("a server's own-PR warning offers Comment instead, and never Approve", async () => {
    const { calls } = setup(reviewed(), {
      [`POST ${API}/post/preview`]: async (req: Request) => {
        const { event } = (await req.clone().json()) as { event: "APPROVE" | "COMMENT" };
        return event === "APPROVE"
          ? Response.json(preview({ event, warnings: ["own_pr_comment_only"], allowed_events: ["COMMENT"] }))
          : Response.json(preview({ event, allowed_events: ["COMMENT"] }));
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await dialog();
    const warning = within(box).getByRole("group", { name: "This is your own PR" });
    expect(within(box).getByRole("button", { name: "Approve" })).toBeDisabled();
    await userEvent.click(within(warning).getByRole("button", { name: "Comment instead" }));
    expect(await within(box).findByRole("button", { name: "Comment" })).toBeEnabled();
    expect(sent(calls, "POST", `${API}/post/preview`)).toHaveLength(2);
  });

  it("approving against the evidence shows the verdict and what is missing", async () => {
    setup(reviewed(), {
      [`POST ${API}/post/preview`]: () => Response.json(preview({ warnings: ["approve_against_evidence"] })),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await dialog();
    const warning = within(box).getByRole("group", { name: "Jesinia's verdict isn't Approve" });
    expect(warning).toHaveTextContent("Jesinia's verdict: ? Needs a closer look.");
    expect(warning).toHaveTextContent("the correctness pass found 1 important issue");
    expect(within(warning).getByRole("checkbox", { name: "Approve anyway" })).not.toBeChecked();
  });

  it("an earlier review on GitHub: links it and says the newer edits were not sent", async () => {
    setup(reviewed(), {
      [`POST ${API}/post`]: () =>
        Response.json(
          {
            error: "EarlierReviewPosted",
            detail: "an earlier version of this review (COMMENT) is already on GitHub",
            review_url: "https://github.com/Acme/web/pull/3#pullrequestreview-1",
            event: "COMMENT",
          },
          { status: 409 },
        ),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await dialog();
    await userEvent.click(within(box).getByRole("button", { name: "Comment" }));
    expect(await within(box).findByText(/your newer edits were not sent/)).toBeInTheDocument();
    expect(within(box).getByRole("link", { name: "the earlier review" })).toHaveAttribute(
      "href",
      "https://github.com/Acme/web/pull/3#pullrequestreview-1",
    );
  });

  it("a post still uncertain waits for Retry-After, then retries with the same key", async () => {
    let attempt = 0;
    const { calls } = setup(reviewed(), {
      [`POST ${API}/post`]: () => {
        attempt += 1;
        if (attempt === 1) {
          return Response.json(
            { error: "PostingStillUncertain", detail: "an earlier attempt may still appear on GitHub", retry_after: 2 },
            { status: 409, headers: { "Retry-After": "2" } },
          );
        }
        return Response.json({ review_url: "https://github.com/Acme/web/pull/3#pullrequestreview-9" });
      },
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await dialog();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await userEvent.click(within(box).getByRole("button", { name: "Comment" }));
    expect(await within(box).findByText(/Trying again in 2 s with the same request/)).toBeInTheDocument();
    expect(sent(calls, "POST", `${API}/post`)).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await waitFor(() => expect(sent(calls, "POST", `${API}/post`)).toHaveLength(2));
    const keys = sent(calls, "POST", `${API}/post`).map((r) => r.headers.get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]);
    expect((await screen.findAllByText(/Posted to GitHub/)).length).toBeGreaterThan(0);
  });

  it("a body over GitHub's limit shows the length and the limit", async () => {
    setup(reviewed(), {
      [`POST ${API}/post/preview`]: () =>
        Response.json(
          {
            error: "ReviewBodyTooLong",
            detail:
              "the review body is 70,120 characters; GitHub allows 65,536: shorten the drafts or move some inline",
          },
          { status: 422 },
        ),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await dialog();
    expect(
      await within(box).findByText(
        /The review body is too long: the review body is 70,120 characters; GitHub allows 65,536/,
      ),
    ).toBeInTheDocument();
    expect(within(box).getByRole("button", { name: "Comment" })).toBeDisabled();
    await userEvent.click(within(box).getByRole("button", { name: "Edit drafts" }));
    expect(screen.getByRole("heading", { level: 2, name: "Your comments" })).toBeInTheDocument();
  });

  it("a posted round says so and links the review", async () => {
    setup(
      reviewed({ round: { ...ROUND, posted_review_url: "https://github.com/Acme/web/pull/3#pullrequestreview-9" } }),
    );
    await ready();
    expect(screen.getByRole("link", { name: "Posted on GitHub ↗" })).toHaveAttribute(
      "href",
      "https://github.com/Acme/web/pull/3#pullrequestreview-9",
    );
  });
});
