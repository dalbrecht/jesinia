// SPDX-License-Identifier: AGPL-3.0-or-later
import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { pageFromPath } from "../api";
import { findMarkers, provenance } from "../provenance";

describe("routes", () => {
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
