// SPDX-License-Identifier: AGPL-3.0-or-later
// Read and Edit in the Notes workspace (R-REN2): defaults by kind, the toggle, ⌘E, memory.
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { VIEW_KEY_PREFIX } from "../read";

const SOURCE_ID = "0199e000-0000-7000-8000-00000000000a";
const NOTE_ID = "0199e000-0000-7000-8000-00000000000b";
const SOURCE = {
  id: SOURCE_ID,
  title: "Kept post",
  path: "Sources/Kept post.md",
  body: `## Article\n\nSee [[${NOTE_ID}|My note]].\n\n![Figure](../_assets/${SOURCE_ID}/f1.png)`,
  jesinia: { kind: "source/rss", source: "https://blog.example/p" },
  foreign: {},
  rev: "r1",
};
const NOTE = {
  id: NOTE_ID,
  title: "My note",
  path: "Notes/My note.md",
  body: "# Heading\n\nNote text.",
  jesinia: { kind: "note" },
  foreign: {},
  rev: "n1",
};

function setup(id = SOURCE_ID, over: Routes = {}) {
  window.history.pushState(null, "", `/notes/page/${id}`);
  const env = fakeClient(
    baseRoutes({
      "GET /api/kb/tree": [],
      [`GET /api/kb/pages/${SOURCE_ID}`]: SOURCE,
      [`GET /api/kb/pages/${NOTE_ID}`]: NOTE,
      ...over,
    }),
  );
  render(<App client={env.client} />);
  return env;
}

const readButton = () => screen.getByRole("button", { name: "Read" });
const editButton = () => screen.getByRole("button", { name: "Edit" });

