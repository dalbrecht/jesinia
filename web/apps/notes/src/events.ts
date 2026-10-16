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

type Target = { action?: string; path?: string; to?: string; title?: string; current?: string; error?: string };

/** The card shows what the server says the write will really do (tool preview). */
function describe(
  tool: string,
  args: Record<string, unknown>,
  target: Target,
): { path: string; summary: string; diff: string } {
  const body = str(args.body ?? args.text);
  const lines = body ? body.split("\n") : [];
  const count = `${lines.length} ${lines.length === 1 ? "line" : "lines"}`;
  if (target.action === "move") {
    return {
      path: str(target.path),
      summary: `Moves the page to ${str(target.to)}`,
      diff: `- ${str(target.path)}\n+ ${str(target.to)}`,
    };
  }
  if (target.action === "update") {
    const before = str(target.current).split("\n");
    const removed = before.filter((l) => !lines.includes(l)).map((l) => `- ${l}`);
    const added = lines.filter((l) => !before.includes(l)).map((l) => `+ ${l}`);
    return {
      path: str(target.path),
      summary: `Changes an existing page (${count})`,
      diff: [...removed, ...added].join("\n"),
    };
  }
  const plus = lines.map((l) => `+ ${l}`).join("\n");
  if (target.action === "create")
    return { path: str(target.path), summary: `Creates a new page (${count})`, diff: plus };
  if (tool === "notes_save")
    return { path: `Notes/${str(args.title, "Untitled")}.md`, summary: `Saves a note (${count})`, diff: plus };
  return { path: tool, summary: target.error ? `${tool}: ${target.error}` : `${tool} (${count})`, diff: plus };
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
      const d = describe(tool, args, (e.data.target ?? {}) as Target);
      messages.push({ id: str(e.data.pending_id), kind: "pending-write", ...d, state: "awaiting" });
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
