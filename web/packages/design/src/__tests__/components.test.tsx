// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  Badge,
  Banner,
  BannerRegion,
  Button,
  ChatTranscript,
  CommandPalette,
  Input,
  List,
  Panel,
  Row,
  StatusIndicator,
  Tabs,
  Toast,
  ToastProvider,
  relativeTime,
  toneFor,
  useToast,
  type ChatMessage,
  type PaletteItem,
} from "../index";

const claude = { id: "claude" as const, name: "Claude" };

afterEach(() => {
  vi.useRealTimers();
});

const toasts = () => [...document.querySelectorAll("[data-toast]")].map((t) => t.textContent ?? "");

describe("Button", () => {
  it("loading sets aria-busy, stays focusable, and ignores clicks", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save note
      </Button>,
    );
    const b = screen.getByRole("button", { name: "Save note" });
    expect(b).toHaveAttribute("aria-busy", "true");
    expect(b).toHaveAttribute("aria-disabled", "true");
    expect(b).not.toBeDisabled();
    await userEvent.click(b);
    expect(onClick).not.toHaveBeenCalled();
    expect(b.querySelector("[data-spinner]")).not.toBeNull();
  });

  it("renders variants, icon-only labels, and submit type", async () => {
    const onClick = vi.fn();
    render(
      <>
        <Button variant="primary" size="sm" icon="+" iconTrailing="→" onClick={onClick}>
          Add
        </Button>
        <Button aria-label="More" icon="…" variant="ghost" />
        <Button type="submit" variant="danger" disabled>
          Rotate token
        </Button>
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Add/ }));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "More" })).toHaveAttribute("type", "button");
    expect(screen.getByRole("button", { name: "Rotate token" })).toHaveAttribute("type", "submit");
    expect(screen.getByRole("button", { name: "Rotate token" })).toBeDisabled();
  });

  it("link buttons navigate, and disabled ones cannot", () => {
    render(
      <>
        <Button href="/notes/">Open Notes</Button>
        <Button href="/rss/" disabled>
          Open RSS
        </Button>
      </>,
    );
    expect(screen.getByRole("link", { name: "Open Notes" })).toHaveAttribute("href", "/notes/");
    const off = screen.getByRole("link", { name: "Open RSS" });
    expect(off).not.toHaveAttribute("href");
    expect(off).toHaveAttribute("aria-disabled", "true");
    expect(off).toHaveAttribute("tabindex", "-1");
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    off.dispatchEvent(click);
    expect(click.defaultPrevented).toBe(true);
    const enter = fireEvent.keyDown(off, { key: "Enter" });
    expect(enter).toBe(false);
    expect(fireEvent.keyDown(off, { key: "a" })).toBe(true);
  });
});