beforeEach(() => {
  localStorage.clear();
  Range.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(() => {
  vi.restoreAllMocks();
  window.history.pushState(null, "", "/");
});

describe("Read view", () => {
  it("is the default for a source page: rendered, with its local image", async () => {
    setup();
    expect(await screen.findByRole("heading", { level: 2, name: "Article" })).toBeInTheDocument();
    expect(readButton()).toHaveAttribute("aria-pressed", "true");
    expect(editButton()).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("editor")).toBeNull();
    const img = screen.getByRole("img", { name: "Figure" });
    expect(img).toHaveAttribute("src", `/api/kb/assets/${SOURCE_ID}/f1.png`);
    for (const el of document.querySelectorAll("img")) {
      expect(el.getAttribute("src") ?? "").toMatch(/^\/(api\/kb\/assets\/|notes\/icons\/)/);
    }
  });

  it("never shows the page's front matter, and keeps a --- block that is part of the body", async () => {
    setup(SOURCE_ID, {
      [`GET /api/kb/pages/${SOURCE_ID}`]: { ...SOURCE, body: `---\nlayout: body-block\n---\n${SOURCE.body}` },
    });
    const read = await screen.findByRole("article", { name: "Read view" });
    expect(read).toHaveTextContent("layout: body-block");
    expect(read).not.toHaveTextContent("source/rss");
    expect(read).not.toHaveTextContent("blog.example");
    expect(read).not.toHaveTextContent(SOURCE_ID);
  });

  it("Edit stays the default for a note", async () => {
    setup(NOTE_ID);
    expect(await screen.findByTestId("editor")).toBeInTheDocument();
    expect(editButton()).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("article", { name: "Read view" })).toBeNull();
  });

  it("the toggle switches views, and the choice is remembered per kind", async () => {
    setup(NOTE_ID);
    await screen.findByTestId("editor");
    fireEvent.click(readButton());
    expect(await screen.findByRole("heading", { level: 1, name: "Heading" })).toBeInTheDocument();
    expect(screen.queryByTestId("editor")).toBeNull();
    expect(localStorage.getItem(`${VIEW_KEY_PREFIX}note`)).toBe("read");
    fireEvent.click(editButton());
    expect(await screen.findByTestId("editor")).toBeInTheDocument();
    expect(localStorage.getItem(`${VIEW_KEY_PREFIX}note`)).toBe("edit");
    expect(localStorage.getItem(`${VIEW_KEY_PREFIX}source/rss`)).toBeNull();
  });

  it("a remembered choice wins over the kind's default", async () => {
    localStorage.setItem(`${VIEW_KEY_PREFIX}source/rss`, "edit");
    localStorage.setItem(`${VIEW_KEY_PREFIX}note`, "bogus");
    setup();
    expect(await screen.findByTestId("editor")).toBeInTheDocument();
  });

  it("storage that throws falls back to the defaults", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    setup();
    expect(await screen.findByRole("heading", { level: 2, name: "Article" })).toBeInTheDocument();
    fireEvent.click(editButton());
    expect(await screen.findByTestId("editor")).toBeInTheDocument();
  });

  it("Ctrl+E toggles off macOS, ⌘E on macOS", async () => {
    setup();
    await screen.findByRole("heading", { level: 2, name: "Article" });
    fireEvent.keyDown(window, { key: "e", metaKey: true }); // not this platform's shortcut
    expect(screen.queryByTestId("editor")).toBeNull();
    fireEvent.keyDown(window, { key: "e", ctrlKey: true });
    expect(await screen.findByTestId("editor")).toBeInTheDocument();
    expect(readButton()).toHaveAttribute("title", "Read (Ctrl+E)");
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    fireEvent.keyDown(window, { key: "E", metaKey: true });
    expect(await screen.findByRole("heading", { level: 2, name: "Article" })).toBeInTheDocument();
    expect(readButton()).toHaveAttribute("title", "Read (⌘E)");
    fireEvent.keyDown(window, { key: "e", metaKey: true, shiftKey: true }); // not ⌘E
    expect(screen.queryByTestId("editor")).toBeNull();
  });

  it("the shortcut does nothing with no page open", async () => {
    window.history.pushState(null, "", "/notes/");
    render(<App client={fakeClient(baseRoutes({ "GET /api/kb/tree": [] })).client} />);
    await screen.findByText("Open a page from the tree, or search the KB.");
    const e = new KeyboardEvent("keydown", { key: "e", ctrlKey: true, cancelable: true });
    window.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(false);
    expect(screen.queryByRole("group", { name: "View" })).toBeNull();
  });

  it("switching to Read shows the current text, unsaved edits included, and saves them", async () => {
    const saved: string[] = [];
    setup(NOTE_ID, {
      [`PATCH /api/kb/pages/${NOTE_ID}/body`]: async (req: Request) => {
        const { body } = (await req.json()) as { body: string };
        saved.push(body);
        return Response.json({ ...NOTE, body, rev: "n2" });
      },
    });
    const el = await screen.findByRole("textbox", { name: "Edit My note" });
    const { EditorView } = await import("@codemirror/view");
    const view = EditorView.findFromDOM(el);
    act(() => view?.dispatch({ changes: { from: view.state.doc.length, insert: "\n\n## Added" } }));
    // The buffer is the body alone (the API strips front matter), so Read needs no stripping.
    expect(view?.state.doc.toString()).toBe(`${NOTE.body}\n\n## Added`);
    fireEvent.click(readButton());
    expect(await screen.findByRole("heading", { level: 2, name: "Added" })).toBeInTheDocument();
    const read = screen.getByRole("article", { name: "Read view" });
    expect(read).not.toHaveTextContent("kind");
    expect(read).not.toHaveTextContent(NOTE_ID);
    await waitFor(() => expect(saved.at(-1)).toContain("## Added"));
  });

  it("a page link opens that page in Notes", async () => {
    setup();
    fireEvent.click(await screen.findByRole("link", { name: "My note" }));
    expect(await screen.findByRole("heading", { level: 1, name: "My note" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/notes/page/${NOTE_ID}`);
  });
});
