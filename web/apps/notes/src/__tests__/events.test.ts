// SPDX-License-Identifier: AGPL-3.0-or-later
import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { pageFromPath } from "../api";
import { EMPTY, markApproving, reduce, type AgentEvent, type ChatState } from "../events";
import { findMarkers, provenance } from "../provenance";

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
    expect(s.lastReply?.text).toBe("Hello"); // no new agent text this turn
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
      ["Notes/Draft.md", "Creates a new page (2 lines)", "+ a\n+ b"],
      ["Notes/N.md", "Saves a note (1 line)", "+ one"],
      ["Ideas/Real.md", "Changes an existing page (2 lines)", "- old\n+ new"],
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

  it("errors end the stream; attach and unknown events", () => {
    let s = run([ev("text", { text: "partial" }), ev("error", { message: "Claude stopped" })]);
    expect(s.messages[0]).toMatchObject({ streaming: false });
    expect(s.messages[1]).toMatchObject({ kind: "error", message: "Claude stopped", retryable: true });
    s = run([ev("error", {}), ev("attached"), ev("mystery")], s);
    expect(s.attached).toBe(true);
    expect(s.messages[2]).toMatchObject({ message: "The agent stopped." });
  });

  it("parses page routes", () => {
    expect(pageFromPath("/notes/page/abc%20d")).toBe("abc d");
    expect(pageFromPath("/notes/")).toBeNull();
  });
});

describe("provenance", () => {
  const doc =
    "Mine.\n\n<!-- jesinia:ai agent=claude-code model=m at=x session=s -->\nTheirs.\n<!-- /jesinia:ai -->\nAfter.";

  it("finds marker lines with chip labels", () => {
    const state = EditorState.create({ doc });
    expect(findMarkers(state.doc).map((m) => m.label)).toEqual(["AI · claude-code · m", "end of AI text"]);
    expect(findMarkers(EditorState.create({ doc: "<!-- jesinia:ai -->" }).doc)[0]?.label).toBe("AI · agent");
  });

  it("rejects edits that touch a marker but allows edits elsewhere", () => {
    let state = EditorState.create({ doc, extensions: [provenance] });
    const marker = findMarkers(state.doc)[0];
    if (!marker) throw new Error("no marker");
    state = state.update({ changes: { from: marker.from + 3, to: marker.from + 6, insert: "" } }).state;
    expect(state.doc.toString()).toBe(doc);
    state = state.update({ changes: { from: 0, to: 4, insert: "Ours" } }).state;
    expect(state.doc.toString().startsWith("Ours.")).toBe(true);
    state = state.update({
      changes: { from: 0, to: state.doc.length, insert: "replaced" },
      userEvent: "jx.external",
    }).state;
    expect(state.doc.toString()).toBe("replaced");
  });
});
