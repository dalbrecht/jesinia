// SPDX-License-Identifier: AGPL-3.0-or-later
import { autocompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { useEffect, useRef } from "react";
import { provenance } from "./provenance";

const style = HighlightStyle.define([
  { tag: tags.heading1, fontSize: "1.5em", fontWeight: "600" },
  { tag: tags.heading2, fontSize: "1.25em", fontWeight: "600" },
  { tag: tags.heading3, fontSize: "1.1em", fontWeight: "600" },
  { tag: tags.strong, fontWeight: "600" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.link, color: "var(--jx-accent-text)" },
  { tag: tags.url, color: "var(--jx-fg-muted)" },
  { tag: tags.monospace, fontFamily: "var(--jx-font-mono)", fontSize: "0.9em" },
  { tag: tags.processingInstruction, color: "var(--jx-fg-subtle)" },
  { tag: tags.quote, color: "var(--jx-fg-muted)" },
]);

const theme = EditorView.theme({
  "&": { height: "100%", background: "transparent", color: "var(--jx-fg)" },
  ".cm-content": {
    fontFamily: "var(--jx-font-read)",
    fontSize: "var(--jx-text-read)",
    lineHeight: "var(--jx-leading-read)",
    maxWidth: "var(--jx-read-measure)",
    padding: "var(--jx-space-4) 0 var(--jx-space-8)",
    caretColor: "var(--jx-fg)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--jx-font-read)" },
  ".jx-provenance": {
    display: "inline-block",
    padding: "0 var(--jx-space-2)",
    borderRadius: "var(--jx-radius-full)",
    background: "var(--jx-ai)",
    color: "var(--jx-fg-muted)",
    fontFamily: "var(--jx-font-ui)",
    fontSize: "var(--jx-text-xs)",
  },
});

export type LinkTarget = { id: string; title: string };
export type LinkSearch = (q: string) => Promise<LinkTarget[]>;

/** `[[` completion: pick a page and insert `[[id|Title]]` (kb-format links resolve by id). */
export function linkCompletion(find: LinkSearch) {
  return async (ctx: CompletionContext): Promise<CompletionResult | null> => {
    const m = ctx.matchBefore(/\[\[[^\]|\n]*/);
    if (!m) return null;
    const q = m.text.slice(2);
    const pages = q ? await find(q) : [];
    return {
      from: m.from,
      filter: false,
      options: pages.map((p) => ({ label: p.title, detail: "page", apply: `[[${p.id}|${p.title}]]` })),
    };
  };
}

export type EditorProps = {
  value: string;
  onChange: (text: string) => void;
  onBlur?: () => void;
  findPages?: LinkSearch;
  label: string;
  /** Bumped to push `value` into the editor (e.g. "Use theirs"); otherwise the editor owns its text. */
  revision?: number;
};

export function Editor({ value, onChange, onBlur, findPages, label, revision = 0 }: EditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const handlers = useRef({ onChange, onBlur });
  handlers.current = { onChange, onBlur };

  useEffect(() => {
    if (!host.current) return;
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          markdown(),
          syntaxHighlighting(style),
          EditorView.lineWrapping,
          theme,
          provenance,
          placeholder("Start writing…"),
          EditorView.contentAttributes.of({ "aria-label": label, "aria-multiline": "true" }),
          autocompletion({ override: findPages ? [linkCompletion(findPages)] : [], activateOnTyping: true }),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && !u.transactions.some((t) => t.isUserEvent("jx.external"))) {
              handlers.current.onChange(u.state.doc.toString());
            }
          }),
          EditorView.domEventHandlers({ blur: () => handlers.current.onBlur?.() }),
        ],
      }),
    });
    view.current = v;
    return () => v.destroy();
    // Mount once per page (the parent keys this component by page id).
  }, []);

  useEffect(() => {
    const v = view.current;
    if (!v || revision === 0) return;
    v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value }, userEvent: "jx.external" });
    // Only an explicit revision bump replaces the text.
  }, [revision]);

  return <div ref={host} className="jx-editor" data-testid="editor" />;
}
