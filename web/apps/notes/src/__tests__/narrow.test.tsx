// SPDX-License-Identifier: AGPL-3.0-or-later
// Notes below 1024 px: the page gets the screen, and the chat is a sheet opened on request.
import { baseRoutes, fakeClient } from "@jesinia/shell";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { App, CHAT_SHEET_QUERY } from "../App";

const PAGE = { id: "p1", title: "Topic", path: "Ideas/Topic.md", body: "Hello", jesinia: {}, foreign: {}, rev: "r1" };

type Listener = () => void;
const ORIGINAL_MATCH_MEDIA = window.matchMedia;

// Not vi.stubGlobal: unstubbing would also undo the setup file's EventSource stub.
function setMatchMedia(fn: unknown) {
  Object.defineProperty(window, "matchMedia", { value: fn, configurable: true, writable: true });
}

/** A window of `width` px: `(max-width: N px)` queries answer as a browser would, and follow resizes. */
function windowOf(width: number) {
  let current = width;
  const lists: { query: string; listeners: Set<Listener>; mql: { matches: boolean } }[] = [];
  const answers = (query: string) => {
    const m = /max-width:\s*(\d+)px/.exec(query);
    return m?.[1] !== undefined && current <= Number(m[1]);
  };
  setMatchMedia((query: string) => {
    const listeners = new Set<Listener>();
    const mql = {
      media: query,
      get matches() {
        return answers(query);
      },
      addEventListener: (_: string, fn: Listener) => listeners.add(fn),
      removeEventListener: (_: string, fn: Listener) => listeners.delete(fn),
    };
    lists.push({ query, listeners, mql });
    return mql;
  });
  return {
    resize: (next: number) =>
      act(() => {
        current = next;
        for (const l of lists) if (l.query.includes("max-width")) for (const fn of l.listeners) fn();
      }),
  };
}

function setup() {
  window.history.pushState(null, "", "/notes/page/p1");
  const env = fakeClient(baseRoutes({ "GET /api/kb/tree": [], "GET /api/kb/pages/p1": PAGE }));
  render(<App client={env.client} />);
  return env;
}

const chat = () => screen.queryByRole("complementary", { name: "Chat" });
const asker = () => screen.getByRole("button", { name: "Open the chat about this page" });

beforeEach(() => {
  localStorage.clear();
  Range.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});
afterEach(() => {
  setMatchMedia(ORIGINAL_MATCH_MEDIA);
  window.history.pushState(null, "", "/");
});

describe("under 1024 px", () => {
  it("asks about narrow windows with the 1023 px query", () => {
    expect(CHAT_SHEET_QUERY).toBe("(max-width: 1023px)");
  });

  it("hides the chat by default, so the page fills the main area", async () => {
    windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    expect(chat()).toBeNull();
    expect(asker()).toHaveAttribute("aria-expanded", "false");
  });

  it("opens the chat as a sheet from the page toolbar", async () => {
    windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.click(asker());
    const sheet = screen.getByRole("dialog", { name: "Chat about this page" });
    expect(sheet).not.toHaveAttribute("aria-modal", "true");
    expect(sheet).toContainElement(chat());
    expect(asker()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
  });

  it("closes on Esc and gives focus back to the button", async () => {
    windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.click(asker());
    await userEvent.keyboard("{Escape}");
    expect(chat()).toBeNull();
    expect(asker()).toHaveFocus();
  });

  it("closes from its close button too", async () => {
    windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.click(asker());
    await userEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(chat()).toBeNull();
    expect(asker()).toHaveFocus();
  });

  it("keeps an unsent draft across closing and reopening", async () => {
    windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.click(asker());
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "half a thought");
    await userEvent.keyboard("{Escape}");
    await userEvent.click(asker());
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("half a thought");
  });

  it("works the same on a phone-width window", async () => {
    windowOf(500);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    expect(chat()).toBeNull();
    await userEvent.click(asker());
    expect(screen.getByRole("dialog", { name: "Chat about this page" })).toBeInTheDocument();
  });

  it("keeps the draft when the window widens past the breakpoint", async () => {
    const win = windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.click(asker());
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "kept");
    win.resize(1400);
    expect(screen.queryByRole("button", { name: "Open the chat about this page" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("kept");
  });
});

describe("crossing from wide to narrow", () => {
  it("keeps a chat with a draft open, as a sheet", async () => {
    const win = windowOf(1400);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "mid-thought");
    win.resize(800);
    expect(screen.getByRole("dialog", { name: "Chat about this page" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("mid-thought");
    expect(asker()).toHaveAttribute("aria-expanded", "true");
  });

  it("closes an unused chat, so the page gets the screen", async () => {
    const win = windowOf(1400);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    win.resize(800);
    expect(chat()).toBeNull();
    expect(asker()).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the rail again on widening, even after the sheet was closed", async () => {
    const win = windowOf(800);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    win.resize(1400);
    expect(chat()).toBeInTheDocument();
  });
});

describe("at 1024 px and wider", () => {
  it("keeps the chat beside the page, with no sheet and no button", async () => {
    windowOf(1400);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    expect(chat()).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open the chat about this page" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is the layout when the browser has no matchMedia", async () => {
    setMatchMedia(undefined);
    setup();
    await screen.findByRole("heading", { name: "Topic", level: 1 });
    expect(chat()).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open the chat about this page" })).toBeNull();
  });
});