describe("Input", () => {
  it("labels, describes, and marks errors", () => {
    render(<Input label="KB folder" variant="path" required help="Absolute path" error="Folder not found" />);
    const f = screen.getByLabelText("KB folder (required)");
    expect(f).toHaveAttribute("aria-invalid", "true");
    expect(f).toHaveAccessibleDescription(/Folder not found/);
    expect(screen.queryByText("Absolute path")).toBeNull();
    expect(f).toHaveAttribute("spellcheck", "false");
  });

  it("search clears on Escape and with the clear button", async () => {
    const onChange = vi.fn();
    render(<Input label="Search" variant="search" hideLabel loading onChange={onChange} />);
    const f = screen.getByRole("searchbox", { name: "Search" });
    expect(f).toHaveAttribute("aria-busy", "true");
    await userEvent.type(f, "ab");
    expect(screen.getByRole("button", { name: "Clear search" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(f).toHaveValue("");
    await userEvent.type(f, "c");
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(f).toHaveValue("");
    expect(onChange).toHaveBeenCalled();
    const outer = vi.fn();
    render(
      <div onKeyDown={outer}>
        <Input label="Empty" variant="search" />
      </div>,
    );
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "Empty" }), { key: "Escape" });
    expect(outer).toHaveBeenCalled();
  });

  it("composer sends on Enter, not Shift+Enter or during IME, and grows", async () => {
    const onSubmit = vi.fn();
    render(<Input label="Message" multiline onSubmit={onSubmit} help="Enter to send" />);
    const t = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(t, "hi{Shift>}{Enter}{/Shift}there");
    expect(t).toHaveAttribute("rows", "2");
    fireEvent.keyDown(t, { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    await userEvent.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("hi\nthere");
    render(<Input label="Plain" multiline value="a" onChange={() => {}} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Plain" }), { key: "Enter" });
  });

  it("controlled text input with adornments", () => {
    render(<Input label="Name" value="x" onChange={() => {}} leading="@" trailing={<span>ok</span>} disabled />);
    expect(screen.getByLabelText("Name")).toHaveValue("x");
    expect(screen.getByText("ok")).toBeInTheDocument();
  });
});

describe("Panel and Badge", () => {
  it("labels a titled panel and renders a div without one", () => {
    const { container } = render(
      <>
        <Panel title="Sync" headingLevel={3} actions={<button type="button">Run</button>} footer="f">
          body
        </Panel>
        <Panel variant="flush" actions="a">
          plain
        </Panel>
      </>,
    );
    expect(screen.getByRole("region", { name: "Sync" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Sync" })).toBeInTheDocument();
    expect(container.querySelectorAll("section")).toHaveLength(1);
  });

  it("status badges hide their icon; tones are stable", () => {
    render(
      <Badge tone="success" icon="✓">
        Synced
      </Badge>,
    );
    expect(screen.getByText("✓")).toHaveAttribute("aria-hidden", "true");
    expect(toneFor("Research")).toBe(toneFor("research"));
    expect(new Set(["a", "b", "c", "d", "e", "f", "g", "h"].map(toneFor)).size).toBeGreaterThan(1);
  });
});

describe("Tabs", () => {
  function Harness() {
    const [sel, setSel] = useState("a");
    return (
      <Tabs
        aria-label="Views"
        selected={sel}
        onSelect={setSel}
        tabs={[
          { id: "a", label: "All", count: 3 },
          { id: "b", label: "Unread", disabled: true },
          { id: "c", label: "Saved" },
        ]}
      >
        {(id) => <p>panel {id}</p>}
      </Tabs>
    );
  }

  it("arrow keys rove focus, wrap, and skip disabled tabs", async () => {
    render(<Harness />);
    const all = screen.getByRole("tab", { name: /All/ });
    all.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("panel c");
    await userEvent.keyboard("{ArrowRight}");
    expect(all).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(all).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveFocus();
    await userEvent.keyboard("x");
    await userEvent.click(screen.getByRole("tab", { name: "Unread" }));
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(all);
    expect(all).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Saved" })).toHaveAttribute("tabindex", "-1");
  });
});

type It = { id: string; title: string; off?: boolean };
const ITEMS: It[] = [
  { id: "1", title: "One" },
  { id: "2", title: "Two", off: true },
  { id: "3", title: "Three" },
];

describe("List and Row", () => {
  function Harness({ selection }: { selection: "single" | "multiple" }) {
    const [sel, setSel] = useState<string[]>([]);
    return (
      <>
        <List
          aria-label="Items"
          items={ITEMS}
          getKey={(i) => i.id}
          isDisabled={(i) => i.off === true}
          selection={selection}
          selected={sel}
          onSelectionChange={setSel}
          onActivate={(k) => (document.title = `open ${k}`)}
          renderRow={(i) => (
            <Row title={i.title} unread={i.id === "1"} excerpt="ex" meta="meta" disabled={i.off === true} />
          )}
        />
        <output>{sel.join(",")}</output>
      </>
    );
  }

  it("single: selection follows focus; disabled rows are skipped", async () => {
    render(<Harness selection="single" />);
    const opts = screen.getAllByRole("option");
    opts[0]?.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(opts[2]).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("3");
    await userEvent.keyboard("k");
    expect(opts[0]).toHaveFocus();
    await userEvent.keyboard("j{End}{Home}{Enter}");
    expect(document.title).toBe("open 1");
    await userEvent.keyboard(" ");
    await userEvent.click(opts[2] as HTMLElement);
    expect(screen.getByRole("status")).toHaveTextContent("3");
    await userEvent.dblClick(opts[2] as HTMLElement);
    expect(document.title).toBe("open 3");
    await userEvent.click(opts[1] as HTMLElement);
    expect(screen.getByRole("status")).toHaveTextContent("3");
  });

  it("multiple: space toggles, shift extends, ⌘A selects all", async () => {
    render(<Harness selection="multiple" />);
    expect(screen.getByRole("listbox")).toHaveAttribute("aria-multiselectable", "true");
    const opts = screen.getAllByRole("option");
    opts[0]?.focus();
    await userEvent.keyboard(" ");
    expect(screen.getByRole("status")).toHaveTextContent("1");
    await userEvent.keyboard(" ");
    expect(screen.getByRole("status")).toHaveTextContent(/^$/);
    await userEvent.keyboard("{Shift>}{ArrowDown}{/Shift}");
    expect(screen.getByRole("status")).toHaveTextContent("1,3");
    await userEvent.keyboard("{ArrowUp}{Meta>}a{/Meta}");
    expect(screen.getByRole("status")).toHaveTextContent("1,3");
    await userEvent.click(opts[0] as HTMLElement);
    expect(screen.getByRole("status")).toHaveTextContent(/^1$/);
    await userEvent.click(opts[2] as HTMLElement);
    fireEvent.click(opts[0] as HTMLElement, { metaKey: true });
    fireEvent.click(opts[0] as HTMLElement, { metaKey: true });
    fireEvent.click(opts[2] as HTMLElement, { shiftKey: true });
    expect(screen.getByRole("status")).not.toHaveTextContent(/^$/);
  });

  it("plain, loading and empty lists", () => {
    const { rerender } = render(
      <List
        aria-label="L"
        items={ITEMS}
        getKey={(i) => i.id}
        renderRow={(i) => <Row title={i.title} />}
        density="compact"
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    rerender(<List aria-label="L" items={ITEMS} getKey={(i) => i.id} renderRow={() => null} loading />);
    expect(document.querySelectorAll("[data-skeleton]")).toHaveLength(3);
    rerender(<List aria-label="L" items={[]} getKey={(i: It) => i.id} renderRow={() => null} />);
    expect(screen.getByText("Nothing here yet.")).toBeInTheDocument();
    rerender(<List aria-label="L" items={[]} getKey={(i: It) => i.id} renderRow={() => null} empty="No feeds" />);
    expect(screen.getByText("No feeds")).toBeInTheDocument();
  });
});

describe("Toast", () => {
  function Fire({ opts }: { opts: Parameters<ReturnType<typeof useToast>["show"]>[0][] }) {
    const t = useToast();
    return (
      <button type="button" onClick={() => opts.forEach((o) => t.show(o))}>
        fire
      </button>
    );
  }

  it("announces politely, auto-dismisses info, and keeps danger", () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <Fire opts={[{ message: "Saved" }, { tone: "danger", message: "Couldn't sync" }]} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("fire"));
    expect(within(screen.getByRole("status")).getByText("Saved")).toBeInTheDocument();
    expect(within(screen.getByRole("alert")).getByText("Couldn't sync")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-toast]")).toHaveLength(2);
    act(() => vi.advanceTimersByTime(5000));
    expect(document.querySelectorAll("[data-toast]")).toHaveLength(1);
    vi.useRealTimers();
  });

  it("caps at three, queues persistent overflow, and dismisses", () => {
    render(
      <ToastProvider>
        <Fire
          opts={[
            { tone: "warn", message: "w1" },
            { tone: "warn", message: "w2" },
            { message: "act", action: { label: "Undo", onAction: () => {} } },
            { tone: "danger", message: "d4" },
          ]}
        />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("fire"));
    expect(document.querySelectorAll("[data-toast]")).toHaveLength(3);
    fireEvent.click(screen.getAllByRole("button", { name: "Dismiss" })[0] as HTMLElement);
    expect(toasts().some((t) => t.includes("d4"))).toBe(true);
  });

  it("evicts the oldest dismissible toast, pauses on hover, and moves focus with F6", () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <Fire
          opts={[
            { message: "a" },
            { tone: "warn", message: "b" },
            { tone: "warn", message: "c" },
            { message: "d", duration: 100 },
          ]}
        />
      </ToastProvider>,
    );
    const fire = screen.getByText("fire");
    fireEvent.click(fire);
    expect(toasts().some((t) => t.startsWith("ia"))).toBe(false);
    const region = screen.getByRole("region", { name: "Notifications" });
    fireEvent.mouseEnter(region);
    act(() => vi.advanceTimersByTime(200));
    expect(toasts().some((t) => t.startsWith("id"))).toBe(true);
    fireEvent.mouseLeave(region);
    act(() => vi.advanceTimersByTime(200));
    expect(toasts().some((t) => t.startsWith("id"))).toBe(false);
    fire.focus();
    fireEvent.keyDown(window, { key: "F6" });
    expect(region).toHaveFocus();
    fireEvent.focus(region);
    fireEvent.keyDown(region, { key: "Escape" });
    expect(fire).toHaveFocus();
    fireEvent.keyDown(window, { key: "F6" });
    fireEvent.keyDown(window, { key: "F6" });
    expect(fire).toHaveFocus();
    fireEvent.blur(region, { relatedTarget: null });
    fireEvent.keyDown(region, { key: "a" });
    vi.useRealTimers();
  });

  it("useToast outside the provider throws; Toast renders an action", () => {
    const Bad = () => {
      useToast();
      return null;
    };
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Bad />)).toThrow("ToastProvider");
    const onAction = vi.fn();
    render(<Toast id="x" tone="success" message="Done" action={{ label: "View", onAction }} onDismiss={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(onAction).toHaveBeenCalled();
  });
});

describe("ChatTranscript", () => {
  const pending: ChatMessage = {
    id: "w1",
    kind: "pending-write",
    path: "Notes/maintenance.md",
    summary: "Adds a section, 12 lines",
    diff: "+ new line",
    state: "awaiting",
  };

  it("renders a pending-write card whose Approve calls onApprove(id)", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<ChatTranscript runner={claude} messages={[pending]} onApprove={onApprove} onReject={onReject} />);
    expect(screen.getByText(/wants to edit/)).toHaveTextContent("Claude wants to edit Notes/maintenance.md");
    await userEvent.click(screen.getByRole("button", { name: /Show changes/ }));
    expect(screen.getByText("+ new line")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve change" }));
    expect(onApprove).toHaveBeenCalledWith("w1");
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onReject).toHaveBeenCalledWith("w1");
  });

  it("shows each pending-write state", () => {
    const { rerender } = render(<ChatTranscript runner={claude} messages={[{ ...pending, state: "approving" }]} />);
    expect(screen.getByRole("button", { name: "Approve change" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    rerender(<ChatTranscript runner={claude} messages={[{ ...pending, state: "failed", error: "File changed" }]} />);
    expect(screen.getByText("File changed")).toBeInTheDocument();
    rerender(<ChatTranscript runner={claude} messages={[{ ...pending, state: "approved" }]} />);
    expect(screen.getByText(/Approved · Notes\/maintenance.md/)).toBeInTheDocument();
    rerender(<ChatTranscript runner={claude} messages={[{ ...pending, state: "rejected" }]} />);
    expect(screen.getByText("Rejected · Notes/maintenance.md")).toBeInTheDocument();
    rerender(<ChatTranscript runner={claude} messages={[{ ...pending, state: "superseded" }]} />);
    expect(screen.getByText("Replaced by a newer change")).toBeInTheDocument();
  });

  it("announces new agent text politely, never token by token", () => {
    const at = new Date().toISOString();
    const { rerender } = render(<ChatTranscript runner={claude} messages={[]} />);
    expect(screen.getByText("Ask Claude about this page.")).toBeInTheDocument();
    const announcer = () => document.querySelector("[data-announcer]") as HTMLElement;
    expect(announcer()).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("log")).toHaveAttribute("aria-live", "off");
    rerender(
      <ChatTranscript runner={claude} messages={[{ id: "a", kind: "agent", markdown: "He", streaming: true, at }]} />,
    );
    expect(announcer()).toHaveTextContent("Claude is responding");
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[{ id: "a", kind: "agent", markdown: "Hello", streaming: true, at }]}
      />,
    );
    expect(announcer()).toHaveTextContent("Claude is responding");
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[{ id: "a", kind: "agent", markdown: "Hello there", streaming: false, at }]}
      />,
    );
    expect(announcer()).toHaveTextContent("Hello there");
    const long = "x".repeat(400);
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[
          { id: "a", kind: "agent", markdown: "Hello there", streaming: false, at },
          { id: "b", kind: "agent", markdown: long, streaming: false, at },
          pending,
        ]}
      />,
    );
    expect(announcer()).toHaveTextContent("Claude wants to edit Notes/maintenance.md. Approve or reject.");
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[
          { id: "a", kind: "agent", markdown: "Hello there", streaming: false, at },
          { id: "c", kind: "agent", markdown: long, streaming: false, at },
        ]}
      />,
    );
    expect(announcer()).toHaveTextContent("Message continues in the transcript");
  });

  it("does not auto-scroll when the user has scrolled up", () => {
    const at = new Date().toISOString();
    const first: ChatMessage[] = [{ id: "u1", kind: "user", text: "hi", at }];
    const { rerender } = render(<ChatTranscript runner={claude} messages={first} />);
    const log = screen.getByRole("log");
    Object.defineProperty(log, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(log, "clientHeight", { configurable: true, value: 400 });
    log.scrollTop = 100;
    fireEvent.scroll(log);
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[...first, { id: "a1", kind: "agent", markdown: "new", streaming: false, at }]}
      />,
    );
    expect(log.scrollTop).toBe(100);
    const jump = screen.getByRole("button", { name: "1 new" });
    fireEvent.click(jump);
    expect(log.scrollTop).toBe(2000);
    expect(screen.queryByRole("button", { name: /new$/ })).toBeNull();
    log.scrollTop = 0;
    fireEvent.scroll(log);
    rerender(
      <ChatTranscript
        runner={claude}
        messages={[
          ...first,
          { id: "a1", kind: "agent", markdown: "new", streaming: false, at },
          { id: "u2", kind: "user", text: "more", at },
        ]}
      />,
    );
    expect(log.scrollTop).toBe(2000); // sending always scrolls to the bottom
    log.focus();
    fireEvent.keyDown(log, { key: "ArrowUp", metaKey: true });
    expect(log.scrollTop).toBe(0);
    fireEvent.keyDown(log, { key: "ArrowDown", metaKey: true });
    expect(log.scrollTop).toBe(2000);
    fireEvent.keyDown(log, { key: "ArrowDown" });
    fireEvent.keyDown(log, { key: "x", metaKey: true });
  });

  it("tool rows group and expand; errors retry and alert; done messages copy and save", async () => {
    const at = new Date().toISOString();
    const onRetry = vi.fn();
    const onSaveToKb = vi.fn();
    const writeText = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });
    const base: ChatMessage[] = [
      { id: "t1", kind: "tool", name: "kb.search", summary: "3 results", detail: "args", running: false },
      { id: "t2", kind: "tool", name: "kb.read", summary: "1 page", running: false },
      { id: "a", kind: "agent", markdown: "**done**", streaming: false, at },
      { id: "t3", kind: "tool", name: "kb.write", summary: "", running: true },
    ];
    const { rerender } = render(
      <ChatTranscript
        runner={{ id: "codex", name: "Codex" }}
        messages={base}
        onRetry={onRetry}
        onSaveToKb={onSaveToKb}
        renderMarkdown={(md) => <b>{md}</b>}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /2 tool calls/ }));
    await userEvent.click(screen.getByRole("button", { name: /kb.search 3 results/ }));
    expect(screen.getByText("args")).toBeInTheDocument();
    expect(screen.getByText("kb.write…")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("**done**");
    await userEvent.click(screen.getByRole("button", { name: "Save to KB" }));
    expect(onSaveToKb).toHaveBeenCalledWith("a");
    rerender(
      <ChatTranscript
        runner={{ id: "codex", name: "Codex" }}
        messages={[...base, { id: "e", kind: "error", message: "Codex stopped: run codex login", retryable: true }]}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Codex stopped");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith("e");
    await userEvent.click(screen.getByRole("button", { name: "kb.read 1 page" }));
    expect(screen.getAllByText("1 page").length).toBeGreaterThan(0);
  });

  it("formats relative times", () => {
    const now = new Date("2026-10-16T12:00:00Z");
    expect(relativeTime("2026-10-16T11:59:30Z", now)).toBe("just now");
    expect(relativeTime("2026-10-16T11:45:00Z", now)).toBe("15 min ago");
    expect(relativeTime("2026-10-16T09:00:00Z", now)).toMatch(/\d/);
    expect(relativeTime("nope", now)).toBe("");
  });
});

