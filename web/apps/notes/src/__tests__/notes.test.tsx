// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

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
function kbChanged(id: string) {
  const src = [...FakeSource.all].reverse().find((s) => s.url.includes("kb.page_changed") && !s.url.includes("kbsync"));
  act(() =>
    src?.listeners.get("kb.page_changed")?.(
      new MessageEvent("kb.page_changed", {
        data: JSON.stringify({ id, origin: "external", change: "updated" }),
        lastEventId: String(++seq),
      }),
    ),
  );
}

const PAGE = {
  id: "p1",
  title: "Topic",
  path: "Ideas/Topic.md",
  body: "Hello body",
  jesinia: {},
  foreign: {},
  rev: "r1",
};

function routes(over: Routes = {}): Routes {
  return baseRoutes({
    "GET /api/agents/runners": {
      fake: { available: true, version: "fake", detail: "ok" },
      codex: { available: false, version: null, detail: "x" },
    },
    "GET /api/kb/tree": [
      {
        path: "Ideas",
        kind: "folder",
        pages: 1,
        children: [{ path: "Ideas/Topic.md", kind: "page", title: "Topic", id: "p1", pages: 1, children: [] }],
      },
    ],
    "GET /api/kb/pages/p1": PAGE,
    "GET /api/search": [
      { source: "kb", key: "p1", title: "Topic", snippet: "a <mark>topic</mark> &amp; more", path: "Ideas/Topic.md" },
      { source: "kb", key: "p2", title: "Other", snippet: "about the <mark>topic</mark>", path: "Ideas/Other.md" },
    ],
    "POST /api/notes/sessions": {
      id: "s1",
      runner: "fake",
      status: "idle",
      subject: { kind: "kb", ref: "p1", title: "Topic" },
    },
    "POST /api/agents/sessions/s1/messages": { ok: true },
    "POST /api/notes/sessions/s1/save": { id: "n1", title: "Note on Topic", path: "Notes/Note on Topic.md", rev: "x" },
    "POST /api/agents/sessions/s1/pending/w1/approve": { id: "w1", state: "approved" },
    "POST /api/agents/sessions/s1/pending/w1/reject": { id: "w1", state: "rejected" },
    ...over,
  });
}

