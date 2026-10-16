// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, subscribe } from "@jesinia/client";
import { Button, ChatTranscript, Input, useToast } from "@jesinia/design";
import { RUNNER_NAMES, useClient, type Runners } from "@jesinia/shell";
import { useEffect, useReducer, useState } from "react";
import { EMPTY, EVENT_KINDS, markApproving, reduce, type AgentEvent, type ChatState } from "./events";
import s from "./notes.module.css";

type Action = { type: "event"; event: AgentEvent } | { type: "approving"; id: string } | { type: "reset" };

function reducer(state: ChatState, a: Action): ChatState {
  if (a.type === "reset") return EMPTY;
  if (a.type === "approving") return markApproving(state, a.id);
  return reduce(state, a.event);
}

export type ChatProps = {
  page: { id: string; title: string };
  runners: Runners | null;
  onSaved: (path: string) => void;
};

export function Chat({ page, runners, onSaved }: ChatProps) {
  const client = useClient();
  const toast = useToast();
  const available = Object.entries(runners ?? {}).filter(([, r]) => r.available);
  const [runner, setRunner] = useState<string>("");
  const [session, setSession] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [state, dispatch] = useReducer(reducer, EMPTY);
  const chosen = runner || available[0]?.[0] || "";

  // A new page starts a new conversation.
  useEffect(() => {
    setSession(null);
    dispatch({ type: "reset" });
  }, [page.id]);

  useEffect(() => {
    if (!session) return;
    return subscribe<AgentEvent>(
      `/api/agents/sessions/${encodeURIComponent(session)}/events`,
      (e) => dispatch({ type: "event", event: e.data }),
      { events: EVENT_KINDS },
    );
  }, [session]);

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || !chosen || state.busy) return;
    let id = session;
    if (!id) {
      const r = await client.POST("/api/notes/sessions", { body: { page_id: page.id, runner: chosen } });
      if (!r.response.ok) {
        toast.show({
          tone: "danger",
          message: errorText((r as { error?: unknown }).error, "Couldn't start the chat."),
        });
        return;
      }
      id = (r.data as { id: string }).id;
      setSession(id);
    }
    setDraft("");
    const m = await client.POST("/api/agents/sessions/{id}/messages", { params: { path: { id } }, body: { text: t } });
    if (!m.response.ok)
      toast.show({ tone: "danger", message: errorText((m as { error?: unknown }).error, "Couldn't send.") });
  };

  const decide = async (pid: string, approve: boolean) => {
    if (!session) return;
    if (approve) dispatch({ type: "approving", id: pid });
    const path = approve
      ? "/api/agents/sessions/{id}/pending/{pid}/approve"
      : "/api/agents/sessions/{id}/pending/{pid}/reject";
    await client.POST(path, { params: { path: { id: session, pid } } });
  };

  const saveNote = async () => {
    if (!session || !state.lastReply) return;
    setSaving(true);
    const r = await client.POST("/api/notes/sessions/{id}/save", {
      params: { path: { id: session } },
      body: {
        title: `Note on ${page.title}`,
        user_text: state.lastUserText,
        agent_text: state.lastReply.text,
        model: state.lastReply.model,
      },
    });
    setSaving(false);
    if (!r.response.ok) {
      toast.show({ tone: "danger", message: errorText((r as { error?: unknown }).error, "Couldn't save the note.") });
      return;
    }
    const saved = r.data as { path: string };
    toast.show({ tone: "success", message: `Saved to ${saved.path}.` });
    onSaved(saved.path);
  };

  const runnerInfo = {
    id: chosen === "codex" ? ("codex" as const) : ("claude" as const),
    name: RUNNER_NAMES[chosen] ?? chosen,
  };
  return (
    <aside className={s.chat} aria-label="Chat">
      <div className={s.chatHead}>
        <label className={s.runnerPick}>
          <span className={s.muted}>Runner</span>
          <select value={chosen} onChange={(e) => setRunner(e.target.value)} disabled={session !== null}>
            {available.map(([id]) => (
              <option key={id} value={id}>
                {RUNNER_NAMES[id] ?? id}
              </option>
            ))}
          </select>
        </label>
        <Button
          size="sm"
          variant="primary"
          disabled={!state.lastReply || state.busy}
          loading={saving}
          onClick={() => void saveNote()}
        >
          Save note
        </Button>
      </div>
      {available.length === 0 && runners !== null ? (
        <p className={s.banner} role="status">
          No agent is available. Install and log in to Claude Code or Codex, then reload.
        </p>
      ) : null}
      <div className={s.transcript}>
        <ChatTranscript
          runner={runnerInfo}
          messages={state.messages}
          empty={`Ask ${runnerInfo.name || "the agent"} about ${page.title}.`}
          onApprove={(id) => void decide(id, true)}
          onReject={(id) => void decide(id, false)}
          onRetry={() => void send(state.lastUserText)}
        />
      </div>
      <div className={s.composer}>
        <Input
          label="Message"
          hideLabel
          multiline
          placeholder={state.busy ? "Waiting for the reply…" : "Ask about this page"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onSubmit={(v) => void send(v)}
        />
        <Button size="sm" onClick={() => void send(draft)} disabled={!draft.trim() || state.busy || !chosen}>
          Send
        </Button>
      </div>
    </aside>
  );
}
