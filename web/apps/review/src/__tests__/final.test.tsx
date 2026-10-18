// SPDX-License-Identifier: AGPL-3.0-or-later
// PR 3 final fix wave: the drafts editor keeps its identity across refreshes (I1), a save keeps
// drafts the server seeded meanwhile (I2), and the posting preview shows hidden characters (M4).
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { PrView } from "../api";
import { queue, routes } from "./fixtures";
import { API, drafts, PATH, preview, reviewed } from "./pr-fixtures";

// Built from code points: the source itself never carries hidden characters (Trojan Source).
const RLO = String.fromCodePoint(0x202e);
const ZWSP = String.fromCodePoint(0x200b);

class FakeSource {
  static all: FakeSource[] = [];
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener() {}
  close() {}
}

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
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const gets = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "GET" && new URL(c.url).pathname === path);
const outline = () => screen.getByRole("navigation", { name: "Review outline" });
const actions = () => screen.getByRole("group", { name: "Page actions" });
const go = async (name: RegExp) => userEvent.click(within(outline()).getByRole("link", { name }));
const ready = () => screen.findByRole("heading", { level: 2, name: "Overview" });
const draftBox = () => screen.getByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
/** A queue read, as every poll or progress event makes: the open review reads again. */
const refresh = async (calls: Request[]) => {
  const before = gets(calls, `${API}/drafts`).length;
  await act(async () => FakeSource.all.find((s) => s.url.includes("review.progress"))?.onopen?.());
  return before;
};

beforeEach(() => {
  localStorage.clear();
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the drafts editor across refreshes (I1)", () => {
  it("a refresh keeps the focused textarea, and the same element", async () => {
    const { calls } = setup();
    await ready();
    await go(/Your comments/);
    const box = draftBox();
    box.focus();
    const before = await refresh(calls);
    await waitFor(() => expect(gets(calls, `${API}/drafts`).length).toBeGreaterThan(before));
    await act(async () => new Promise((r) => setTimeout(r, 10)));
    expect(draftBox()).toBe(box);
    expect(box).toHaveFocus();
  });

  it("a draft just added on a finding survives a refresh", async () => {
    const { calls } = setup();
    await ready();
    await go(/2\. Posting to the ledger/);
    await userEvent.click(within(actions()).getByRole("button", { name: "Draft comment on this finding" }));
    const added = await screen.findByRole("textbox", { name: /\(new\)/ });
    const before = await refresh(calls);
    await waitFor(() => expect(gets(calls, `${API}/drafts`).length).toBeGreaterThan(before));
    await act(async () => new Promise((r) => setTimeout(r, 10)));
    expect(screen.getByRole("textbox", { name: /\(new\)/ })).toBe(added);
    expect(added).toHaveFocus();
  });

  it("a hidden review doesn't read its drafts on every poll, and reads them again when shown", async () => {
    const { calls } = setup();
    await ready();
    const workspace = screen.getByRole("navigation", { name: "Open reviews" });
    await userEvent.click(within(workspace).getByRole("link", { name: /Queue/ }));
    const before = await refresh(calls);
    await act(async () => new Promise((r) => setTimeout(r, 20)));
    expect(gets(calls, `${API}/drafts`)).toHaveLength(before);
    await userEvent.click(within(workspace).getByRole("link", { name: /Acme\/web#3/ }));
    await waitFor(() => expect(gets(calls, `${API}/drafts`).length).toBeGreaterThan(before));
  });
});

describe("a save that triggers seeding (I2)", () => {
  it("drafts the server added in its answer appear in the editor", async () => {
    setup(reviewed(), {
      [`PUT ${API}/drafts`]: async (req: Request) => {
        const body = (await req.clone().json()) as ReturnType<typeof drafts>;
        const [first] = drafts().drafts;
        if (!first) throw new Error("fixture has a draft");
        const seeded = { ...first, id: 7, body: "Seeded from a finding.", origin_finding: "f2" };
        return Response.json(drafts({ summary: body.summary, drafts: [...body.drafts, seeded] }));
      },
    });
    await ready();
    await go(/Your comments/);
    await userEvent.type(draftBox(), " more");
    fireEvent.blur(draftBox());
    expect(await screen.findByDisplayValue("Seeded from a finding.")).toBeInTheDocument();
    const boxes = screen.getAllByRole("textbox", { name: "Comment on src/billing/proration.py:14–15" });
    expect(boxes.map((b) => (b as HTMLTextAreaElement).value)).toEqual([
      "Rounding happens once. more",
      "Seeded from a finding.",
    ]);
  });
});

describe("the posting preview shows hidden characters (M4)", () => {
  it("renders the body and each comment with visible tokens", async () => {
    setup(reviewed(), {
      [`POST ${API}/post/preview`]: () =>
        Response.json(
          preview({
            event: "COMMENT",
            body: `Fine${RLO} reversed`,
            comments: [{ ...preview().comments[0], body: `zero${ZWSP}width` }] as ReturnType<
              typeof preview
            >["comments"],
          }),
        ),
    });
    await ready();
    await userEvent.click(within(actions()).getByRole("button", { name: "Comment…" }));
    const box = await screen.findByRole("dialog", { name: /on GitHub/ });
    expect(await within(box).findByText("⟪U+202E⟫")).toBeInTheDocument();
    expect(within(box).getByText("⟪U+200B⟫")).toBeInTheDocument();
    expect(box.textContent).not.toContain(RLO);
    expect(box.textContent).not.toContain(ZWSP);
  });
});

describe("the other hunks toggle is per step (M3)", () => {
  it("showing a file's other hunks on one step doesn't carry over to the next", async () => {
    setup();
    await ready();
    await go(/1\. The new prorate function/);
    await userEvent.click(screen.getByRole("button", { name: "Show the file's 1 other hunk" }));
    const diff = () => screen.getByRole("region", { name: "Diff for this step" });
    expect(within(diff()).getAllByRole("group")).toHaveLength(2);
    await go(/2\. Posting to the ledger/);
    expect(within(diff()).getAllByRole("group")).toHaveLength(1);
  });
});

describe("seeds in a dropped read (N2)", () => {
  it("a read answered after a keystroke still keeps the drafts the server seeded in it", async () => {
    let release: (() => void) | null = null;
    let reads = 0;
    const seeded = {
      ...(drafts().drafts[0] as ReturnType<typeof drafts>["drafts"][number]),
      id: 9,
      body: "Seeded in the read.",
    };
    const { calls } = setup(reviewed(), {
      [`GET ${API}/drafts`]: async () => {
        reads += 1;
        if (reads === 1) return Response.json(drafts());
        await new Promise<void>((resolve) => (release = resolve));
        return Response.json(drafts({ drafts: [...drafts().drafts, seeded] }));
      },
    });
    await ready();
    await go(/Your comments/);
    await refresh(calls);
    await waitFor(() => expect(release).not.toBeNull());
    const box = draftBox();
    await userEvent.type(box, " typed");
    await act(async () => release?.());
    expect(await screen.findByDisplayValue("Seeded in the read.")).toBeInTheDocument();
    fireEvent.blur(box);
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(1));
    const put = (await calls
      .filter((c) => c.method === "PUT")[0]
      ?.clone()
      .json()) as { drafts: { body: string }[] };
    expect(put.drafts.map((d) => d.body)).toEqual(["Rounding happens once. typed", "Seeded in the read."]);
  });
});
