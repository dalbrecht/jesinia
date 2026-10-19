// SPDX-License-Identifier: AGPL-3.0-or-later
// Task 9 fix round 1: safe draft saves (I1, I2, R59), stable tabs (I3, R60), the posting
// dialog's focus (I5), visible hidden characters (I6, R62), and the smaller fixes.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { WINDOW_FROM } from "../pr/Outline";
import { LINES_SHOWN } from "../pr/Walkthrough";
import type { PrView } from "../api";
import { queue, routes, row } from "./fixtures";
import { API, ARTIFACTS, drafts, FILES, PATH, preview, reviewed } from "./pr-fixtures";

type Put = { summary: string; drafts: { body: string }[] };

function setup(view: PrView = reviewed(), over: Routes = {}, extraRows = [] as ReturnType<typeof row>[]) {
  window.history.replaceState(null, "", PATH);
  const env = fakeClient(
    routes(queue([view.pr, ...extraRows]), {
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
const actions = () => screen.getByRole("group", { name: "Page actions" });
const go = async (name: RegExp) => userEvent.click(within(outline()).getByRole("link", { name }));
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const draftBox = () => screen.getByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
const summaryBox = () => screen.getByRole("textbox", { name: "Review summary" });
const workspace = () => screen.getByRole("navigation", { name: "Open reviews" });

/**
 * Enters text the way a paste does: one input event. Typing per keystroke re-renders the whole
 * app each time, and on a loaded CI runner long strings took tests past the 5 s timeout.
 */
async function enter(field: HTMLElement, text: string): Promise<void> {
  await userEvent.click(field);
  await userEvent.paste(text);
}

/** A PUT route that answers only when the test says so. */
function deferredPuts() {
  const pending: { body: Put; key: string | null; resolve: () => void }[] = [];
  const route = async (req: Request) => {
    const body = (await req.clone().json()) as Put;
    await new Promise<void>((resolve) => pending.push({ body, key: req.headers.get("Idempotency-Key"), resolve }));
    return Response.json(drafts({ summary: body.summary, drafts: body.drafts as ReturnType<typeof drafts>["drafts"] }));
  };
  return { pending, route };
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("draft saves (I1, I2, R59)", () => {
  it("text typed while a save is in flight is kept, then saved next", async () => {
    const puts = deferredPuts();
    const { calls } = setup(reviewed(), { [`PUT ${API}/drafts`]: puts.route });
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " Edited.");
    fireEvent.blur(draftBox());
    await waitFor(() => expect(puts.pending).toHaveLength(1));
    await enter(summaryBox(), " TYPED-DURING-SAVE");
    await act(async () => puts.pending[0]?.resolve());
    expect(summaryBox()).toHaveValue("Looks good overall. TYPED-DURING-SAVE");
    expect(draftBox()).toHaveValue("Rounding happens once. Edited.");
    fireEvent.blur(summaryBox());
    await waitFor(() => expect(puts.pending).toHaveLength(2));
    expect(puts.pending[1]?.body.summary).toBe("Looks good overall. TYPED-DURING-SAVE");
    await act(async () => puts.pending[1]?.resolve());
    expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(2);
  });

  it("saves one at a time, and different content never reuses a key", async () => {
    const puts = deferredPuts();
    setup(reviewed(), { [`PUT ${API}/drafts`]: puts.route });
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " one");
    fireEvent.blur(draftBox());
    await waitFor(() => expect(puts.pending).toHaveLength(1));
    await enter(draftBox(), " two");
    fireEvent.blur(draftBox());
    fireEvent.blur(draftBox());
    await new Promise((r) => setTimeout(r, 20));
    expect(puts.pending).toHaveLength(1); // the second waits for the first
    await act(async () => puts.pending[0]?.resolve());
    await waitFor(() => expect(puts.pending).toHaveLength(2));
    await act(async () => puts.pending[1]?.resolve());
    expect(puts.pending.map((p) => p.body.drafts[0]?.body)).toEqual([
      "Rounding happens once. one",
      "Rounding happens once. one two",
    ]);
    expect(puts.pending[1]?.key).not.toBe(puts.pending[0]?.key);
  });

  it("saves on its own after the author stops typing for a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = setup();
    await ready();
    await go(/Your comments/);
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    await user.type(draftBox(), " typed");
    expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1));
  });

  it("saves when the window is hidden, and guards unload only while something is unsaved", async () => {
    const { calls } = setup();
    await ready();
    await go(/Your comments/);
    const leave = () => {
      const e = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    };
    expect(leave()).toBe(false);
    await enter(draftBox(), " unsaved");
    expect(leave()).toBe(true);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    await waitFor(() => expect(leave()).toBe(false));
  });

  it("Approve after an edit saves first, then previews the saved drafts", async () => {
    const { calls } = setup();
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " BLOCKING");
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    await screen.findByRole("dialog", { name: /on GitHub/ });
    const order = calls
      .filter((c) => ["PUT", "POST"].includes(c.method) && /drafts|preview/.test(c.url))
      .map((c) => `${c.method} ${new URL(c.url).pathname.split("/").pop()}`);
    expect(order).toEqual(["PUT drafts", "POST preview"]);
    const put = (await sent(calls, "PUT", `${API}/drafts`)[0]?.clone().json()) as Put;
    expect(put.drafts[0]?.body).toBe("Rounding happens once. BLOCKING");
  });

  it("a save that fails keeps the dialog shut and says why", async () => {
    const { calls } = setup(reviewed(), {
      [`PUT ${API}/drafts`]: () => Response.json({ error: "ReviewError", detail: "locked" }, { status: 503 }),
    });
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " x");
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    expect((await screen.findAllByText(/Couldn't save the drafts: locked/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent(calls, "POST", `${API}/post/preview`)).toHaveLength(0);
  });

  it("closing the tab saves what was typed", async () => {
    const { calls } = setup();
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " closing");
    fireEvent.click(within(workspace()).getByRole("button", { name: "Close Acme/web#3" }));
    await waitFor(() => expect(sent(calls, "PUT", `${API}/drafts`)).toHaveLength(1));
  });
});

