// SPDX-License-Identifier: AGPL-3.0-or-later
// Keep (n), spec §3.1: a small popover over the reader with a note, tags from the catalog
// (TagPicker: suggested in the background, near-duplicates caught) and, after Engage, the
// agent's takeaway to edit. ⌘↵ keeps, Esc cancels. Keeping is always the author's action.
// It is modal while open (the app's keys are off): focus stays inside and Esc works anywhere.
import { Button, Input } from "@jesinia/design";
import { TagPicker, useClient, type TagPickerHandle } from "@jesinia/shell";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { errorCause, keepPost, TAKEAWAY_MAX, type Kept, type KeepRequest, type Post } from "./api";
import s from "./rss.module.css";

export type KeepPopoverProps = {
  post: Post;
  /** Prefilled: the author's last message, when keeping with a conversation. */
  initialNote?: string;
  /** From Keep with this conversation: shown and editable; cleared, it is left out. */
  takeaway?: { text: string; session: string } | null;
  /** False when no runner is available: suggestions then come from the catalog alone. */
  hasAgent: boolean;
  onKept: (kept: Kept) => void;
  onCancel: () => void;
};

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
const FOCUSABLE = "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select, [href]";

const causeOf = errorCause;

export function KeepPopover({ post, initialNote = "", takeaway = null, hasAgent, onKept, onCancel }: KeepPopoverProps) {
  const client = useClient();
  const [note, setNote] = useState(initialNote);
  const [tags, setTags] = useState<string[]>([]);
  const [takeawayText, setTakeawayText] = useState(takeaway?.text ?? "");
  const [keeping, setKeeping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const noteBox = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const picker = useRef<TagPickerHandle>(null);
  /** In flight: a second ⌘↵ before React re-renders must not send again. */
  const busy = useRef(false);
  /** The idempotency key of the intended action, with the request it was for (R25). */
  const intent = useRef<{ key: string; request: string } | null>(null);
  const cancel = useRef(onCancel);
  cancel.current = onCancel;

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    noteBox.current?.focus();
    // Esc cancels even when focus has strayed (a click elsewhere); the app's keys are off.
    const onEscape = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) cancel.current();
    };
    document.addEventListener("keydown", onEscape);
    const popover = root.current;
    return () => {
      document.removeEventListener("keydown", onEscape);
      // Back to where the reader was, unless the author already moved on (clicked a row).
      const active = document.activeElement;
      if (active === null || active === document.body || popover?.contains(active)) opener?.focus();
    };
  }, []);

  const keep = async () => {
    if (busy.current) return;
    busy.current = true;
    setKeeping(true);
    setError(null);
    // A tag still typed in the box counts (R23); a near-match or an invalid tag stops here.
    const chosen = (await picker.current?.commitDraft()) ?? null;
    if (chosen === null) {
      busy.current = false;
      setKeeping(false);
      return;
    }
    const text = takeawayText.trim();
    const request: KeepRequest = {
      note: note.trim(),
      tags: chosen,
      ...(takeaway && text ? { takeaway: { text, session: takeaway.session } } : {}),
    };
    // The same request again is a retry of the same action; any edit makes a new one.
    const body = JSON.stringify(request);
    if (intent.current?.request !== body) intent.current = { key: crypto.randomUUID(), request: body };
    try {
      onKept(await keepPost(client, post.id, request, intent.current.key));
    } catch (e) {
      setError(causeOf(e));
      busy.current = false;
      setKeeping(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void keep();
    } else if (e.key === "Tab") {
      // Modal: Tab and Shift+Tab cycle through the popover's own controls.
      const controls = [...(root.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      }
    }
  };

  return (
    <div
      ref={root}
      className={s.keep}
      role="dialog"
      aria-modal="true"
      aria-labelledby="rss-keep-title"
      onKeyDown={onKeyDown}
    >
      <h2 id="rss-keep-title" className={s.keepTitle}>
        Keep this post
      </h2>
      <Input
        ref={noteBox}
        label="Note"
        multiline
        placeholder="Why keep it?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className={s.keepTags}>
        <span className={s.keepLabel}>Tags</span>
        <TagPicker
          ref={picker}
          text={[post.title, post.snippet, takeaway?.text ?? ""].filter(Boolean).join("\n\n")}
          context={String(post.id)}
          value={tags}
          onChange={setTags}
          suggestOnOpen
        />
        {hasAgent ? null : <p className={s.muted}>Suggestions from your catalog only (no agent available).</p>}
      </div>
      {takeaway ? (
        <Input
          label="Takeaway"
          multiline
          maxLength={TAKEAWAY_MAX}
          help={`From the conversation, written to the page's AI section (up to ${TAKEAWAY_MAX} characters). Clear it to leave it out.`}
          value={takeawayText}
          onChange={(e) => setTakeawayText(e.target.value)}
        />
      ) : null}
      {error ? (
        <p className={s.error} role="alert">
          Couldn't keep this post: {error}
        </p>
      ) : null}
      <div className={s.keepActions}>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          variant="primary"
          size="sm"
          loading={keeping}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          onClick={() => void keep()}
        >
          Keep
          <kbd className={s.kbd} aria-hidden="true">
            {IS_MAC ? "⌘↵" : "Ctrl+↵"}
          </kbd>
        </Button>
      </div>
    </div>
  );
}
