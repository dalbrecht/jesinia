// SPDX-License-Identifier: AGPL-3.0-or-later
// The Notes chat: the shared chat rail about the open page, with Save note (and its tags) as
// the rail's footer. Beside the page on a wide window; under 1024 px a sheet over it, opened on
// request (the RSS pattern).
import { errorText } from "@jesinia/client";
import { Button, useToast } from "@jesinia/design";
import { ChatRail, TagPicker, useClient, type ChatState, type Runners } from "@jesinia/shell";
import { useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent, type Ref } from "react";
import s from "./notes.module.css";

/** What the workspace may ask of the chat, whose conversation state lives in the rail. */
export type ChatHandle = {
  /** A draft, a running reply, or a conversation with messages: the author is using the chat. */
  isInUse: () => boolean;
};

export type ChatProps = {
  page: { id: string; title: string };
  runners: Runners | null;
  onSaved: (path: string) => void;
  /** A sheet over the page (under 1024 px) rather than a column beside it. */
  sheet: boolean;
  /** Whether the sheet shows. Closed, it is hidden, not unmounted: a draft or a running reply lives in the rail. */
  open: boolean;
  onClose: () => void;
  handle?: Ref<ChatHandle>;
};

export function Chat({ page, runners, onSaved, sheet, open, onClose, handle }: ChatProps) {
  const client = useClient();
  const toast = useToast();
  const [session, setSession] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [tags, setTags] = useState<string[]>([]);
  const currentPage = useRef<string | null>(page.id);
  const slot = useRef<HTMLDivElement>(null);
  // The rail's latest state, as handed to its footer on every render.
  const latest = useRef<ChatState | null>(null);
  useImperativeHandle(
    handle,
    () => ({
      isInUse: () => {
        const draft = slot.current?.querySelector("textarea")?.value ?? "";
        const state = latest.current;
        return draft.trim() !== "" || (state !== null && (state.busy || state.messages.length > 0));
      },
    }),
    [],
  );

  // The sheet covers the page: opening it takes focus to its message box.
  useEffect(() => {
    if (sheet && open) slot.current?.querySelector("textarea")?.focus();
  }, [sheet, open]);

  // A new page starts a new conversation.
  useEffect(() => {
    currentPage.current = page.id;
    setSession(null);
    setTags([]);
    return () => {
      currentPage.current = null; // switching pages may unmount the chat while it loads
    };
  }, [page.id]);

  const startSession = async (runner: string): Promise<string | null> => {
    const forPage = page.id;
    const r = await client.POST("/api/notes/sessions", { body: { page_id: forPage, runner } });
    // The user switched pages while this was in flight: that session belongs to the old page.
    if (currentPage.current !== forPage) return null;
    if (!r.response.ok) {
      toast.show({
        tone: "danger",
        message: errorText((r as { error?: unknown }).error, "Couldn't start the chat."),
      });
      return null;
    }
    const id = (r.data as { id: string }).id;
    setSession(id);
    return id;
  };

  const saveNote = async (chat: ChatState) => {
    if (!session || !chat.lastReply) return;
    setSaving(true);
    const r = await client.POST("/api/notes/sessions/{id}/save", {
      params: { path: { id: session } },
      body: {
        title: `Note on ${page.title}`,
        user_text: chat.lastUserText,
        agent_text: chat.lastReply.text,
        model: chat.lastReply.model,
        tags,
      },
    });
    setSaving(false);
    if (!r.response.ok) {
      toast.show({ tone: "danger", message: errorText((r as { error?: unknown }).error, "Couldn't save the note.") });
      return;
    }
    const saved = r.data as { path: string };
    setTags([]);
    toast.show({ tone: "success", message: `Saved to ${saved.path}.` });
    onSaved(saved.path);
  };

  const footer = (chat: ChatState) => {
    latest.current = chat;
    return (
      <>
        {chat.lastReply ? (
          <TagPicker
            text={`${chat.lastUserText}\n\n${chat.lastReply.text}`}
            context={page.id}
            value={tags}
            onChange={setTags}
          />
        ) : null}
        <span className={s.saveRow}>
          <Button
            size="sm"
            variant="primary"
            disabled={!chat.lastReply || chat.busy}
            loading={saving}
            onClick={() => void saveNote(chat)}
          >
            Save note
          </Button>
        </span>
      </>
    );
  };

  // One tree for both layouts, so crossing the breakpoint keeps the transcript and the draft.
  return (
    <div
      ref={slot}
      id="notes-chat"
      className={sheet ? s.sheet : s.railSlot}
      hidden={sheet && !open}
      {...(sheet
        ? {
            // Not modal (like the RSS sheet): the page stays reachable around it.
            role: "dialog",
            "aria-label": "Chat about this page",
            onKeyDown: (e: KeyboardEvent) => {
              if (e.key === "Escape") onClose();
            },
          }
        : {})}
    >
      <ChatRail
        className={s.chat}
        session={session}
        onSessionNeeded={startSession}
        footer={footer}
        runners={runners}
        subject={page.title}
        {...(sheet
          ? {
              actions: (
                <Button
                  variant="ghost"
                  size="sm"
                  icon="✕"
                  aria-label="Close chat"
                  title="Close chat"
                  onClick={onClose}
                />
              ),
            }
          : {})}
      />
    </div>
  );
}
