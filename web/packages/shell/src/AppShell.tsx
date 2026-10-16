// SPDX-License-Identifier: AGPL-3.0-or-later
import type { JesiniaClient } from "@jesinia/client";
import {
  Banner,
  BannerRegion,
  Button,
  CommandPalette,
  StatusIndicator,
  ToastProvider,
  useToast,
  type PaletteItem,
  type StatusItem,
} from "@jesinia/design";
import { useEffect, useMemo, type ReactNode } from "react";
import s from "./AppShell.module.css";
import { APPS, RUNNER_NAMES } from "./apps";
import { ClientProvider, useClient } from "./context";
import { useServerStatus, type ServerStatus } from "./status";
import { applyTheme, cycleTheme, readTheme } from "./theme";

export type ShellApp = { id: string; name: string; glyph?: string };

export type AppShellProps = {
  app: ShellApp;
  children: ReactNode | ((status: ServerStatus) => ReactNode);
  /** App controls placed in the title bar. */
  controls?: ReactNode;
  /** The app has its own search field: the title bar shows no ⌕ button (shell decision 3). */
  ownSearch?: boolean;
  appSearch?: (q: string, signal: AbortSignal) => Promise<PaletteItem[]>;
  commands?: PaletteItem[];
  client?: JesiniaClient;
};

function go(href: string, newWindow: boolean) {
  if (newWindow) window.open(href, "_blank", "noopener");
  else window.location.assign(href);
}

/** Status-dot items for minor conditions; blocking ones also get a banner. */
export function statusItems(st: ServerStatus): StatusItem[] {
  const items: StatusItem[] = [];
  const kb = st.health?.modules.kb;
  if (kb && !kb.ok) items.push({ id: "kb", severity: "blocking", title: "KB unavailable", detail: kb.detail, href: "/settings/#kb" });
  if (st.sync?.paused) {
    items.push({ id: "sync", severity: "blocking", title: "Sync paused", detail: "Resolve the conflict, then resume.", href: "/settings/#sync" });
  } else if (st.sync?.last_result?.outcome === "error") {
    items.push({ id: "sync-error", severity: "minor", title: "Sync failed", detail: st.sync.last_result.message, href: "/settings/#sync" });
  }
  const dups = st.conflicts.filter((c) => c.kind === "duplicate_id").length;
  const missing = st.conflicts.filter((c) => c.kind === "missing").length;
  if (dups) items.push({ id: "dups", severity: "minor", title: `${dups} duplicate page ${dups === 1 ? "id" : "ids"}`, detail: "Two files share an id.", href: "/settings/#conflicts" });
  if (missing) items.push({ id: "missing", severity: "minor", title: `${missing} missing ${missing === 1 ? "page" : "pages"}`, detail: "A file Jesinia indexed is gone.", href: "/settings/#conflicts" });
  for (const [id, r] of Object.entries(st.runners ?? {})) {
    if (!r.available) items.push({ id: `runner-${id}`, severity: "minor", title: `${RUNNER_NAMES[id] ?? id} isn't available`, detail: r.detail, href: "/settings/#agents" });
  }
  return items;
} // prettier-ignore

