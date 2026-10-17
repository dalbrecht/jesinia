// SPDX-License-Identifier: AGPL-3.0-or-later
// Moved with the chat from Notes (Task 14): the shared rail's event reducer.
import { describe, expect, it } from "vitest";
import { EMPTY, lineDiff, markApproving, reduce, type AgentEvent, type ChatState } from "../index";

let seq = 0;
const ev = (kind: string, data: Record<string, unknown> = {}): AgentEvent => ({
  seq: ++seq,
  kind,
  data,
  origin: "jesinia",
});
const run = (events: AgentEvent[], from: ChatState = EMPTY) => events.reduce((s, e) => reduce(s, e, "t"), from);

describe("chat events", () => {
  it("streams deltas into one agent message and completes on turn_done", () => {
    let s = run([ev("user_message", { text: "Hi" }), ev("text", { text: "Hel" }), ev("text", { text: "lo" })]);
    expect(s.busy).toBe(true);
    expect(s.messages.filter((m) => m.kind === "agent")).toEqual([
      expect.objectContaining({ kind: "agent", markdown: "Hello", streaming: true }),
    ]);
    s = run([ev("turn_done", { model: "m1" })], s);
    expect(s.busy).toBe(false);
    expect(s.lastReply).toEqual({ text: "Hello", model: "m1" });
    expect(s.lastUserText).toBe("Hi");
    s = run([ev("user_message", { text: "Again" }), ev("turn_done", {})], s);
    expect(s.lastReply).toBeNull(); // never pair a new question with an earlier answer
  });

  it("shows terminal progress notes as completed messages that never leave the chat busy", () => {
    const term = (text: string): AgentEvent => ({ ...ev("text", { text }), origin: "terminal" });
    let s = run([ev("attached"), term("Reading the page"), term("Done")]);
    expect(s.busy).toBe(false);
    expect(s.messages.filter((m) => m.kind === "agent")).toEqual([
      expect.objectContaining({ markdown: "Reading the page", streaming: false }),
      expect.objectContaining({ markdown: "Done", streaming: false }),
    ]);
    s = run([ev("user_message", { text: "Hi" }), ev("text", { text: "Hel" }), term("note")], EMPTY);
    expect(s.busy).toBe(true); // a terminal note does not end the PWA's own turn
    expect(s.messages.filter((m) => m.kind === "agent")).toEqual([
      expect.objectContaining({ markdown: "Hel", streaming: true }),
      expect.objectContaining({ markdown: "note", streaming: false }),
    ]);
  });

  it("tracks tool calls, pending writes and their resolution", () => {
    let s = run([
      ev("tool_use", { name: "kb_search", input: { q: "a".repeat(80) } }),
      ev("tool_use", { name: "kb_write", input: { title: "Draft" } }),
      ev("tool_result", { name: "kb_write", ok: false }),
      ev("tool_result", { name: "kb_search", ok: true }),
      ev("tool_use", { name: "other", input: 3 }),
      ev("tool_use", { name: "x", input: { n: 1 } }),
      ev("pending_write", {
        pending_id: "p1",
        tool: "kb_write",
        args: { title: "Draft", folder: "Notes", body: "a\nb" },
        target: { action: "create", path: "Notes/Draft.md" },
      }),
      ev("pending_write", { pending_id: "p2", tool: "notes_save", args: { title: "N", text: "one" } }),
      ev("pending_write", {
        pending_id: "p3",
        tool: "kb_write",
        args: { id: "abc", title: "Harmless", folder: "Notes", body: "keep\nnew" },
        target: { action: "update", path: "Ideas/Real.md", current: "keep\nold" },
      }),
      ev("pending_write", {
        pending_id: "p4",
        tool: "kb_move",
        args: { id: "abc", folder: "Archive" },
        target: { action: "move", path: "Ideas/Real.md", to: "Archive/Real.md" },
      }),
      ev("pending_write", { pending_id: "p5", tool: "other", args: {}, target: { action: "unknown", error: "boom" } }),
      ev("pending_write", { pending_id: "p6", tool: "x", args: { body: "z" } }),
    ]);
    const tools = s.messages.filter((m) => m.kind === "tool");
    expect(tools.map((t) => (t.kind === "tool" ? [t.summary, t.running] : null))).toEqual([
      [`"${"a".repeat(60)}…"`, false],
      ['"Draft" · failed', false],
      ["", true],
      ["", true],
    ]);
    const pending = s.messages.filter((m) => m.kind === "pending-write");
    expect(pending.map((p) => (p.kind === "pending-write" ? [p.path, p.summary, p.diff] : null))).toEqual([
      ["Notes/Draft.md", "Creates a new page (2 lines).", "+ a\n+ b"],
      ["Notes/N.md", "Saves a note (1 line)", "+ one"],
      ["Ideas/Real.md", "Changes an existing page (2 lines)", "  keep\n- old\n+ new"],
      ["Ideas/Real.md", "Moves the page to Archive/Real.md", "- Ideas/Real.md\n+ Archive/Real.md"],
      ["other", "other: boom", ""],
      ["x", "x (1 line)", "+ z"],
    ]);
    s = markApproving(s, "p1");
    expect(s.messages.find((m) => m.id === "p1")).toMatchObject({ state: "approving" });
    s = run(
      [
        ev("pending_resolved", { pending_id: "p1", state: "approved" }),
        ev("pending_resolved", { pending_id: "p2", state: "rejected" }),
        ev("pending_resolved", { pending_id: "p3", state: "failed", result: { error: "StaleWrite" } }),
        ev("pending_resolved", { pending_id: "p4", state: "failed" }),
      ],
      s,
    );
    expect(
      s.messages
        .filter((m) => m.kind === "pending-write")
        .slice(0, 4)
        .map((m) => (m.kind === "pending-write" ? [m.state, m.error] : null)),
    ).toEqual([
      ["approved", undefined],
      ["rejected", undefined],
      ["failed", "StaleWrite"],
      ["failed", "The write failed."],
    ]);
  });

  it("diffs by sequence, shows moves and metadata, and caps huge inputs", () => {
    expect(lineDiff(["a", "b", "a"], ["a", "a"])).toBe("  a\n- b\n  a");
    expect(lineDiff(["x"], ["y", "x"])).toBe("+ y\n  x");
    const big = Array.from({ length: 2100 }, (_, i) => String(i));
    expect(lineDiff(big, big).startsWith("(too large to compare")).toBe(true);
    const s = run([
      ev("pending_write", {
        pending_id: "m1",
        tool: "kb_write",
        args: { id: "a", title: "T2", folder: "B", body: "same" },
        target: {
          action: "update+move",
          path: "A/T.md",
          to: "B/T2.md",
          new_title: "T2",
          current: "same",
          jesinia: { from: {}, to: { k: 1 } },
        },
      }),
    ]);
    expect(s.messages[0]).toMatchObject({
      path: "A/T.md",
      summary: "Changes an existing page and moves it to B/T2.md",
      diff: '~ moves to B/T2.md (title "T2")\n~ metadata: {} → {"k":1}\n  same',
    });
  });

  it("errors end the stream; attach and unknown events", () => {
    let s = run([ev("text", { text: "partial" }), ev("error", { message: "Claude stopped" })]);
    expect(s.messages[0]).toMatchObject({ streaming: false });
    expect(s.messages[1]).toMatchObject({ kind: "error", message: "Claude stopped", retryable: true });
    s = run([ev("error", {}), ev("attached"), ev("mystery")], s);
    expect(s.attached).toBe(true);
    expect(s.messages[2]).toMatchObject({ message: "The agent stopped." });
  });
});
