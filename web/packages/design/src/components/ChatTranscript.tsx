// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "./Button";
import s from "./ChatTranscript.module.css";
import { Spinner, VisuallyHidden } from "./Spinner";
import { cx, prefersReducedMotion } from "./util";

export type Runner = { id: "claude" | "codex"; name: string };
export type PendingState = "awaiting" | "approving" | "approved" | "rejected" | "failed" | "superseded";

export type ChatMessage =
  | { id: string; kind: "user"; text: string; at: string }
  | { id: string; kind: "agent"; markdown: string; streaming: boolean; at: string }
  | { id: string; kind: "tool"; name: string; summary: string; detail?: string; running: boolean }
  | { id: string; kind: "error"; message: string; retryable: boolean }
  | {
      id: string;
      kind: "pending-write";
      path: string;
      summary: string;
      diff: string;
      state: PendingState;
      error?: string;
    };

export type ChatTranscriptProps = {
  runner: Runner;
  messages: ChatMessage[];
  onRetry?: (id: string) => void;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  onSaveToKb?: (id: string) => void;
  renderMarkdown?: (md: string) => ReactNode;
  empty?: ReactNode;
};

const BOTTOM_ZONE = 48;
const ANNOUNCE_LIMIT = 300;

export function RunnerGlyph({ runner }: { runner: Runner }) {
  return (
    <span className={cx(s.glyph, s[runner.id])} aria-hidden="true">
      {runner.id === "claude" ? "C" : "X"}
    </span>
  );
}

export function relativeTime(at: string, now: Date = new Date()): string {
  const then = new Date(at);
  const secs = Math.round((now.getTime() - then.getTime()) / 1000);
  if (Number.isNaN(secs)) return "";
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  return then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

type ToolMsg = Extract<ChatMessage, { kind: "tool" }>;
type Item = { kind: "single"; message: ChatMessage } | { kind: "tools"; id: string; tools: ToolMsg[] };

/** Consecutive tool calls collapse into one group row. */
function group(messages: ChatMessage[]): Item[] {
  const out: Item[] = [];
  for (const m of messages) {
    const last = out[out.length - 1];
    if (m.kind === "tool" && last?.kind === "tools") last.tools.push(m);
    else if (m.kind === "tool") out.push({ kind: "tools", id: m.id, tools: [m] });
    else out.push({ kind: "single", message: m });
  }
  return out;
}

function ToolRow({ tool }: { tool: ToolMsg }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={s.tool}>
      <button type="button" className={s.disclosure} aria-expanded={open} onClick={() => setOpen(!open)}>
        {tool.running ? <Spinner /> : <span aria-hidden="true">{open ? "▾" : "▸"}</span>}
        {tool.running ? `${tool.name}…` : `${tool.name} ${tool.summary}`}
      </button>
      {open ? <div className={s.detail}>{tool.detail ?? tool.summary}</div> : null}
    </div>
  );
}

