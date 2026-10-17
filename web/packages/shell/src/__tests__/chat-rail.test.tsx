// SPDX-License-Identifier: AGPL-3.0-or-later
// The shared chat rail (Task 14): the Notes chat minus Save note, which apps pass as `footer`.
import { ToastProvider } from "@jesinia/design";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatRail, ClientProvider, fakeClient, type ChatRailProps, type Routes, type Runners } from "../index";

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {
    this.closed = true;
  }
}
let seq = 0;
function agent(kind: string, data: Record<string, unknown> = {}) {
  const src = [...FakeSource.all].reverse().find((s) => !s.closed);
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

beforeEach(() => {
  FakeSource.all = [];
  vi.stubGlobal("EventSource", FakeSource);
});
afterEach(() => vi.unstubAllGlobals());

const RUNNERS: Runners = {
  claude: { available: true, version: "2", detail: "ok" },
  codex: { available: true, version: "1", detail: "ok" },
  gone: { available: false, version: null, detail: "x" },
};

const ROUTES: Routes = {
  "POST /api/agents/sessions/s1/messages": { ok: true },
  "POST /api/agents/sessions/s1/pending/w1/approve": { id: "w1", state: "approved" },
  "POST /api/agents/sessions/s1/pending/w1/reject": { id: "w1", state: "rejected" },
};

type Over = Partial<Omit<ChatRailProps, "session" | "onSessionNeeded">> & {
  start?: (runner: string) => Promise<string | null>;
  initial?: string | null;
  routes?: Routes;
};

function setup(over: Over = {}) {
  const env = fakeClient({ ...ROUTES, ...over.routes });
  const start = vi.fn(over.start ?? (async () => "s1"));
  let setSession: (id: string | null) => void = () => {};
  function Host() {
    const [session, set] = useState<string | null>(over.initial ?? null);
    setSession = set;
    return (
      <ChatRail
        runners={over.runners === undefined ? RUNNERS : over.runners}
        subject={over.subject ?? "Topic"}
        session={session}
        onSessionNeeded={async (runner) => {
          const id = await start(runner);
          if (id) set(id);
          return id;
        }}
        {...(over.footer ? { footer: over.footer } : {})}
        {...(over.actions ? { actions: over.actions } : {})}
        {...(over.placeholder ? { placeholder: over.placeholder } : {})}
        {...(over.label ? { label: over.label } : {})}
      />
    );
  }
  render(
    <ClientProvider client={env.client}>
      <ToastProvider>
        <Host />
      </ToastProvider>
    </ClientProvider>,
  );
  return { ...env, start, setSession: (id: string | null) => act(() => setSession(id)) };
}

const message = () => screen.getByRole("textbox", { name: "Message" });

describe("ChatRail", () => {
  it("starts a session with the chosen runner on the first message, then streams the reply", async () => {
    const footer = vi.fn(() => <p>footer here</p>);
    const { calls, start } = setup({ footer, label: "Chat about this post", placeholder: "Ask about this post" });
    expect(screen.getByRole("complementary", { name: "Chat about this post" })).toBeInTheDocument();
    expect(screen.getByText("Ask Claude Code about Topic.")).toBeInTheDocument();
    expect(message()).toHaveAttribute("placeholder", "Ask about this post");
    expect(screen.getByText("footer here")).toBeInTheDocument();
    const runner = screen.getByRole("combobox", { name: "Runner" });
    expect([...runner.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["Claude Code", "Codex"]);
    await userEvent.selectOptions(runner, "codex");
    expect(screen.getByText("Ask Codex about Topic.")).toBeInTheDocument();
    await userEvent.type(message(), "What is this?{Enter}");
    expect(start).toHaveBeenCalledWith("codex");
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/api/agents/sessions/s1/messages"))).toBe(true));
    const sent = calls.find((c) => c.url.endsWith("/messages"));
    expect(await sent?.json()).toEqual({ text: "What is this?" });
    expect(message()).toHaveValue("");
    expect(runner).toBeDisabled();
    expect(FakeSource.all[0]?.url).toContain("/api/agents/sessions/s1/events");
    agent("user_message", { text: "What is this?" });
    agent("text", { text: "See [x](javascript:alert(1)) and " });
    agent("text", { text: "**bold**." });
    agent("turn_done", { model: "m1" });
    // R17: agent Markdown is rendered safely, never as HTML.
    expect(await screen.findByText("bold")).toBeInstanceOf(HTMLElement);
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(document.querySelector("a[href^='javascript']")).toBeNull();
    expect(footer).toHaveBeenLastCalledWith(
      expect.objectContaining({
        busy: false,
        lastUserText: "What is this?",
        lastReply: { text: expect.any(String), model: "m1" },
      }),
    );
  });

  it("sends nothing when no session could be started", async () => {
    const { calls, start } = setup({ start: async () => null });
    await userEvent.type(message(), "hi{Enter}");
    await waitFor(() => expect(start).toHaveBeenCalled());
    expect(calls).toHaveLength(0);
    expect(message()).toHaveValue("hi"); // kept, so it can be sent again
    expect(screen.getByRole("combobox", { name: "Runner" })).toBeEnabled();
  });

  it("uses an existing session, and a new one starts a new conversation", async () => {
    const { calls, start, setSession } = setup({ initial: "s1" });
    await userEvent.type(message(), "  {Enter}"); // blank: nothing
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(calls).toHaveLength(0);
    await userEvent.type(message(), "again");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(start).not.toHaveBeenCalled();
    agent("user_message", { text: "again" });
    expect(screen.getByText("again")).toBeInTheDocument();
    setSession(null);
    expect(screen.queryByText("again")).toBeNull();
    expect(FakeSource.all[0]?.closed).toBe(true);
  });

  it("approves and rejects pending writes, retries, and says why a send failed", async () => {
    const { calls } = setup({
      initial: "s1",
      routes: { "POST /api/agents/sessions/s1/messages": () => Response.json({ detail: "busy" }, { status: 409 }) },
    });
    agent("pending_write", {
      pending_id: "w1",
      tool: "kb_write",
      args: { title: "Draft", folder: "Notes", body: "x" },
      target: { action: "create", path: "Notes/Draft.md", title: "Draft" },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/pending/w1/reject"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Approve change" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/pending/w1/approve"))).toBe(true));
    agent("user_message", { text: "hi" });
    agent("error", { message: "Stopped" });
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect((await screen.findAllByText("Couldn't send: busy. Press Send to try again.")).length).toBeGreaterThan(0);
    const retried = calls.filter((c) => c.url.endsWith("/messages"));
    expect(await retried[0]?.json()).toEqual({ text: "hi" });
  });

  it("says why an approval failed and offers it again", async () => {
    const { calls } = setup({
      initial: "s1",
      routes: {
        "POST /api/agents/sessions/s1/pending/w1/approve": () =>
          Response.json({ detail: "write gate closed" }, { status: 409 }),
        "POST /api/agents/sessions/s1/pending/w1/reject": () => Response.json({ detail: "gone" }, { status: 404 }),
      },
    });
    agent("pending_write", {
      pending_id: "w1",
      tool: "kb_write",
      args: { body: "x" },
      target: { action: "create", path: "N.md" },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Approve change" }));
    expect((await screen.findAllByText("Couldn't approve the change: write gate closed")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve change" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect((await screen.findAllByText("Couldn't reject the change: gone")).length).toBeGreaterThan(0);
    expect(calls.filter((c) => c.url.includes("/pending/"))).toHaveLength(2);
  });

  it("a send that gets no answer keeps the text and says why", async () => {
    setup({
      initial: "s1",
      routes: { "POST /api/agents/sessions/s1/messages": () => Promise.reject(new TypeError("Failed to fetch")) },
    });
    await userEvent.type(message(), "Keep this{Enter}");
    expect(
      (
        await screen.findAllByText(
          "Couldn't send: Failed to fetch. Check that Jesinia is running, then press Send again.",
        )
      ).length,
    ).toBeGreaterThan(0);
    expect(message()).toHaveValue("Keep this");
  });

  it("a refused send keeps the text too; an accepted one clears it unless you typed more", async () => {
    let refuse = true;
    let answer: (r: Response) => void = () => {};
    setup({
      initial: "s1",
      routes: {
        "POST /api/agents/sessions/s1/messages": () =>
          refuse
            ? Response.json({ detail: "busy" }, { status: 409 })
            : new Promise<Response>((resolve) => (answer = resolve)),
      },
    });
    await userEvent.type(message(), "First{Enter}");
    expect((await screen.findAllByText("Couldn't send: busy. Press Send to try again.")).length).toBeGreaterThan(0);
    expect(message()).toHaveValue("First");
    refuse = false;
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await userEvent.type(message(), " and more"); // typed while the send is on its way
    await act(async () => answer(Response.json({ ok: true })));
    expect(message()).toHaveValue("First and more");
  });

  it("a session that can't be started for a send says why and keeps the text", async () => {
    setup({ start: () => Promise.reject(new TypeError("Failed to fetch")) });
    await userEvent.type(message(), "hello{Enter}");
    expect(
      (
        await screen.findAllByText(
          "Couldn't send: Failed to fetch. Check that Jesinia is running, then press Send again.",
        )
      ).length,
    ).toBeGreaterThan(0);
    expect(message()).toHaveValue("hello");
  });

  it("an approval or rejection that gets no answer goes back to awaiting, with the cause", async () => {
    setup({
      initial: "s1",
      routes: {
        "POST /api/agents/sessions/s1/pending/w1/approve": () => Promise.reject(new TypeError("Failed to fetch")),
        "POST /api/agents/sessions/s1/pending/w1/reject": () => Promise.reject(new TypeError("Failed to fetch")),
      },
    });
    agent("pending_write", {
      pending_id: "w1",
      tool: "kb_write",
      args: { body: "x" },
      target: { action: "create", path: "N.md" },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Approve change" }));
    expect(
      (
        await screen.findAllByText(
          "Couldn't approve the change: Failed to fetch. Check that Jesinia is running, then try again.",
        )
      ).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve change" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(
      (
        await screen.findAllByText(
          "Couldn't reject the change: Failed to fetch. Check that Jesinia is running, then try again.",
        )
      ).length,
    ).toBeGreaterThan(0);
  });

  it("explains when no agent is available, and shows the app's own actions", () => {
    setup({ runners: { codex: { available: false, version: null, detail: "x" } }, actions: <button>Done</button> });
    expect(
      screen.getByText("No agent is available. Install and log in to Claude Code or Codex, then reload."),
    ).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText("Ask the agent about Topic.")).toBeInTheDocument();
  });

  it("waits for runners before saying none is available", () => {
    setup({ runners: null, footer: (chat) => <span>{chat.busy ? "busy" : "idle"}</span> });
    expect(screen.queryByText(/No agent is available/)).toBeNull();
    expect(screen.getByText("idle")).toBeInTheDocument();
  });
});
