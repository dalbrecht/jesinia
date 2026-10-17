// SPDX-License-Identifier: AGPL-3.0-or-later
// Keep (n), spec §3.1: a small popover over the reader with a note, tags from the catalog
// (TagPicker: suggested in the background, near-duplicates caught) and, after Engage, the
// agent's takeaway to edit. ⌘↵ keeps, Esc cancels. Keeping is always the author's action.
import { Button, Input } from "@jesinia/design";
import { TagPicker, useClient } from "@jesinia/shell";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { keepPost, type Kept, type KeepRequest, type Post } from "./api";
import { sentence } from "./Reader";
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

function causeOf(e: unknown): string {
  return sentence(e instanceof Error ? e.message : String(e));
}

export function KeepPopover({ post, initialNote = "", takeaway = null, hasAgent, onKept, onCancel }: KeepPopoverProps) {
  const client = useClient();
  const [note, setNote] = useState(initialNote);
  const [tags, setTags] = useState<string[]>([]);
  const [takeawayText, setTakeawayText] = useState(takeaway?.text ?? "");
  const [keeping, setKeeping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteBox = useRef<HTMLInputElement | HTMLTextAreaElement>(null);

  useEffect(() => {
    // Back to where the reader was when the popover closes.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    noteBox.current?.focus();
    return () => opener?.focus();
  }, []);

  const keep = async () => {
    if (keeping) return;
    setKeeping(true);
    setError(null);
    const text = takeawayText.trim();
    const request: KeepRequest = {
      note: note.trim(),
      tags,
      ...(takeaway && text ? { takeaway: { text, session: takeaway.session } } : {}),
    };
    try {
      onKept(await keepPost(client, post.id, request));
    } catch (e) {
      setError(causeOf(e));
      setKeeping(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onCancel();
    } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void keep();
    }
  };

  return (
    <div className={s.keep} role="dialog" aria-modal="false" aria-labelledby="rss-keep-title" onKeyDown={onKeyDown}>
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
          help="From the conversation, written to the page's AI section. Clear it to leave it out."
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
        <Button variant="primary" size="sm" loading={keeping} onClick={() => void keep()}>
          Keep <kbd className={s.kbd}>⌘↵</kbd>
        </Button>
      </div>
    </div>
  );
}
