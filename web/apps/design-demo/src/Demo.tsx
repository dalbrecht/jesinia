// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  Badge,
  Banner,
  BannerRegion,
  Button,
  ChatTranscript,
  CommandPalette,
  Input,
  List,
  Panel,
  Row,
  StatusIndicator,
  Tabs,
  ToastProvider,
  useToast,
  type ChatMessage,
} from "@jesinia/design";
import { useState } from "react";

const SEMANTIC = [
  "bg", "surface", "surface-raised", "surface-hover", "surface-sunken", "fg", "fg-muted", "fg-subtle",
  "border", "border-strong", "accent", "accent-hover", "accent-text", "accent-subtle", "focus", "highlight",
  "ai", "success", "success-bg", "warn", "warn-bg", "danger", "danger-bg",
  "app-notes", "app-notes-chrome", "app-rss", "app-rss-chrome", "app-review", "app-review-chrome",
  "app-issues", "app-issues-chrome", "runner-claude", "runner-codex",
]; // prettier-ignore

const PRIMITIVES = ["paper", "sage", "marigold", "plum", "cornflower", "clay", "fern"];
const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];

const now = new Date().toISOString();
const TRANSCRIPT: ChatMessage[] = [
  { id: "u1", kind: "user", text: "Summarize the maintenance budget notes.", at: now },
  { id: "t1", kind: "tool", name: "kb.search", summary: '"maintenance budget" · 3 results', detail: "q: maintenance budget", running: false },
  { id: "t2", kind: "tool", name: "kb.read", summary: "Notes/maintenance.md", running: false },
  { id: "a1", kind: "agent", markdown: "The budget splits into three funding models. Contributions cover most of it.", streaming: false, at: now },
  { id: "w1", kind: "pending-write", path: "Notes/maintenance.md", summary: "Adds a 'Funding models' section, 12 lines", diff: "+## Funding models\n+- Grants\n+- Sponsorship", state: "awaiting" },
  { id: "e1", kind: "error", message: "Claude stopped: the CLI is logged out. Run `claude login`, then Retry.", retryable: true },
  { id: "a2", kind: "agent", markdown: "Drafting the section", streaming: true, at: now },
]; // prettier-ignore

function Swatch({ name }: { name: string }) {
  return (
    <div className="swatch">
      <div className="chip" style={{ background: `var(--jx-${name})` }} />
      --jx-{name}
    </div>
  );
}

function ToastButtons() {
  const toast = useToast();
  return (
    <div className="row">
      <Button onClick={() => toast.show({ tone: "success", message: "Note saved." })}>Success toast</Button>
      <Button onClick={() => toast.show({ tone: "warn", message: "Sync is slow today." })}>Warning toast</Button>
      <Button
        onClick={() =>
          toast.show({ message: "Page moved to Archive.", action: { label: "Undo", onAction: () => undefined } })
        }
      >
        Toast with action
      </Button>
    </div>
  );
}