describe("CommandPalette", () => {
  const cmd = (id: string, label: string, onRun = vi.fn()): PaletteItem => ({ id, label, group: "command", onRun });

  it("opens on ⌘K, filters, moves with arrows, runs, and restores focus", async () => {
    const sync = vi.fn();
    const other = vi.fn();
    render(
      <>
        <button type="button">invoker</button>
        <CommandPalette commands={[cmd("sync", "Sync now", sync), cmd("theme", "Toggle theme", other)]} />
      </>,
    );
    screen.getByText("invoker").focus();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const box = screen.getByRole("combobox");
    expect(box).toHaveFocus();
    expect(screen.getByRole("dialog", { name: "Command palette" })).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(box.getAttribute("aria-activedescendant")).toContain("theme");
    fireEvent.keyDown(box, { key: "ArrowUp" });
    fireEvent.keyDown(box, { key: "Tab" });
    fireEvent.keyDown(box, { key: "Enter", metaKey: true });
    expect(sync).toHaveBeenCalledWith({ newWindow: true });
    expect(screen.getByText("invoker")).toHaveFocus();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("merges async app and KB results with a 100 ms debounce, capped per group", async () => {
    const appSearch = vi.fn(async (q: string) =>
      Array.from({ length: 8 }, (_, i) => ({
        id: `n${i}`,
        label: `${q} note ${i}`,
        group: "app" as const,
        onRun: vi.fn(),
      })),
    );
    const kbSearch = vi.fn(async () => {
      throw new Error("offline");
    });
    render(<CommandPalette appSearch={appSearch} kbSearch={kbSearch} commands={[cmd("x", "Open RSS")]} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.type(screen.getByRole("combobox"), "budget");
    expect(await screen.findByText("budget note 0")).toBeInTheDocument();
    expect(appSearch).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Show all 8")).toBeInTheDocument();
    expect(screen.queryByText("budget note 7")).toBeNull();
    await userEvent.click(screen.getByText("Show all 8"));
    expect(screen.getByText("budget note 7")).toBeInTheDocument();
    fireEvent.mouseMove(screen.getByText("budget note 2"));
    await userEvent.click(screen.getByText("budget note 2"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows no matches and closes from the scrim", async () => {
    render(<CommandPalette commands={[cmd("x", "Open RSS")]} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.type(screen.getByRole("combobox"), "zzz");
    expect(await screen.findByText("No matches")).toBeInTheDocument();
    fireEvent.click(document.querySelector("[data-scrim]") as HTMLElement);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("Banner and StatusIndicator", () => {
  it("banners present at load are silent; later ones announce; extra banners collapse", () => {
    const onMore = vi.fn();
    const act1 = vi.fn();
    const { rerender } = render(
      <BannerRegion onMore={onMore}>
        <Banner tone="danger" cause="KB missing:" action={{ label: "Open settings", onAction: act1 }}>
          choose a folder.
        </Banner>
      </BannerRegion>,
    );
    expect(screen.getByRole("region", { name: "System status" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
    expect(act1).toHaveBeenCalled();
    rerender(
      <BannerRegion onMore={onMore}>
        <Banner tone="danger" cause="KB missing:">
          choose a folder.
        </Banner>
        <Banner tone="warn" cause="Sync paused:" secondaryAction={{ label: "Details", onAction: () => {} }}>
          resolve the conflict.
        </Banner>
        <Banner tone="warn" cause="Third:">
          x
        </Banner>
      </BannerRegion>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Sync paused:");
    fireEvent.click(screen.getByRole("button", { name: "+1 more" }));
    expect(onMore).toHaveBeenCalled();
    render(
      <Banner tone="warn" cause="Alone:">
        outside a region
      </Banner>,
    );
  });

  it("summarizes status, opens a popover, and closes with Escape or outside click", async () => {
    const { rerender } = render(<StatusIndicator items={[]} />);
    expect(screen.getByRole("button", { name: "Status: all clear" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Status: all clear" }));
    expect(screen.getByText("Everything is working.")).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByText("Everything is working.")).toBeNull();
    rerender(
      <StatusIndicator
        items={[
          { id: "1", severity: "minor", title: "Duplicate ids", detail: "Fix in Settings", href: "/settings/#kb" },
          { id: "2", severity: "blocking", title: "Sync paused", detail: "Resolve" },
        ]}
      />,
    );
    const btn = screen.getByRole("button", { name: "Status: 2 issues" });
    await userEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Open settings" })).toHaveAttribute("href", "/settings/#kb");
    fireEvent.mouseDown(screen.getByText("Sync paused"));
    await userEvent.keyboard("{Escape}");
    expect(btn).toHaveFocus();
    expect(btn).toHaveAttribute("aria-expanded", "false");
    rerender(<StatusIndicator items={[{ id: "1", severity: "minor", title: "One", detail: "d" }]} />);
    expect(screen.getByRole("button", { name: "Status: 1 issue" })).toBeInTheDocument();
  });
});