function ToolGroup({ tools }: { tools: ToolMsg[] }) {
  const [open, setOpen] = useState(false);
  const only = tools[0];
  if (tools.length === 1 && only) return <ToolRow tool={only} />;
  return (
    <div className={s.tool}>
      <button type="button" className={s.disclosure} aria-expanded={open} onClick={() => setOpen(!open)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        {tools.length} tool calls
      </button>
      {open ? tools.map((t) => <ToolRow key={t.id} tool={t} />) : null}
    </div>
  );
}

function PendingCard({
  runner,
  m,
  onApprove,
  onReject,
}: {
  runner: Runner;
  m: Extract<ChatMessage, { kind: "pending-write" }>;
  onApprove?: ((id: string) => void) | undefined;
  onReject?: ((id: string) => void) | undefined;
}) {
  const [showDiff, setShowDiff] = useState(false);
  if (m.state === "approved") {
    return (
      <div className={s.collapsed}>
        <span className={s.approved} aria-hidden="true">
          ✓{" "}
        </span>
        Approved · {m.path}
      </div>
    );
  }
  if (m.state === "rejected") return <div className={s.collapsed}>Rejected · {m.path}</div>;
  if (m.state === "superseded") return <div className={s.collapsed}>Replaced by a newer change</div>;
  return (
    <div className={s.card} data-pending={m.id}>
      <div className={s.header}>
        <RunnerGlyph runner={runner} />
        <span>
          {runner.name} wants to edit <strong>{m.path}</strong>
        </span>
      </div>
      <div>{m.summary}</div>
      {m.state === "failed" && m.error ? <div className={s.inlineError}>{m.error}</div> : null}
      <button type="button" className={s.disclosure} aria-expanded={showDiff} onClick={() => setShowDiff(!showDiff)}>
        <span aria-hidden="true">{showDiff ? "▾" : "▸"}</span> Show changes
      </button>
      {showDiff ? <pre className={s.diff}>{m.diff}</pre> : null}
      <div className={s.footer}>
        <Button variant="primary" size="sm" loading={m.state === "approving"} onClick={() => onApprove?.(m.id)}>
          Approve change
        </Button>
        <Button size="sm" disabled={m.state === "approving"} onClick={() => onReject?.(m.id)}>
          Reject
        </Button>
      </div>
    </div>
  );
}

function plain(md: string): string {
  return md.length > ANNOUNCE_LIMIT ? `${md.slice(0, ANNOUNCE_LIMIT)}… Message continues in the transcript` : md;
}

export function ChatTranscript(props: ChatTranscriptProps) {
  const { runner, messages } = props;
  const log = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const seen = useRef(new Map<string, string>());
  const primed = useRef(false); // history present at mount is not announced
  const [unseen, setUnseen] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const [alert, setAlert] = useState("");

  const measure = () => {
    const el = log.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_ZONE;
    if (atBottom.current) setUnseen(0);
  };

  const toBottom = (smooth: boolean) => {
    const el = log.current;
    if (!el) return;
    if (smooth && !prefersReducedMotion() && el.scrollTo) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else el.scrollTop = el.scrollHeight;
    atBottom.current = true;
    setUnseen(0);
  };

  // Work out what changed since the last render: new content, and what to announce.
  useLayoutEffect(() => {
    let added = 0;
    let say: string | null = null;
    let sentByUser = false;
    for (const m of messages) {
      const sig =
        m.kind === "agent"
          ? `${m.streaming}:${m.markdown.length}`
          : m.kind === "pending-write"
            ? m.state
            : m.kind === "tool"
              ? String(m.running)
              : "1";
      const before = seen.current.get(m.id);
      if (before === sig) continue;
      seen.current.set(m.id, sig);
      added += before === undefined ? 1 : 0;
      if (before === undefined && m.kind === "user") sentByUser = true;
      if (m.kind === "agent") {
        if (before === undefined && m.streaming) say = `${runner.name} is responding`;
        else if (!m.streaming && before?.startsWith("true")) say = plain(m.markdown);
        else if (!m.streaming && before === undefined) say = plain(m.markdown);
      } else if (m.kind === "pending-write" && m.state === "awaiting" && before === undefined) {
        say = `${runner.name} wants to edit ${m.path}. Approve or reject.`;
      } else if (m.kind === "error" && before === undefined && primed.current) {
        setAlert(m.message);
      }
    }
    if (!primed.current) {
      primed.current = true;
      toBottom(false);
      return;
    }
    if (say !== null) setAnnouncement(say);
    if (sentByUser || atBottom.current) toBottom(false);
    else if (added > 0) setUnseen((n) => n + added);
  }, [messages, runner.name]);

  useEffect(() => {
    const el = log.current;
    if (!el) return;
    el.addEventListener("scroll", measure, { passive: true });
    return () => el.removeEventListener("scroll", measure);
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      toBottom(false);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (log.current) log.current.scrollTop = 0;
      atBottom.current = false;
    }
  };

  const render = (m: ChatMessage): ReactNode => {
    switch (m.kind) {
      case "user":
        return <div className={s.user}>{m.text}</div>;
      case "agent":
        return (
          <article className={s.agent} aria-busy={m.streaming || undefined}>
            <header className={s.header}>
              <RunnerGlyph runner={runner} />
              <span className={s.runnerName}>{runner.name}</span>
              <time dateTime={m.at}>{relativeTime(m.at)}</time>
            </header>
            <div className={props.renderMarkdown ? undefined : s.text}>
              {props.renderMarkdown ? props.renderMarkdown(m.markdown) : m.markdown}
              {m.streaming ? <span className={s.cursor} aria-hidden="true" /> : null}
            </div>
            {!m.streaming ? (
              <div className={s.footer}>
                <Button variant="ghost" size="sm" onClick={() => void navigator.clipboard?.writeText(m.markdown)}>
                  Copy
                </Button>
                {props.onSaveToKb ? (
                  <Button variant="ghost" size="sm" onClick={() => props.onSaveToKb?.(m.id)}>
                    Save to KB
                  </Button>
                ) : null}
              </div>
            ) : null}
          </article>
        );
      case "error":
        return (
          <div className={s.error}>
            <span aria-hidden="true">×</span>
            <div>
              <div>{m.message}</div>
              {m.retryable && props.onRetry ? (
                <div className={s.footer}>
                  <Button size="sm" onClick={() => props.onRetry?.(m.id)}>
                    Retry
                  </Button>
                </div>
              ) : null}
            </div>
          </div>
        );
      case "pending-write":
        return <PendingCard runner={runner} m={m} onApprove={props.onApprove} onReject={props.onReject} />;
      default:
        return null; // tool messages render through their group
    }
  };

  return (
    <div className={s.wrap}>
      <div
        ref={log}
        role="log"
        aria-live="off"
        aria-label={`Conversation with ${runner.name}`}
        tabIndex={0}
        className={s.log}
        onKeyDown={onKeyDown}
      >
        {messages.length === 0 ? (
          <p className={s.empty}>{props.empty ?? `Ask ${runner.name} about this page.`}</p>
        ) : (
          group(messages).map((item) =>
            item.kind === "tools" ? (
              <ToolGroup key={item.id} tools={item.tools} />
            ) : (
              <div key={item.message.id} data-message={item.message.id}>
                {render(item.message)}
              </div>
            ),
          )
        )}
      </div>
      {unseen > 0 ? (
        <div className={s.jump}>
          <Button size="sm" onClick={() => toBottom(true)}>
            {`${unseen} new`}
          </Button>
        </div>
      ) : null}
      <div aria-live="polite" aria-atomic="true" data-announcer="">
        <VisuallyHidden>{announcement}</VisuallyHidden>
      </div>
      <div role="alert">
        <VisuallyHidden>{alert}</VisuallyHidden>
      </div>
    </div>
  );
}