export function Demo() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [tab, setTab] = useState("all");
  const [selected, setSelected] = useState<string[]>(["2"]);
  const [messages, setMessages] = useState(TRANSCRIPT);
  const decide = (id: string, state: "approved" | "rejected") =>
    setMessages((ms) => ms.map((m) => (m.id === id && m.kind === "pending-write" ? { ...m, state } : m)));

  const flip = () => {
    const next = theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    setTheme(next);
  };

  return (
    <ToastProvider>
      <CommandPalette
        commands={[
          { id: "theme", label: "Toggle theme", group: "command", onRun: flip },
          { id: "notes", label: "Open Notes", detail: "/notes/", group: "command", onRun: () => undefined },
        ]}
      />
      <BannerRegion>
        <Banner tone="warn" cause="Sync paused:" action={{ label: "Resume", onAction: () => undefined }}>
          the KB has a merge conflict. Resolve it, then resume.
        </Banner>
      </BannerRegion>
      <main className="page">
        <div className="top">
          <h1>Jesinia design system</h1>
          <div className="row">
            <StatusIndicator
              items={[
                {
                  id: "dup",
                  severity: "minor",
                  title: "Duplicate page ids",
                  detail: "2 files share an id.",
                  href: "#",
                },
              ]}
            />
            <Button onClick={flip}>{theme === "light" ? "Dark theme" : "Light theme"}</Button>
          </div>
        </div>

        <Panel title="Semantic roles">
          <div className="swatches">
            {SEMANTIC.map((n) => (
              <Swatch key={n} name={n} />
            ))}
          </div>
        </Panel>

        <Panel title="Primitives">
          {PRIMITIVES.map((p) => (
            <div key={p} className="swatches" style={{ marginBottom: "var(--jx-space-3)" }}>
              {STEPS.map((s) => (
                <Swatch key={s} name={`${p}-${s}`} />
              ))}
            </div>
          ))}
        </Panel>

        <Panel title="Type">
          <p>Bricolage Grotesque: the interface face, for labels, lists and controls.</p>
          <p className="type-read">
            Newsreader is the reading face. Notes and articles set in it at a comfortable measure, with <em>italics</em>{" "}
            for emphasis.
          </p>
          <p className="type-mono">JetBrains Mono: kb.search "maintenance budget"</p>
        </Panel>

        <Panel
          title="Buttons"
          actions={
            <Badge tone="success" icon="✓">
              All states
            </Badge>
          }
        >
          <div className="row">
            <Button variant="primary">Save note</Button>
            <Button>Cancel</Button>
            <Button variant="ghost">More</Button>
            <Button variant="danger">Rotate token</Button>
            <Button variant="primary" loading>
              Saving
            </Button>
            <Button disabled>Disabled</Button>
            <Button size="sm">Small</Button>
            <Button aria-label="Settings" icon="⚙" variant="ghost" />
            <Button href="#">Link button</Button>
          </div>
        </Panel>

        <Panel title="Inputs">
          <div style={{ display: "grid", gap: "var(--jx-space-4)", maxWidth: 480 }}>
            <Input label="Title" placeholder="Untitled" help="Shown in the file name." />
            <Input label="Search" variant="search" placeholder="Search notes" />
            <Input label="KB folder" variant="path" defaultValue="/Users/me/kb" error="Folder not found." />
            <Input label="Message" multiline placeholder="Ask Claude…" />
          </div>
        </Panel>

        <Panel title="Badges">
          <div className="row">
            <Badge>draft</Badge>
            <Badge tone="marigold">reading</Badge>
            <Badge tone="plum">research</Badge>
            <Badge tone="cornflower">infra</Badge>
            <Badge tone="clay">home</Badge>
            <Badge tone="fern">garden</Badge>
            <Badge tone="warn" icon="!">
              Paused
            </Badge>
            <Badge tone="danger" icon="×">
              Failed
            </Badge>
          </div>
        </Panel>

        <Panel title="Tabs and list" variant="flush">
          <Tabs
            aria-label="Filter"
            selected={tab}
            onSelect={setTab}
            tabs={[
              { id: "all", label: "All", count: 3 },
              { id: "unread", label: "Unread", count: 1 },
              { id: "saved", label: "Saved", disabled: true },
            ]}
          >
            {() => (
              <List
                aria-label="Feed items"
                selection="single"
                selected={selected}
                onSelectionChange={setSelected}
                items={[
                  { id: "1", title: "Notes on garden planning", unread: true },
                  { id: "2", title: "Maintenance budget", unread: false },
                  { id: "3", title: "Reading list", unread: false },
                ]}
                getKey={(i) => i.id}
                renderRow={(i) => (
                  <Row
                    title={i.title}
                    unread={i.unread}
                    excerpt="A short excerpt from the page."
                    meta="Notes · 2 h ago"
                  />
                )}
              />
            )}
          </Tabs>
        </Panel>

        <Panel title="Toasts">
          <ToastButtons />
        </Panel>

        <Panel title="Chat transcript" variant="flush">
          <div className="chat">
            <ChatTranscript
              runner={{ id: "claude", name: "Claude" }}
              messages={messages}
              onApprove={(id) => decide(id, "approved")}
              onReject={(id) => decide(id, "rejected")}
              onRetry={() => undefined}
            />
          </div>
        </Panel>
        <p>Press ⌘K for the command palette.</p>
      </main>
    </ToastProvider>
  );
}
