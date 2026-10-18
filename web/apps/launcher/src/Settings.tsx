// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText } from "@jesinia/client";
import { Badge, Button, Input, Panel, useToast } from "@jesinia/design";
import { RUNNER_NAMES, useClient, type KbPathInfo, type ServerStatus, type SyncStatus } from "@jesinia/shell";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { clock, MODE_LABEL } from "./format";
import { IssuesSection } from "./settings/Issues";
import { RssSection } from "./settings/Rss";
import { TagsSection } from "./settings/Tags";
import { unreachable } from "./settings/unreachable";
import s from "./settings.module.css";

/** The outline, in the design pass's order (Review joins when it ships, before Issues). */
const SECTIONS = [
  ["kb", "Knowledge base"],
  ["sync", "Git sync"],
  ["agents", "Agents"],
  ["tags", "Tags"],
  ["rss", "RSS"],
  ["issues", "Issues"],
  ["conflicts", "Conflicts"],
  ["security", "Security"],
] as const;
type SectionId = (typeof SECTIONS)[number][0];

function sectionOf(hash: string): SectionId {
  const wanted = hash.replace(/^#/, "");
  return SECTIONS.find(([id]) => id === wanted)?.[0] ?? "kb";
}

/** The section the address names (`/settings/#rss`), so deep links open it; else the first. */
function useSection(): SectionId {
  const [section, setSection] = useState(() => sectionOf(window.location.hash));
  useEffect(() => {
    const onChange = () => setSection(sectionOf(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return section;
}

function KbSection({ status }: { status: ServerStatus }) {
  const client = useClient();
  const toast = useToast();
  const [info, setInfo] = useState<KbPathInfo | null>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  /** No answer while loading (a restart, say): the cause, with Try again. */
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    setLoadError(null);
    client.GET("/api/kb/path").then(
      (r) => {
        const data = r.data as KbPathInfo | undefined;
        if (data) {
          setInfo(data);
          setValue(data.path ?? "");
        }
      },
      (e: unknown) => setLoadError(`Couldn't load the KB folder: ${unreachable(e)}`),
    );
  }, [client, attempt]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const r = await client.PUT("/api/kb/path", { body: { path: value.trim() } }).catch((err: unknown) => {
      setError(`Couldn't save the KB folder: ${unreachable(err)}`);
      return null;
    });
    setSaving(false);
    if (r === null) return;
    if (r.error) {
      setError(errorText(r.error, "Couldn't use that folder."));
      return;
    }
    const data = r.data as { indexed?: number };
    toast.show({ tone: "success", message: `KB folder saved. ${data.indexed ?? 0} pages indexed.` });
    const fresh = await client.GET("/api/kb/path");
    if (fresh.data) setInfo(fresh.data as KbPathInfo);
    await status.refresh();
  };

  return (
    <Panel title="Knowledge base" id="kb">
      <form className={s.form} onSubmit={(e) => void save(e)}>
        <Input
          label="KB folder"
          variant="path"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="/Users/you/kb"
          loading={saving}
          error={error ?? (info?.problem && !error ? info.problem : undefined)}
          help="Your Markdown folder. Changing it reindexes; your files aren't moved."
        />
        <div className={s.actions}>
          <Button type="submit" variant="primary" loading={saving}>
            Save
          </Button>
        </div>
        {loadError ? (
          <div className={s.actions}>
            <p className={s.muted}>{loadError}</p>
            <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </Button>
          </div>
        ) : null}
      </form>
    </Panel>
  );
}

function SyncSection({ status }: { status: ServerStatus }) {
  const client = useClient();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const sync = status.sync;

  const act = async (name: string, path: "/api/kbsync/run" | "/api/kbsync/resume" | "/api/kbsync/init") => {
    setBusy(name);
    const r = await client.POST(path).catch((e: unknown) => {
      toast.show({ tone: "danger", message: `Couldn't sync: ${unreachable(e)}` });
      return null;
    });
    setBusy(null);
    if (r === null) return;
    if (!r.response.ok) {
      toast.show({ tone: "danger", message: errorText((r as { error?: unknown }).error) });
      return;
    }
    const body = r.data as { status?: SyncStatus; result?: { message: string } } & Partial<SyncStatus>;
    status.setSync((body.status ?? body) as SyncStatus);
    if (body.result) toast.show({ message: `Sync: ${body.result.message}` });
  };

  return (
    <Panel title="Git sync" id="sync">
      {sync ? (
        <div className={s.form}>
          <dl className={s.facts}>
            <dt>Mode</dt>
            <dd data-testid="sync-mode">{MODE_LABEL[sync.state.mode]}</dd>
            {sync.state.branch ? (
              <>
                <dt>Branch</dt>
                <dd className={s.mono}>
                  {sync.state.branch}
                  {sync.state.upstream ? ` → ${sync.state.upstream}` : ""}
                </dd>
              </>
            ) : null}
            {sync.state.problem_text ? (
              <>
                <dt>Problem</dt>
                <dd>{sync.state.problem_text}</dd>
              </>
            ) : null}
            <dt>Last run</dt>
            <dd data-testid="sync-last">
              {clock(sync.last_run)}
              {sync.last_result ? ` · ${sync.last_result.message}` : ""}
            </dd>
            <dt>Next run</dt>
            <dd>{clock(sync.next_run)}</dd>
          </dl>
          {sync.paused ? (
            <p>
              <Badge tone="warn" icon="!">
                Paused
              </Badge>{" "}
              Resolve the merge conflict in your KB folder, then resume.
            </p>
          ) : null}
          <div className={s.actions}>
            {sync.state.mode !== "off" ? (
              <Button loading={busy === "run"} onClick={() => void act("run", "/api/kbsync/run")}>
                Sync now
              </Button>
            ) : null}
            {sync.paused ? (
              <Button
                variant="primary"
                loading={busy === "resume"}
                onClick={() => void act("resume", "/api/kbsync/resume")}
              >
                Resume
              </Button>
            ) : null}
            {sync.state.mode === "off" && status.health?.modules.kb?.ok ? (
              <Button loading={busy === "init"} onClick={() => void act("init", "/api/kbsync/init")}>
                Initialize git
              </Button>
            ) : null}
          </div>
        </div>
      ) : (
        <p className={s.muted}>Loading…</p>
      )}
    </Panel>
  );
}

function AgentsSection({ status }: { status: ServerStatus }) {
  return (
    <Panel
      title="Agents"
      id="agents"
      actions={
        <Button size="sm" onClick={() => void status.refresh()}>
          Check again
        </Button>
      }
    >
      {status.runners ? (
        <ul className={s.list}>
          {Object.entries(status.runners).map(([id, r]) => (
            <li key={id} className={s.item}>
              <strong>{RUNNER_NAMES[id] ?? id}</strong>
              {r.available ? (
                <span>
                  <Badge tone="success" icon="✓">
                    Available
                  </Badge>{" "}
                  <span className={s.muted}>{r.version ?? ""}</span>
                </span>
              ) : (
                <span>
                  <Badge tone="warn" icon="!">
                    Unavailable
                  </Badge>{" "}
                  <span className={s.muted}>{r.detail}</span>
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className={s.muted}>Checking…</p>
      )}
    </Panel>
  );
}

function ConflictsSection({ status }: { status: ServerStatus }) {
  const n = status.conflicts.length;
  return (
    <Panel title="Conflicts" id="conflicts" actions={n ? <Badge>{`${n} to review`}</Badge> : undefined}>
      {n === 0 ? (
        <p className={s.muted}>No conflicts.</p>
      ) : (
        <>
          <p className={s.muted}>Jesinia never deletes or renames your files. Fix these in your editor.</p>
          <ul className={s.list}>
            {status.conflicts.map((c) => (
              <li key={`${c.kind}-${c.id}`} className={s.item}>
                <strong>{c.kind === "duplicate_id" ? "Duplicate id:" : "Missing page:"}</strong>
                <span className={s.mono}>id: {c.id}</span>
                {c.paths.map((p) => (
                  <span key={p} className={s.mono}>
                    {p}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function SecuritySection() {
  const client = useClient();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const rotate = async () => {
    setBusy(true);
    const r = await client.POST("/api/auth/rotate");
    setBusy(false);
    setConfirming(false);
    setDone(r.response.ok);
    const warning = (r.data as { warning?: string } | undefined)?.warning;
    if (warning) setWarning(warning);
  };
  return (
    <Panel title="Security" id="security">
      <div className={s.form}>
        <p>
          The login token lets browsers and agents talk to Jesinia on this Mac. Rotate it if a login link was shared by
          mistake.
        </p>
        {done ? (
          <p role="status">
            Token rotated. Open a new login link from <code>jesinia status</code>.{warning ? ` ${warning}` : ""}
          </p>
        ) : confirming ? (
          <div className={s.actions}>
            <span>You'll be signed out, and Claude Code and Codex are re-registered with the new token.</span>
            <Button variant="danger" loading={busy} onClick={() => void rotate()}>
              Rotate token
            </Button>
            <Button onClick={() => setConfirming(false)}>Cancel</Button>
          </div>
        ) : (
          <div className={s.actions}>
            <Button variant="danger" onClick={() => setConfirming(true)}>
              Rotate token…
            </Button>
          </div>
        )}
      </div>
    </Panel>
  );
}

/**
 * The book layout (design pass, R27): an outline of sections on the left selects the one the
 * view pane shows, with the pane's toolbar on top and the section's actions at its bottom.
 */
export function Settings({ status }: { status: ServerStatus }) {
  const section = useSection();
  const page = useRef<HTMLDivElement>(null);
  const opened = useRef(section);
  // Choosing another section moves focus to its title, so keyboard and screen-reader users land
  // on what they chose (not on the first open: focus then stays where the browser put it).
  useEffect(() => {
    if (opened.current === section) return;
    opened.current = section;
    const title = page.current?.querySelector<HTMLElement>("h2");
    title?.setAttribute("tabindex", "-1");
    title?.focus();
  }, [section]);
  const pages: Record<SectionId, ReactNode> = {
    kb: <KbSection status={status} />,
    sync: <SyncSection status={status} />,
    agents: <AgentsSection status={status} />,
    tags: <TagsSection />,
    rss: <RssSection />,
    issues: <IssuesSection status={status} />,
    conflicts: <ConflictsSection status={status} />,
    security: <SecuritySection />,
  };
  return (
    <div className={s.layout}>
      <nav className={s.nav} aria-label="Settings sections">
        {SECTIONS.map(([id, label]) => (
          <a key={id} href={`#${id}`} aria-current={section === id ? "location" : undefined}>
            {label}
          </a>
        ))}
      </nav>
      <section className={s.pane} aria-labelledby="settings-title">
        <header className={s.toolbar}>
          <h1 id="settings-title">Settings</h1>
          <p>Changes apply to every Jesinia app.</p>
        </header>
        <div ref={page} className={s.column}>
          {pages[section]}
        </div>
      </section>
    </div>
  );
}
