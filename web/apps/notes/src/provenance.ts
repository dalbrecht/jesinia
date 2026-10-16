// SPDX-License-Identifier: AGPL-3.0-or-later
// Provenance comments (`<!-- jesinia:ai … -->` … `<!-- /jesinia:ai -->`, kb-format) fold
// into read-only chips: the markers can't be edited or partly deleted from the editor.
import { EditorState, RangeSetBuilder, StateField, type Text } from "@codemirror/state";
import { Decoration, EditorView, WidgetType, type DecorationSet } from "@codemirror/view";

const OPEN = /^<!-- jesinia:ai\b(.*?)-->\s*$/;
const CLOSE = /^<!-- \/jesinia:ai -->\s*$/;

export type Marker = { from: number; to: number; label: string };

/** Marker lines in a document: their ranges and chip labels. */
export function findMarkers(doc: Text): Marker[] {
  const out: Marker[] = [];
  for (let i = 1; i <= doc.lines; i++) {
    const line = doc.line(i);
    const open = OPEN.exec(line.text);
    if (open) {
      const attrs = Object.fromEntries(
        [...(open[1] ?? "").matchAll(/(\w+)=(\S+)/g)].map((m) => [m[1] ?? "", m[2] ?? ""]),
      );
      out.push({
        from: line.from,
        to: line.to,
        label: `AI · ${attrs.agent ?? "agent"}${attrs.model ? ` · ${attrs.model}` : ""}`,
      });
    } else if (CLOSE.test(line.text)) {
      out.push({ from: line.from, to: line.to, label: "end of AI text" });
    }
  }
  return out;
}

class Chip extends WidgetType {
  constructor(readonly label: string) {
    super();
  }
  override eq(other: Chip) {
    return other.label === this.label;
  }
  toDOM() {
    const el = document.createElement("span");
    el.className = "jx-provenance";
    el.textContent = this.label;
    el.setAttribute("contenteditable", "false");
    el.title = "Written by an agent (provenance is kept automatically)";
    return el;
  }
}

function build(state: EditorState): DecorationSet {
  const b = new RangeSetBuilder<Decoration>();
  for (const m of findMarkers(state.doc)) b.add(m.from, m.to, Decoration.replace({ widget: new Chip(m.label) }));
  return b.finish();
}

const field = StateField.define<DecorationSet>({
  create: build,
  update: (deco, tr) => (tr.docChanged ? build(tr.state) : deco),
  provide: (f) => [EditorView.decorations.from(f), EditorView.atomicRanges.of((view) => view.state.field(f))],
});

/** Reject any edit that touches a marker line (insertions at its edges are fine). */
const guard = EditorState.transactionFilter.of((tr) => {
  if (!tr.docChanged || tr.isUserEvent("jx.external")) return tr;
  const markers = findMarkers(tr.startState.doc);
  let blocked = false;
  tr.changes.iterChangedRanges((fromA, toA) => {
    for (const m of markers) if (fromA < m.to && toA > m.from) blocked = true;
  });
  return blocked ? [] : tr;
});

export const provenance = [field, guard];
