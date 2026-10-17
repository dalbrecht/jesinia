// SPDX-License-Identifier: AGPL-3.0-or-later
// Agent session events (GET /api/agents/sessions/{id}/events) → ChatTranscript messages, for
// the shared chat rail (Notes, RSS Engage).
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
  /** The last completed agent reply, for the rail's footer (Save note, Keep with this conversation). */
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

type Target = {
  action?: string;
  path?: string;
  to?: string;
  title?: string;
  new_title?: string;
  current?: string;
  error?: string;
  note?: string;
  jesinia?: { from: unknown; to: unknown };
};

const MAX_DIFF_CELLS = 4_000_000; // beyond this, show the whole new body instead

/** A unified line diff (LCS): unchanged lines keep a two-space prefix. */
export function lineDiff(before: string[], after: string[]): string {
  const n = before.length;
  const m = after.length;
  if (n * m > MAX_DIFF_CELLS)
    return ["(too large to compare: the full new text follows)", ...after.map((l) => `+ ${l}`)].join("\n");
  // lcs(i, j): longest common subsequence of before[i..] and after[j..].
  const table = new Int32Array((n + 1) * (m + 1));
  const lcs = (i: number, j: number) => table[i * (m + 1) + j] ?? 0;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * (m + 1) + j] = before[i] === after[j] ? lcs(i + 1, j + 1) + 1 : Math.max(lcs(i + 1, j), lcs(i, j + 1));
    }
  }
  const out: string[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && before[i] === after[j]) {
      out.push(`  ${before[i]}`);
      i++;
      j++;
    } else if (i < n && (j === m || lcs(i + 1, j) >= lcs(i, j + 1))) {
      out.push(`- ${before[i]}`); // deletions first, as in a unified diff
      i++;
    } else {
      out.push(`+ ${after[j]}`);
      j++;
    }
  }
  return out.join("\n");
}

/** The card shows what the server says the write will really do (tool preview). */
function describe(
  tool: string,
  args: Record<string, unknown>,
  target: Target,
): { path: string; summary: string; diff: string } {
  const body = str(args.body ?? args.text);
  const lines = body ? body.split("\n") : [];
  const count = `${lines.length} ${lines.length === 1 ? "line" : "lines"}`;
  const plus = lines.map((l) => `+ ${l}`).join("\n");
  if (target.action === "move") {
    return {
      path: str(target.path),
      summary: `Moves the page to ${str(target.to)}`,
      diff: `- ${str(target.path)}\n+ ${str(target.to)}`,
    };
  }
  if (target.action === "update" || target.action === "update+move") {
    const parts = [lineDiff(str(target.current).split("\n"), lines)];
    if (target.jesinia)
      parts.unshift(`~ metadata: ${JSON.stringify(target.jesinia.from)} → ${JSON.stringify(target.jesinia.to)}`);
    const moving = target.action === "update+move";
    if (moving) parts.unshift(`~ moves to ${str(target.to)} (title "${str(target.new_title)}")`);
    return {
      path: str(target.path),
      summary: moving
        ? `Changes an existing page and moves it to ${str(target.to)}`
        : `Changes an existing page (${count})`,
      diff: parts.join("\n"),
    };
  }
  if (target.action === "create") {
    const note = target.note ? ` ${str(target.note)}` : "";
    return { path: str(target.path), summary: `Creates a new page (${count}).${note}`, diff: plus };
  }
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
      // A new turn: Save note must never pair this question with an earlier answer.
      return { ...state, messages, busy: true, lastUserText: text, lastReply: null };
    }
    case "text": {
      const chunk = str(e.data.text);
      if (e.origin === "terminal") {
        // agents_session_note: a whole progress note with no turn_done to follow.
        messages.push({ id: `e${e.seq}`, kind: "agent", markdown: chunk, streaming: false, at: now });
        return { ...state, messages };
      }
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
