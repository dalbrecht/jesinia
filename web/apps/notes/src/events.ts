// SPDX-License-Identifier: AGPL-3.0-or-later
// Agent session events (GET /api/agents/sessions/{id}/events) → ChatTranscript messages.
import type { ChatMessage } from "@jesinia/design";

export type AgentEvent = { seq: number; kind: string; data: Record<string, unknown>; origin: string };

export const EVENT_KINDS = [
  "user_message",
  "text",
  "tool_use",
  "tool_result",
  "turn_done",
  "error",
  "pending_write",
  "pending_resolved",
  "attached",
];

export type ChatState = {
  messages: ChatMessage[];
  /** A turn is in flight (from the user's message until turn_done or error). */
  busy: boolean;
  /** The last completed agent reply, for Save note. */
  lastReply: { text: string; model: string } | null;
  lastUserText: string;
  attached: boolean;
};

export const EMPTY: ChatState = { messages: [], busy: false, lastReply: null, lastUserText: "", attached: false };

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);

function summarize(input: unknown): string {
  if (!input || typeof input !== "object") return "";
  const o = input as Record<string, unknown>;
  const first = o.q ?? o.query ?? o.title ?? o.id ?? Object.values(o)[0];
  return typeof first === "string" ? `"${first.length > 60 ? `${first.slice(0, 60)}…` : first}"` : "";
}

function pendingPath(tool: string, args: Record<string, unknown>): string {
  if (tool === "kb_write" || tool === "notes_save") {
    const folder = str(args.folder, tool === "notes_save" ? "Notes" : "");
    return `${folder ? `${folder}/` : ""}${str(args.title, "Untitled")}.md`;
  }
  return str(args.id ?? args.path, tool);
}

export function reduce(state: ChatState, e: AgentEvent, now = new Date().toISOString()): ChatState {
  const messages = [...state.messages];
  const last = messages[messages.length - 1];
  switch (e.kind) {
    case "user_message": {
      const text = str(e.data.text);
      messages.push({ id: `e${e.seq}`, kind: "user", text, at: now });
      return { ...state, messages, busy: true, lastUserText: text };
    }
    case "text": {
      const chunk = str(e.data.text);
      if (last?.kind === "agent" && last.streaming) {
        messages[messages.length - 1] = { ...last, markdown: last.markdown + chunk };
      } else {
        messages.push({ id: `e${e.seq}`, kind: "agent", markdown: chunk, streaming: true, at: now });
      }
      return { ...state, messages, busy: true };
    }
    case "tool_use":
      messages.push({
        id: `e${e.seq}`,
        kind: "tool",
        name: str(e.data.name),
        summary: summarize(e.data.input),
        running: true,
      });
      return { ...state, messages };
    case "tool_result": {
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i];
        if (m?.kind === "tool" && m.running && m.name === e.data.name) {
          messages[i] = {
            ...m,
            running: false,
            summary: e.data.ok === false ? `${m.summary} · failed`.trim() : m.summary,
          };
          break;
        }
      }
      return { ...state, messages };
    }
    case "turn_done": {
      let reply: ChatState["lastReply"] = state.lastReply;
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i];
        if (m?.kind === "agent") {
          messages[i] = { ...m, streaming: false };
          reply = { text: m.markdown, model: str(e.data.model, "unknown") };
          break;
        }
        if (m?.kind === "user") break;
      }
      return { ...state, messages, busy: false, lastReply: reply };
    }
    case "error": {
      const done = messages.map((m) => (m.kind === "agent" && m.streaming ? { ...m, streaming: false } : m));
      done.push({
        id: `e${e.seq}`,
        kind: "error",
        message: str(e.data.message, "The agent stopped."),
        retryable: true,
      });
      return { ...state, messages: done, busy: false };
    }
    case "pending_write": {
      const tool = str(e.data.tool);
      const args = (e.data.args ?? {}) as Record<string, unknown>;
      const body = str(args.body ?? args.text);
      messages.push({
        id: str(e.data.pending_id),
        kind: "pending-write",
        path: pendingPath(tool, args),
        summary: `${tool}: ${body.split("\n").length} ${body.split("\n").length === 1 ? "line" : "lines"}`,
        diff: body
          .split("\n")
          .map((l) => `+ ${l}`)
          .join("\n"),
        state: "awaiting",
      });
      return { ...state, messages };
    }
    case "pending_resolved": {
      const id = str(e.data.pending_id);
      const st = str(e.data.state);
      const result = (e.data.result ?? {}) as Record<string, unknown>;
      return {
        ...state,
        messages: messages.map((m) =>
          m.kind === "pending-write" && m.id === id
            ? st === "failed"
              ? { ...m, state: "failed", error: str(result.error, "The write failed.") }
              : { ...m, state: st === "approved" ? "approved" : "rejected" }
            : m,
        ),
      };
    }
    case "attached":
      return { ...state, attached: true };
    default:
      return state;
  }
}

/** Mark a pending write as in progress (approve clicked) before the server answers. */
export function markApproving(state: ChatState, id: string): ChatState {
  return {
    ...state,
    messages: state.messages.map((m) => (m.kind === "pending-write" && m.id === id ? { ...m, state: "approving" } : m)),
  };
}
