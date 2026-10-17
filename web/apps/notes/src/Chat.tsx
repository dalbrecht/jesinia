// SPDX-License-Identifier: AGPL-3.0-or-later
// The Notes chat: the shared chat rail about the open page, with Save note (and its tags) as
// the rail's footer.
import { errorText } from "@jesinia/client";
import { Button, useToast } from "@jesinia/design";
import { ChatRail, TagPicker, useClient, type ChatState, type Runners } from "@jesinia/shell";
import { useEffect, useRef, useState } from "react";
import s from "./notes.module.css";

export type ChatProps = {
  page: { id: string; title: string };
  runners: Runners | null;
  onSaved: (path: string) => void;
};

export function Chat({ page, runners, onSaved }: ChatProps) {
  const client = useClient();
  const toast = useToast();
  const [session, setSession] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [tags, setTags] = useState<string[]>([]);
  const currentPage = useRef<string | null>(page.id);

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

  const footer = (chat: ChatState) => (
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

  return (
    <ChatRail
      className={s.chat}
      session={session}
      onSessionNeeded={startSession}
      footer={footer}
      runners={runners}
      subject={page.title}
    />
  );
}