function Shell({ app, children, controls, ownSearch, appSearch, commands = [] }: AppShellProps) {
  const client = useClient();
  const toast = useToast();
  const st = useServerStatus();

  useEffect(() => applyTheme(readTheme()), []);

  const resume = async () => {
    const r = await client.POST("/api/kbsync/resume");
    if (r.data) {
      st.setSync(r.data as unknown as ServerStatus["sync"] & object);
      toast.show({ tone: "success", message: "Sync resumed." });
    }
  };
  const syncNow = async () => {
    const r = await client.POST("/api/kbsync/run");
    const body = r.data as { result?: { message: string; outcome: string }; status?: unknown } | undefined;
    if (body?.status) st.setSync(body.status as NonNullable<ServerStatus["sync"]>);
    if (body?.result) {
      toast.show({
        tone: body.result.outcome === "error" ? "danger" : "info",
        message: `Sync: ${body.result.message}`,
      });
    }
  };

  const shellCommands: PaletteItem[] = useMemo(
    () => [
      ...APPS.filter((a) => a.built && a.id !== app.id).map((a) => ({
        id: `open-${a.id}`,
        label: `Open ${a.name}`,
        detail: a.href,
        group: "command" as const,
        onRun: ({ newWindow }: { newWindow: boolean }) => go(a.href, newWindow),
      })),
      { id: "open-launcher", label: "Open launcher", detail: "/", group: "command", onRun: ({ newWindow }) => go("/", newWindow) },
      { id: "open-settings", label: "Open Settings", detail: "/settings/", group: "command", onRun: ({ newWindow }) => go("/settings/", newWindow) },
      { id: "sync-now", label: "Sync now", detail: "Commit and push", group: "command", onRun: () => void syncNow() },
      { id: "theme", label: "Toggle theme", group: "command", onRun: () => void cycleTheme() },
    ],
    [app.id],
  ); // prettier-ignore

  const kbSearch = async (q: string, signal: AbortSignal): Promise<PaletteItem[]> => {
    const r = await client.GET("/api/search", { params: { query: { q, source: ["kb"], limit: 12 } }, signal });
    return (r.data ?? []).map((h) => ({
      id: `kb-${h.key}`,
      label: h.title,
      group: "kb" as const,
      ...(h.path ? { detail: h.path } : {}),
      onRun: ({ newWindow }: { newWindow: boolean }) => go(`/notes/page/${encodeURIComponent(h.key)}`, newWindow),
    }));
  };

  const kb = st.health?.modules.kb;
  return (
    <div className={s.shell} data-app={app.id}>
      <header className={s.bar}>
        <a className={s.home} href="/" aria-label={`${app.name}: back to the launcher`}>
          <span className={s.glyph} aria-hidden="true">
            {app.glyph ? <img src={app.glyph} alt="" /> : null}
          </span>
          {app.name}
        </a>
        <div className={s.controls}>{controls}</div>
        <div className={s.end}>
          <StatusIndicator items={statusItems(st)} />
          {ownSearch ? null : (
            <Button
              variant="ghost"
              size="sm"
              aria-label="Search and commands (⌘K)"
              icon="⌕"
              onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
            />
          )}
          {app.id === "settings" ? null : <Button variant="ghost" size="sm" aria-label="Settings" icon="⚙" href="/settings/" />}
        </div>
      </header>
      <BannerRegion onMore={() => window.location.assign("/settings/")}>
        {st.signedOut ? (
          <Banner tone="danger" cause="Signed out:">
            open a new login link from <code>jesinia status</code> in a terminal.
          </Banner>
        ) : null}
        {kb && !kb.ok ? (
          <Banner tone="danger" cause="KB unavailable:" action={{ label: "Open Settings", onAction: () => go("/settings/#kb", false) }}>
            {kb.detail}. Choose a folder to continue.
          </Banner>
        ) : null}
        {st.sync?.paused ? (
          <Banner tone="warn" cause="Sync paused:" action={{ label: "Resume", onAction: () => void resume() }}
            secondaryAction={{ label: "Details", onAction: () => go("/settings/#sync", false) }}>
            the KB has a merge conflict. Resolve it, then choose Resume.
          </Banner>
        ) : null}
      </BannerRegion>
      <main className={s.main}>{typeof children === "function" ? children(st) : children}</main>
      <CommandPalette commands={[...commands, ...shellCommands]} kbSearch={kbSearch} {...(appSearch ? { appSearch } : {})} />
    </div>
  ); // prettier-ignore
}

/** The app frame every PWA uses: title bar, system banners, status, ⌘K, toasts. */
export function AppShell(props: AppShellProps) {
  return (
    <ClientProvider {...(props.client ? { client: props.client } : {})}>
      <ToastProvider>
        <Shell {...props} />
      </ToastProvider>
    </ClientProvider>
  );
}
