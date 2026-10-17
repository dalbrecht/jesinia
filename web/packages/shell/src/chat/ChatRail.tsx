// SPDX-License-Identifier: AGPL-3.0-or-later
// The chat rail shared by Notes and RSS Engage: a runner picker, the transcript of one agent
// session (replies rendered as safe Markdown, ruling R17), pending-write approvals and a
// composer. What to do with the conversation (Save note, Keep with this conversation) is the
// app's, passed as `footer`.
import { errorText, subscribe } from "@jesinia/client";
import { Button, ChatTranscript, Input, useToast } from "@jesinia/design";
import { useEffect, useReducer, useState, type ReactNode } from "react";
import { RUNNER_NAMES } from "../apps";
import { useClient } from "../context";
import { renderMarkdown } from "../safe-markdown";
import type { Runners } from "../types";
import s from "./ChatRail.module.css";
import { EMPTY, EVENT_KINDS, markApproving, markAwaiting, reduce, type AgentEvent, type ChatState } from "./events";

type Action =
  | { type: "event"; event: AgentEvent }
  | { type: "approving"; id: string }
  | { type: "awaiting"; id: string }
  | { type: "reset" };

function reducer(state: ChatState, a: Action): ChatState {
  if (a.type === "reset") return EMPTY;
  if (a.type === "approving") return markApproving(state, a.id);
  if (a.type === "awaiting") return markAwaiting(state, a.id);
  return reduce(state, a.event);
}

export type ChatRailProps = {
  /** The agent session shown; null until one is started. A different one starts a new transcript. */
  session: string | null;
  /**
   * Start a session with `runner`, for the first message when there is none. Resolves to its
   * id, or null when it couldn't start (the app says why) or no longer applies.
   */
  onSessionNeeded: (runner: string) => Promise<string | null>;
  /** Under the transcript: the app's actions on the conversation, given its current state. */
  footer?: (chat: ChatState) => ReactNode;
  /** null while the runners are still loading. */
  runners: Runners | null;
  /** What the conversation is about, for the empty transcript ("Ask Codex about …"). */
  subject: string;
  /** The rail's accessible name. */
  label?: string;
  placeholder?: string;
  /** Controls at the head's end (Close, Done). */
  actions?: ReactNode;
  className?: string | undefined;
};

export function ChatRail(props: ChatRailProps) {
  const { session, runners } = props;
  const client = useClient();
  const toast = useToast();
  const available = Object.entries(runners ?? {}).filter(([, r]) => r.available);
  const [runner, setRunner] = useState<string>("");
  const [draft, setDraft] = useState("");
  const [state, dispatch] = useReducer(reducer, EMPTY);
  const chosen = runner || available[0]?.[0] || "";

  // One transcript per session: a new (or no) session starts a new conversation.
  useEffect(() => {
    dispatch({ type: "reset" });
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
    const id = session ?? (await props.onSessionNeeded(chosen));
    if (!id) return;
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
    const r = await client.POST(path, { params: { path: { id: session, pid } } });
    if (r.response.ok) return;
    // Refused (the write gate, a gone session): say why, and offer the decision again.
    if (approve) dispatch({ type: "awaiting", id: pid });
    const what = approve ? "approve" : "reject";
    toast.show({
      tone: "danger",
      message: `Couldn't ${what} the change: ${errorText((r as { error?: unknown }).error, `HTTP ${r.response.status}`)}`,
    });
  };

  const runnerInfo = {
    id: chosen === "codex" ? ("codex" as const) : ("claude" as const),
    name: RUNNER_NAMES[chosen] ?? chosen,
  };
  return (
    <aside className={[s.chat, props.className].filter(Boolean).join(" ")} aria-label={props.label ?? "Chat"}>
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
        {props.actions ? <span className={s.actions}>{props.actions}</span> : null}
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
          empty={`Ask ${runnerInfo.name || "the agent"} about ${props.subject}.`}
          renderMarkdown={renderMarkdown}
          onApprove={(id) => void decide(id, true)}
          onReject={(id) => void decide(id, false)}
          onRetry={() => void send(state.lastUserText)}
        />
      </div>
      {props.footer ? <div className={s.footer}>{props.footer(state)}</div> : null}
      <div className={s.composer}>
        <Input
          label="Message"
          hideLabel
          multiline
          placeholder={state.busy ? "Waiting for the reply…" : (props.placeholder ?? "Ask about this page")}
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