describe("tabs (I3, R60)", () => {
  it("switching away and back keeps the page and never POSTs /open again", async () => {
    const { calls } = setup();
    await ready();
    await go(/Metrics/);
    await userEvent.click(within(workspace()).getByRole("link", { name: /Queue/ }));
    await userEvent.click(within(workspace()).getByRole("link", { name: /Acme\/web#3/ }));
    expect(screen.getByRole("heading", { level: 2, name: "Metrics" })).toBeInTheDocument();
    expect(sent(calls, "POST", `${API}/open`)).toHaveLength(1);
  });

  it("a failed round says so with Retry; only Retry runs it again", async () => {
    const { calls } = setup(reviewed(), {
      [`POST ${API}/open`]: () =>
        Response.json({ state: "failed", reason: "the last review of this head failed: boom; Retry runs it again", round_id: 3, jobs: [] }),
      [`POST ${API}/retry`]: () => Response.json({ state: "started", reason: "reviewing", round_id: 4, jobs: ["correctness"] }, { status: 202 }),
    }); // prettier-ignore
    expect(await screen.findByText(/the last review of this head failed: boom/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(sent(calls, "POST", `${API}/retry`)).toHaveLength(1));
  });
});

describe("the posting dialog's focus (I5)", () => {
  it("traps Tab, closes on Escape, and returns focus to the button that opened it", async () => {
    setup();
    await ready();
    const opener = within(actions()).getByRole("button", { name: "Comment…" });
    await userEvent.click(opener);
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    await within(box).findByText("Looks good overall.");
    const inside = () => box.contains(document.activeElement);
    for (let i = 0; i < 8; i += 1) {
      await userEvent.tab();
      expect(inside()).toBe(true);
    }
    for (let i = 0; i < 8; i += 1) {
      await userEvent.tab({ shift: true });
      expect(inside()).toBe(true);
    }
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("with two warnings, ticking one leaves the action locked (M15)", async () => {
    setup(reviewed(), {
      [`POST ${API}/post/preview`]: () =>
        Response.json(preview({ warnings: ["blocking_comment", "newer_head"], pr_head_sha: "f".repeat(40) })),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Approve…" }));
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    const [first, second] = await within(box).findAllByRole("checkbox");
    await userEvent.click(first as HTMLElement);
    expect(within(box).getByRole("button", { name: "Approve" })).toBeDisabled();
    // M3: the newer-head text names GitHub's live head, not the cached one.
    expect(within(box).getByRole("group", { name: "There are newer commits" })).toHaveTextContent(
      "the PR head is now fffffff",
    );
    await userEvent.click(second as HTMLElement);
    expect(within(box).getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("names a refused draft from the structured error, not its wording (M6)", async () => {
    setup(reviewed(), {
      [`PUT ${API}/drafts`]: () =>
        Response.json(
          { error: "InvalidDraftContent", detail: "reworded entirely", field: "draft", draft_index: 0 },
          { status: 422 },
        ),
    });
    await ready();
    await go(/Your comments/);
    await enter(draftBox(), " x");
    fireEvent.blur(draftBox());
    expect(
      await screen.findByText(/^Draft 1 \(src\/billing\/proration\.py:14–15\) can't be saved: reworded entirely\.$/),
    ).toBeInTheDocument();
  });
});

describe("hidden and direction-changing characters (I6, R62)", () => {
  const RLO = "\u202E";
  it.each([
    ["a bidi override", "\u202E", "U+202E"],
    ["a bidi isolate", "\u2066", "U+2066"],
    ["a direction mark", "\u200F", "U+200F"],
    ["an Arabic letter mark", "\u061C", "U+061C"],
    ["a zero-width space", "\u200B", "U+200B"],
    ["a byte order mark", "\uFEFF", "U+FEFF"],
    ["a word joiner", "\u2060", "U+2060"],
    ["a control character", "\u001B", "U+001B"],
  ])("shows %s in a diff line as a visible token, and badges the hunk", async (_name, ch, code) => {
    const patch = `@@ -1 +1 @@\n-ok\n+if (isAdmin${ch}) {`;
    const files = [{ ...FILES[0], patch }, FILES[1], FILES[2]] as PrView["files"];
    setup(reviewed({ files }));
    await ready();
    await go(/1\. The new prorate function/);
    const diff = screen.getByRole("region", { name: "Diff for this step" });
    const token = within(diff).getByText(`⟪${code}⟫`);
    expect(token).toHaveAttribute("title", expect.stringContaining(code));
    expect(within(diff).getAllByText("hidden or direction-changing characters").length).toBeGreaterThan(0);
    expect(diff.textContent).not.toContain(ch);
  });

  it("shows them in file names, titles and the outline", async () => {
    const path = `src/evil${RLO}gnp.py`;
    const files = [{ ...FILES[0], path }, FILES[1], FILES[2]] as PrView["files"];
    const walkthrough = { steps: [{ title: `Admin ${RLO}check`, body: "b", hunks: [{ path, hunk: 0 }] }], skipped: [] };
    setup(reviewed({ files, artifacts: { ...ARTIFACTS, walkthrough } }, { title: `Fix ${RLO}auth` }));
    await ready();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Fix ⟪U+202E⟫auth");
    expect(within(outline()).getByRole("link", { name: /Admin.*U\+202E.*check/ })).toBeInTheDocument();
    await go(/Admin/);
    const diff = screen.getByRole("region", { name: "Diff for this step" });
    expect(within(diff).getByText((_, el) => el?.textContent === `src/evil⟪U+202E⟫gnp.py`)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(RLO);
  });
});

describe("the smaller fixes", () => {
  it("each finding on a step can be drafted on (M8); navigation focuses the page heading (M11)", async () => {
    setup();
    await ready();
    await go(/2\. Posting to the ledger/);
    expect(screen.getByRole("heading", { level: 2, name: "Posting to the ledger" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Draft a comment on src/billing/proration.py:14–15" }));
    expect(
      await screen.findByRole("textbox", { name: /Comment on src\/billing\/proration\.py:14–15 \(new\)/ }),
    ).toHaveFocus();
  });

  it("navigating just after a load's render still focuses the new page's heading (#81)", async () => {
    // A load that renders just before a click leaves its effects pending; React runs them
    // before the click's own render, so they must not take the new page's focus for the old.
    let answer: (r: Response) => void = () => undefined;
    setup(reviewed(), { [`GET ${API}/drafts`]: () => new Promise<Response>((r) => (answer = r)) });
    await ready();
    const nav = outline();
    const step = within(nav).getByRole("link", { name: /2\. Posting to the ledger/ });
    const clicked = new Promise<void>((done) => {
      const watch = new MutationObserver(() => {
        if (!nav.textContent?.includes("1 draft")) return;
        watch.disconnect();
        step.click(); // after the drafts render commits, before its effects run
        done();
      });
      watch.observe(document.body, { subtree: true, childList: true, characterData: true });
    });
    answer(Response.json(drafts()));
    await clicked;
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Posting to the ledger" })).toHaveFocus());
  });

  it("the outline's parts label their own lists, and Contents names what it opens (M11)", async () => {
    setup();
    await ready();
    expect(within(outline()).getByRole("list", { name: "Before you read" })).toBeInTheDocument();
    expect(within(outline()).getByRole("list", { name: "After you read" })).toBeInTheDocument();
    const contents = screen.getByRole("button", { name: "Contents" });
    expect(document.getElementById(contents.getAttribute("aria-controls") ?? "")).toBe(outline());
  });

  it("read ticks of the PR's older rounds are pruned (M14)", async () => {
    localStorage.setItem("jesinia.review.read.acme/web#3@1", '["overview"]');
    localStorage.setItem("jesinia.review.read.acme/web#9@1", '["overview"]');
    setup();
    await ready();
    expect(localStorage.getItem("jesinia.review.read.acme/web#3@1")).toBeNull();
    expect(localStorage.getItem("jesinia.review.read.acme/web#9@1")).not.toBeNull();
  });
});

describe("big PRs (I4, R61)", () => {
  function big(n: number, linesPerHunk = 3): PrView {
    const body = Array.from({ length: linesPerHunk }, (_, k) => `+line ${k}`).join("\n");
    const patch = Array.from(
      { length: n },
      (_, i) => `@@ -${i * 10 + 1},1 +${i * 10 + 1},${linesPerHunk} @@\n${body}`,
    ).join("\n");
    const file = {
      path: "big.py",
      status: "modified",
      additions: n * linesPerHunk,
      deletions: 0,
      patch,
      previous_path: null,
    };
    const steps = Array.from({ length: n }, (_, i) => ({
      title: `Step ${i}`,
      body: "b",
      hunks: [{ path: "big.py", hunk: i }],
    }));
    return reviewed({ files: [file], artifacts: { ...ARTIFACTS, walkthrough: { steps, skipped: [] } } });
  }

  it("windows a long walkthrough in the outline, keeping the current step in view", async () => {
    // Just past the windowing threshold: enough to window, small enough to stay fast on CI.
    const n = WINDOW_FROM + 50;
    setup(big(n));
    await ready();
    const steps = within(outline()).getByRole("list", { name: /Walkthrough/ });
    const shown = within(steps).getAllByRole("link");
    expect(shown.length).toBeLessThan(n / 2);
    expect(shown[0]).toHaveAccessibleName(/1\. Step 0/);
    fireEvent.scroll(steps, { target: { scrollTop: 30 * 150 } });
    expect(within(steps).getByRole("link", { name: /151\. Step 150/ })).toBeInTheDocument();
    await userEvent.click(within(steps).getByRole("link", { name: /151\. Step 150/ }));
    expect(screen.getByRole("heading", { level: 2, name: "Step 150" })).toBeInTheDocument();
  });

  it("a step shows its own hunks, the file's others on request, and a big hunk in pieces", async () => {
    // A hunk just past the line window (not thousands of lines): it still shows in pieces.
    setup(big(3, LINES_SHOWN + 20));
    await ready();
    await go(/1\. Step 0/);
    const diff = screen.getByRole("region", { name: "Diff for this step" });
    // Counted with selectors, not role or text queries: those compute the accessibility tree
    // over hundreds of diff lines on every call, which is what made this test slow on CI.
    const hunks = () => diff.querySelectorAll('[role="group"]').length;
    const lines = () => [...diff.querySelectorAll("pre")].filter((p) => p.textContent?.startsWith("+line ")).length;
    expect(hunks()).toBe(1);
    expect(lines()).toBe(LINES_SHOWN);
    await userEvent.click(within(diff).getByRole("button", { name: "Show 20 more lines" }));
    expect(lines()).toBe(LINES_SHOWN + 20);
    await userEvent.click(screen.getByRole("button", { name: "Show the file's 2 other hunks" }));
    expect(hunks()).toBe(3);
    await userEvent.click(screen.getByRole("button", { name: "Show only this step's hunks" }));
    expect(hunks()).toBe(1);
  });

  it("paging through steps costs about the same at n and 4n steps (generous ratio, absolute ceiling)", async () => {
    const page = async (n: number) => {
      setup(big(n));
      await ready();
      await go(/1\. Step 0/);
      const start = performance.now();
      // fireEvent, not userEvent: the measure is the app's work per page, not the simulated
      // pointer sequence around each click.
      for (let i = 0; i < 5; i += 1) {
        act(() => {
          fireEvent.click(within(actions()).getByRole("button", { name: /→$/ }));
        });
      }
      const ms = performance.now() - start;
      expect(screen.getByRole("heading", { level: 2, name: "Step 5" })).toBeInTheDocument();
      cleanup();
      return ms;
    };
    // Robust on a loaded CI box (final M2): warm-up, the median of 3, a floor on the small side,
    // a generous ratio and an absolute ceiling.
    const median = async (n: number) => {
      const runs: number[] = [];
      for (let i = 0; i < 3; i += 1) runs.push(await page(n));
      return runs.sort((a, b) => a - b)[1] as number;
    };
    // Both sizes are past the windowing threshold, so the ratio measures windowing itself.
    await page(WINDOW_FROM + 50); // warm up
    const small = await median(WINDOW_FROM + 50);
    const large = await median(4 * (WINDOW_FROM + 50));
    expect(large).toBeLessThan(10_000);
    expect(large / Math.max(small, 20)).toBeLessThan(8);
    // An explicit timeout: this one test renders the app 7 times (a warm-up and two medians
    // of three) to compare n with 4n. It takes about 1 s locally; a loaded CI runner can be
    // ten times slower, and the default 5 s would fail it for load, not for a regression.
  }, 30_000);
});