beforeEach(() => {
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
  // CodeMirror measures layout; jsdom has no layout.
  Range.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(() => {
  window.history.pushState(null, "", "/");
  vi.useRealTimers();
});

function setup(over: Routes = {}, path = "/notes/page/p1") {
  window.history.pushState(null, "", path);
  const env = fakeClient(routes(over));
  render(<App client={env.client} />);
  return env;
}

describe("Notes", () => {
  it("search debounces 200 ms and lists hits grouped titles-first", async () => {
    const { calls } = setup({}, "/notes/");
    expect(await screen.findByText("Open a page from the tree, or search the KB.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Topic" })).toBeInTheDocument(); // the tree
    const box = screen.getByRole("searchbox", { name: "Search notes" });
    await userEvent.type(box, "topic");
    const searches = () => calls.filter((c) => c.url.includes("/api/search")).length;
    expect(searches()).toBe(0);
    await waitFor(() => expect(searches()).toBe(1), { timeout: 1000 });
    const results = await screen.findByTestId("search-results");
    expect(results.querySelectorAll("section")[0]).toHaveAccessibleName("Titles");
    expect(results.querySelectorAll("section")[1]).toHaveAccessibleName("Content");
    expect(results.querySelector("mark")?.textContent).toBe("topic");
    expect(results).toHaveTextContent("a topic & more");
    await userEvent.click(screen.getByRole("button", { name: /Other/ }));
    expect(window.location.pathname).toBe("/notes/page/p2");
  });

  it("no results, and a page that is gone", async () => {
    setup({ "GET /api/search": [], "GET /api/kb/pages/p1": () => Response.json({ detail: "gone" }, { status: 404 }) });
    expect(await screen.findByText("This page isn't in the KB any more.")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search notes" }), "zzz");
    expect(await screen.findByText(/No pages match/)).toBeInTheDocument();
  });

  it("chat streams deltas into one agent message; Save note waits for the turn to finish", async () => {
    const { calls } = setup();
    expect(await screen.findByRole("heading", { name: "Topic", level: 1 })).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save note" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "What is this?{Enter}");
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/api/agents/sessions/s1/messages"))).toBe(true));
    agent("user_message", { text: "What is this?" });
    agent("text", { text: "It is " });
    agent("text", { text: "a topic." });
    expect(await screen.findByText("It is a topic.")).toBeInTheDocument();
    expect(document.querySelectorAll("article")).toHaveLength(1);
    expect(save).toBeDisabled();
    agent("turn_done", { model: "fake-model" });
    await waitFor(() => expect(save).toBeEnabled());
    await userEvent.click(save);
    const body = await calls.find((c) => c.url.endsWith("/save"))?.json();
    expect(body).toEqual({
      title: "Note on Topic",
      user_text: "What is this?",
      agent_text: "It is a topic.",
      model: "fake-model",
    });
    expect((await screen.findAllByText("Saved to Notes/Note on Topic.md.")).length).toBeGreaterThan(0);
  });

  it("pending writes: approve and reject reach the server", async () => {
    const { calls } = setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Draft it");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(FakeSource.all.some((s) => s.url.includes("/sessions/s1/"))).toBe(true));
    agent("pending_write", {
      pending_id: "w1",
      tool: "kb_write",
      args: { title: "Draft", folder: "Notes", body: "x" },
      target: { action: "create", path: "Notes/Draft.md", title: "Draft" },
    });
    expect(await screen.findByText("Notes/Draft.md")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/pending/w1/reject"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Approve change" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/pending/w1/approve"))).toBe(true));
    agent("pending_resolved", { pending_id: "w1", state: "approved" });
    expect(await screen.findByText(/Approved · Notes\/Draft.md/)).toBeInTheDocument();
    agent("error", { message: "Fake stopped" });
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
  });

  it("reports chat failures", async () => {
    setup({
      "POST /api/notes/sessions": () => Response.json({ detail: "unknown runner" }, { status: 400 }),
    });
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "hi{Enter}");
    expect((await screen.findAllByText("unknown runner")).length).toBeGreaterThan(0);
  });

  it("reports send and save failures, and no runners", async () => {
    setup({
      "POST /api/agents/sessions/s1/messages": () => Response.json({ detail: "busy" }, { status: 409 }),
      "POST /api/notes/sessions/s1/save": () => Response.json({ detail: "KB unavailable" }, { status: 409 }),
    });
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "hi{Enter}");
    expect((await screen.findAllByText("busy")).length).toBeGreaterThan(0);
    agent("text", { text: "ok" });
    agent("turn_done", {});
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect((await screen.findAllByText("KB unavailable")).length).toBeGreaterThan(0);
  });

  it("shows when no agent is available", async () => {
    setup({ "GET /api/agents/runners": { codex: { available: false, version: null, detail: "x" } } });
    expect(await screen.findByText(/No agent is available/)).toBeInTheDocument();
  });

  it("autosaves after a pause, then handles a changed-on-disk conflict", async () => {
    let rev = "r1";
    const patches: { body: string; expected_rev: string }[] = [];
    const { calls } = setup({
      "PATCH /api/kb/pages/p1/body": async (req: Request) => {
        const body = (await req.json()) as { body: string; expected_rev: string };
        patches.push(body);
        if (body.expected_rev !== rev)
          return Response.json({ error: "StaleWrite", rev, body: "theirs" }, { status: 409 });
        rev = `r${patches.length + 1}`;
        return Response.json({ ...PAGE, body: body.body, rev });
      },
    });
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    const editor = await screen.findByRole("textbox", { name: "Edit Topic" });
    expect(editor).toHaveTextContent("Hello body");
    await act(async () => {
      editor.focus();
      document.execCommand?.("insertText", false, "!");
    });
    // jsdom has no editing commands: drive CodeMirror through its view instead.
    const { EditorView } = await import("@codemirror/view");
    const view = EditorView.findFromDOM(editor);
    if (!view) throw new Error("no editor view");
    act(() => view.dispatch({ changes: { from: view.state.doc.length, insert: " more" }, userEvent: "input.type" }));
    expect(screen.getByTestId("save-state")).toHaveTextContent("Editing…");
    await waitFor(() => expect(patches).toHaveLength(1), { timeout: 2500 });
    expect(patches[0]).toEqual({ body: "Hello body more", expected_rev: "r1" });
    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("Saved"));

    rev = "disk";
    act(() => view.dispatch({ changes: { from: 0, insert: "Mine: " }, userEvent: "input.type" }));
    fireEvent.blur(view.contentDOM);
    expect(await screen.findByText("Changed on disk:")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Keep mine" }));
    await waitFor(() => expect(patches.at(-1)).toEqual({ body: "Mine: Hello body more", expected_rev: "disk" }));
    await waitFor(() => expect(screen.queryByText("Changed on disk:")).toBeNull());

    rev = "disk2";
    act(() => view.dispatch({ changes: { from: 0, insert: "x" }, userEvent: "input.type" }));
    fireEvent.blur(view.contentDOM);
    await userEvent.click(await screen.findByRole("button", { name: "Use theirs" }));
    await waitFor(() => expect(view.state.doc.toString()).toBe("theirs"));
    expect(calls.filter((c) => c.method === "PATCH").length).toBe(patches.length);
  });

  it("an external change reloads a clean page and flags a dirty one", async () => {
    let body = "v1";
    let rev = "r1";
    setup({
      "GET /api/kb/pages/p1": () => Response.json({ ...PAGE, body, rev }),
      "PATCH /api/kb/pages/p1/body": () => Response.json({ detail: "boom" }, { status: 500 }),
    });
    const editor = await screen.findByRole("textbox", { name: "Edit Topic" });
    const { EditorView } = await import("@codemirror/view");
    const view = EditorView.findFromDOM(editor);
    if (!view) throw new Error("no editor view");
    body = "v2";
    rev = "r2";
    kbChanged("p1");
    await waitFor(() => expect(view.state.doc.toString()).toBe("v2"));
    kbChanged("other");
    act(() => view.dispatch({ changes: { from: 0, insert: "local " }, userEvent: "input.type" }));
    body = "v3";
    rev = "r3";
    kbChanged("p1");
    expect(await screen.findByText("Changed on disk:")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Keep mine" }));
    expect(await screen.findByText("Couldn't save (500).")).toBeInTheDocument();
  });

  it("follows browser history and offers [[ link completion", async () => {
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    window.history.pushState(null, "", "/notes/");
    act(() => window.dispatchEvent(new PopStateEvent("popstate")));
    expect(await screen.findByText("Open a page from the tree, or search the KB.")).toBeInTheDocument();
    const { linkCompletion } = await import("../Editor");
    const { CompletionContext } = await import("@codemirror/autocomplete");
    const { EditorState } = await import("@codemirror/state");
    const find = vi.fn(async () => [{ id: "p2", title: "Other" }]);
    const state = EditorState.create({ doc: "see [[Oth" });
    const result = await linkCompletion(find)(new CompletionContext(state, state.doc.length, false));
    expect(result?.from).toBe(4);
    expect(result?.options[0]).toMatchObject({ label: "Other", apply: "[[p2|Other]]" });
    const empty = EditorState.create({ doc: "[[" });
    expect((await linkCompletion(find)(new CompletionContext(empty, 2, false)))?.options).toEqual([]);
    const none = EditorState.create({ doc: "plain" });
    expect(await linkCompletion(find)(new CompletionContext(none, 5, false))).toBeNull();
  });

  it("folds provenance into chips, picks runners, opens tree pages, and uses [[ search", async () => {
    const body =
      "Mine.\n\n<!-- jesinia:ai agent=claude-code model=m at=x session=s -->\nTheirs.\n<!-- /jesinia:ai -->\n";
    const { calls } = setup({
      "GET /api/kb/pages/p1": { ...PAGE, body },
      "GET /api/agents/runners": {
        "claude-code": { available: true, version: "2", detail: "ok" },
        codex: { available: true, version: "1", detail: "ok" },
      },
      "GET /api/kb/tree": [
        {
          path: "Ideas",
          kind: "folder",
          pages: 2,
          children: [
            { path: "Ideas/Topic.md", kind: "page", title: "Topic", id: "p1", pages: 1, children: [] },
            { path: "Ideas/untitled.md", kind: "page", title: null, id: null, pages: 1, children: [] },
          ],
        },
      ],
    });
    const editor = await screen.findByRole("textbox", { name: "Edit Topic" });
    const chips = [...editor.querySelectorAll(".jx-provenance")].map((c) => c.textContent);
    expect(chips).toEqual(["AI · claude-code · m", "end of AI text"]);
    await userEvent.selectOptions(screen.getByRole("combobox"), "codex");
    expect(screen.getByRole("combobox")).toHaveValue("codex");
    expect(screen.getByRole("button", { name: "Topic" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Ideas/untitled.md" }));
    expect(window.location.pathname).toBe("/notes/page/p1");
    const { EditorView } = await import("@codemirror/view");
    const { startCompletion } = await import("@codemirror/autocomplete");
    const view = EditorView.findFromDOM(editor);
    if (!view) throw new Error("no editor view");
    act(() =>
      view.dispatch({ changes: { from: 0, insert: "[[Oth" }, selection: { anchor: 5 }, userEvent: "input.type" }),
    );
    act(() => {
      startCompletion(view);
    });
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/search") && c.url.includes("q=Oth"))).toBe(true));
  });
});
