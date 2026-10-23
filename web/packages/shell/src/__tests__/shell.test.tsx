// SPDX-License-Identifier: AGPL-3.0-or-later
import { UNAUTHORIZED_EVENT } from "@jesinia/client";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  APPS,
  AppShell,
  appCommands,
  applyTheme,
  baseRoutes,
  cycleTheme,
  fakeClient,
  readTheme,
  registerServiceWorker,
  statusItems,
  syncStatus,
  useClient,
  type ServerStatus,
} from "../index";

class FakeSource {
  static last: FakeSource | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSource.last = this;
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {}
  emit(name: string, id: number, data: unknown) {
    this.listeners.get(name)?.(new MessageEvent(name, { data: JSON.stringify(data), lastEventId: String(id) }));
  }
}

const assign = vi.fn();
const open = vi.fn();

const NOTES_BUILT = APPS.some((a) => a.id === "notes" && a.built);
function setNotesBuilt(built: boolean) {
  for (const a of APPS) if (a.id === "notes") a.built = built;
}

afterEach(() => {
  setNotesBuilt(NOTES_BUILT);
  assign.mockReset();
  open.mockReset();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

// A search endpoint that, like the server, returns only the hits of the requested sources.
function searchRoute(hits: ({ source: string } & Record<string, unknown>)[]) {
  return (req: Request) => {
    const sources = new URL(req.url).searchParams.getAll("source");
    return Response.json(hits.filter((h) => sources.includes(h.source)));
  };
}

function setup(over = {}, props: Partial<Parameters<typeof AppShell>[0]> = {}) {
  vi.stubGlobal("EventSource", FakeSource);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign, origin: "http://localhost:3000" },
  });
  vi.stubGlobal("open", open);
  const { client, calls } = fakeClient(baseRoutes(over));
  render(
    <AppShell app={{ id: "notes", name: "Notes" }} client={client} {...props}>
      {(st) => <p>{st.loading ? "loading" : "ready"}</p>}
    </AppShell>,
  );
  return calls;
}

describe("AppShell", () => {
  it("renders the title bar, ⌕ and settings, and the healthy state", async () => {
    setup();
    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notes: back to the launcher" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "Status: all clear" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings/");
    await userEvent.click(screen.getByRole("button", { name: "Search and commands (⌘K)" }));
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    expect(document.querySelector("[data-app='notes']")).not.toBeNull();
  });

  it("shows blocking banners and resumes sync", async () => {
    const calls = setup({
      "GET /healthz": { ok: false, modules: { kb: { ok: false, detail: "KB folder not found: /x" } } },
      "GET /api/kbsync/status": syncStatus({ paused: true }),
      "POST /api/kbsync/resume": syncStatus(),
    });
    expect(await screen.findByText("KB unavailable:")).toBeInTheDocument();
    expect(screen.getByText("Sync paused:")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open Settings" }));
    expect(assign).toHaveBeenCalledWith("/settings/#kb");
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(assign).toHaveBeenCalledWith("/settings/#sync");
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(screen.queryByText("Sync paused:")).toBeNull());
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/api/kbsync/resume"))).toBe(true);
    expect((await screen.findAllByText("Sync resumed.")).length).toBeGreaterThan(0);
  });

  it("follows sync status over SSE and refetches conflicts on page changes", async () => {
    const calls = setup();
    await screen.findByText("ready");
    expect(decodeURIComponent(FakeSource.last?.url ?? "")).toContain(
      "/api/events?topics=kbsync.status,kb.page_changed",
    );
    act(() => FakeSource.last?.emit("kbsync.status", 1, syncStatus({ paused: true })));
    expect(await screen.findByText("Sync paused:")).toBeInTheDocument();
    const before = calls.filter((c) => c.url.endsWith("/api/kb/conflicts")).length;
    act(() => FakeSource.last?.emit("kb.page_changed", 2, { change: "created" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/api/kb/conflicts")).length).toBe(before + 1));
  });

  it("a server that doesn't answer settles loading and offers Retry", async () => {
    let down = true;
    setup({
      "GET /healthz": () =>
        down ? Promise.reject(new TypeError("Failed to fetch")) : Response.json({ ok: true, modules: {} }),
    });
    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.getByText("Jesinia isn't answering:")).toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText("Jesinia isn't answering:")).toBeNull());
  });

  it("a 401 shows the signed-out banner", async () => {
    setup();
    await screen.findByText("ready");
    act(() => {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    });
    expect(await screen.findByText("Signed out:")).toBeInTheDocument();
  });

  it("palette: shell commands, app commands, KB results", async () => {
    setNotesBuilt(true);
    const appRun = vi.fn();
    const calls = setup(
      {
        "GET /api/search": searchRoute([
          {
            source: "kb",
            key: "p1",
            title: "Budget",
            snippet: "",
            path: "Notes/Budget.md",
            authoritative: true,
            score: 1,
            url: null,
          },
        ]),
        "POST /api/kbsync/run": { result: { outcome: "error", message: "push failed" }, status: syncStatus() },
      },
      { commands: [{ id: "new", label: "New note", group: "command", onRun: appRun }], controls: <span>ctl</span> },
    );
    await screen.findByText("ready");
    expect(screen.getByText("ctl")).toBeInTheDocument();
    const openPalette = () => fireEvent.keyDown(window, { key: "k", metaKey: true });
    openPalette();
    expect(screen.queryByText("Open Notes")).toBeNull(); // the current app
    await userEvent.click(screen.getByText("New note"));
    expect(appRun).toHaveBeenCalled();
    openPalette();
    await userEvent.click(screen.getByText("Open launcher"));
    expect(assign).toHaveBeenCalledWith("/");
    openPalette();
    fireEvent.click(screen.getByText("Open Settings"), { metaKey: true });
    expect(open).toHaveBeenCalledWith("/settings/", "_blank", "noopener");
    openPalette();
    // Eight commands now that Issues is built: the group shows six and "Show all".
    await userEvent.click(screen.getByText("Show all 8"));
    await userEvent.click(screen.getByText("Toggle theme"));
    expect(document.documentElement.dataset.theme).toBe("light");
    openPalette();
    await userEvent.click(screen.getByText("Show all 8")); // Sync now is past the first six
    await userEvent.click(screen.getByText("Sync now"));
    expect((await screen.findAllByText("Sync: push failed")).length).toBeGreaterThan(0);
    openPalette();
    await userEvent.type(screen.getByRole("combobox"), "budget");
    await userEvent.click(await screen.findByText("Budget"));
    expect(assign).toHaveBeenCalledWith("/notes/page/p1");
    openPalette();
    await userEvent.type(screen.getByRole("combobox"), "budget");
    fireEvent.click(await screen.findByText("Budget"), { metaKey: true });
    expect(open).toHaveBeenCalledWith("/notes/page/p1", "_blank", "noopener");
    const search = calls.find((c) => c.url.includes("/api/search"));
    expect(search).toBeDefined();
    const searches = calls.filter((c) => c.url.includes("/api/search")).map((c) => new URL(c.url).searchParams);
    expect(new Set(searches.map((q) => q.getAll("source").join()))).toEqual(new Set(["kb", "zotero"]));
    expect(new Set(searches.map((q) => q.get("limit")))).toEqual(new Set(["6"]));
  });

  describe("palette search groups", () => {
    const hit = (source: string, key: string) => ({
      source,
      key,
      title: `${source} ${key}`,
      snippet: "",
      path: null,
      authoritative: source === "kb",
      score: 1,
      url: source === "zotero" ? `zotero://select/library/items/${key}` : null,
    });
    const searchSetup = (handler: (source: string) => Response | Promise<Response>) =>
      setup({
        "GET /api/search": (req: Request) => handler(new URL(req.url).searchParams.get("source") ?? ""),
      });
    const type = async () => {
      await screen.findByText("ready");
      fireEvent.keyDown(window, { key: "k", metaKey: true });
      await userEvent.type(screen.getByRole("combobox"), "q");
    };

    it("still shows papers when there are many KB hits", async () => {
      searchSetup((source) =>
        Response.json(
          source === "kb" ? Array.from({ length: 6 }, (_, i) => hit("kb", `k${i}`)) : [hit("zotero", "PAPER001")],
        ),
      );
      await type();
      const group = await screen.findByRole("group", { name: "Papers" });
      expect(within(group).getByText("zotero PAPER001")).toBeInTheDocument();
      expect(screen.getByText("kb k0")).toBeInTheDocument();
    });

    it("still shows KB hits when the paper search fails", async () => {
      searchSetup((source) =>
        source === "kb" ? Response.json([hit("kb", "k1")]) : Response.json({ detail: "down" }, { status: 503 }),
      );
      await type();
      expect(await screen.findByText("kb k1")).toBeInTheDocument();
      expect(screen.queryByRole("group", { name: "Papers" })).toBeNull();
    });

    it("still shows KB hits when the paper request throws", async () => {
      searchSetup((source) =>
        source === "kb" ? Response.json([hit("kb", "k1")]) : Promise.reject(new TypeError("Failed to fetch")),
      );
      await type();
      expect(await screen.findByText("kb k1")).toBeInTheDocument();
      expect(screen.queryByRole("group", { name: "Papers" })).toBeNull();
    });
  });

  it("palette shows paper hits under Papers and opens their url", async () => {
    const href = vi.fn();
    const paper = (key: string, url: string | null) => ({
      source: "zotero",
      key,
      title: `Paper ${key} \u2014 Smith 2024`,
      snippet: "",
      path: null,
      authoritative: false,
      score: 1,
      url,
    });
    setup({
      "GET /api/search": searchRoute([
        paper("ABCD1234", "zotero://select/library/items/ABCD1234"),
        paper("NOURL000", null),
        paper("EVIL0000", "https://evil.example/"),
      ]),
    });
    const location = { ...window.location, assign, origin: "http://localhost:3000" };
    Object.defineProperty(location, "href", { configurable: true, get: () => "http://localhost:3000/", set: href });
    Object.defineProperty(window, "location", { configurable: true, value: location });
    await screen.findByText("ready");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.type(screen.getByRole("combobox"), "smith");
    const group = await screen.findByRole("group", { name: "Papers" });
    expect(within(group).getAllByRole("option")).toHaveLength(1);
    await userEvent.click(within(group).getByText(/Paper ABCD1234/));
    expect(href).toHaveBeenCalledWith("zotero://select/library/items/ABCD1234");
  });

  it("apps with their own search have no ⌕ button; static children render", async () => {
    vi.stubGlobal("EventSource", FakeSource);
    const { client } = fakeClient(baseRoutes());
    render(
      <AppShell app={{ id: "settings", name: "Settings", glyph: "/g.png" }} client={client} ownSearch>
        <p>static</p>
      </AppShell>,
    );
    expect(screen.getByText("static")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /⌘K/ })).toBeNull();
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    await screen.findByRole("button", { name: "Status: all clear" });
  });
});

describe("AppShell edge cases", () => {
  it("tolerates failing endpoints, opens other apps, and collapses extra banners", async () => {
    setNotesBuilt(true);
    vi.stubGlobal("EventSource", FakeSource);
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign, origin: "http://localhost:3000" },
    });
    const fail = () => Response.json({ detail: "down" }, { status: 500 });
    const { client } = fakeClient({
      "GET /healthz": { ok: false, modules: { kb: { ok: false, detail: "gone" } } },
      "GET /api/kbsync/status": syncStatus({ paused: true }),
      "GET /api/kb/conflicts": fail,
      "GET /api/agents/runners": fail,
      "POST /api/kbsync/resume": fail,
      "POST /api/kbsync/run": fail,
      "GET /api/search": searchRoute([
        {
          source: "kb",
          key: "p2",
          title: "No path",
          snippet: "",
          path: null,
          authoritative: true,
          score: 1,
          url: null,
        },
      ]),
    });
    render(
      <AppShell app={{ id: "launcher", name: "Jesinia" }} client={client}>
        <p>x</p>
      </AppShell>,
    );
    expect(await screen.findByText("Sync paused:")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(screen.getByText("Sync paused:")).toBeInTheDocument();
    act(() => {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    });
    await userEvent.click(await screen.findByRole("button", { name: "+1 more" }));
    expect(assign).toHaveBeenCalledWith("/settings/");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.click(screen.getByText("Open Notes"));
    expect(assign).toHaveBeenCalledWith("/notes/");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.click(screen.getByText(/^Show all/)); // Sync now is past the first six
    await userEvent.click(screen.getByText("Sync now"));
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.type(screen.getByRole("combobox"), "no");
    fireEvent.click(await screen.findByText("No path"), { metaKey: true });
    expect(open).toHaveBeenCalledWith("/notes/page/p2", "_blank", "noopener");
  });

  it("offers no KB results while Notes is unbuilt", async () => {
    setNotesBuilt(false);
    const calls = setup({
      "GET /api/search": searchRoute([
        { source: "kb", key: "p3", title: "Hidden", snippet: "", path: null, authoritative: true, score: 1, url: null },
      ]),
    });
    await screen.findByText("ready");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await userEvent.type(screen.getByRole("combobox"), "hid");
    await new Promise((r) => setTimeout(r, 300)); // past the palette's debounce
    expect(screen.queryByText("Hidden")).toBeNull();
    expect(calls.some((c) => c.url.includes("/api/search"))).toBe(false);
  });

  it("creates its own client when none is given", async () => {
    vi.stubGlobal("EventSource", FakeSource);
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => Response.json({ detail: "x" }, { status: 404 }));
    render(
      <AppShell app={{ id: "notes", name: "Notes" }}>
        <p>own</p>
      </AppShell>,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    fetch.mockRestore();
  });
});

describe("appCommands", () => {
  it("offers every built app except the current one", () => {
    Object.defineProperty(window, "location", { configurable: true, value: { ...window.location, assign } });
    const apps = [
      { id: "notes", name: "Notes", href: "/notes/", description: "", built: true },
      { id: "rss", name: "RSS", href: "/rss/", description: "", built: false },
      { id: "here", name: "Here", href: "/here/", description: "", built: true },
    ];
    const cmds = appCommands(apps, "here");
    expect(cmds.map((c) => c.label)).toEqual(["Open Notes"]);
    cmds[0]?.onRun({ newWindow: false });
    expect(assign).toHaveBeenCalledWith("/notes/");
  });
});

describe("statusItems", () => {
  it("lists blocking and minor conditions", () => {
    const st = {
      health: { ok: false, modules: { kb: { ok: false, detail: "gone" } } },
      sync: syncStatus({ paused: true }),
      conflicts: [
        { kind: "duplicate_id", id: "a", paths: [], detected_at: "" },
        { kind: "duplicate_id", id: "b", paths: [], detected_at: "" },
        { kind: "missing", id: "c", paths: [], detected_at: "" },
      ],
      runners: {
        codex: { available: false, version: null, detail: "not logged in" },
        x: { available: false, version: null, detail: "?" },
      },
    } as unknown as ServerStatus;
    const items = statusItems(st);
    expect(items.map((i) => i.id)).toEqual(["kb", "sync", "dups", "missing", "runner-codex", "runner-x"]);
    expect(items.find((i) => i.id === "dups")?.title).toBe("2 duplicate page ids");
    expect(items.find((i) => i.id === "runner-codex")?.title).toBe("Codex isn't available");
    const one = statusItems({
      health: null,
      sync: syncStatus({ last_result: { outcome: "error", message: "push failed" } }),
      conflicts: [
        { kind: "duplicate_id", id: "a", paths: [], detected_at: "" },
        { kind: "missing", id: "b", paths: [], detected_at: "" },
      ],
      runners: null,
    } as unknown as ServerStatus);
    expect(one.map((i) => i.title)).toEqual(["Sync failed", "1 duplicate page id", "1 missing page"]);
  });
});

describe("theme, PWA, context", () => {
  it("cycles system → light → dark → system and persists", () => {
    expect(readTheme()).toBe("system");
    expect(cycleTheme()).toBe("light");
    expect(localStorage.getItem("jesinia.theme")).toBe("light");
    expect(cycleTheme()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(cycleTheme()).toBe("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("survives unavailable storage", () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readTheme()).toBe("system");
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    get.mockRestore();
    set.mockRestore();
  });

  it("registers the service worker when supported", async () => {
    const register = vi.fn().mockResolvedValueOnce({}).mockRejectedValueOnce(new Error("no"));
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: { register } });
    expect(await registerServiceWorker("/notes/")).toBe(true);
    expect(register).toHaveBeenCalledWith("/notes/sw.js", { scope: "/notes/" });
    expect(await registerServiceWorker("/")).toBe(false);
    // @ts-expect-error -- removing the API to simulate an unsupported browser
    delete navigator.serviceWorker;
    expect(await registerServiceWorker("/")).toBe(false);
  });

  it("useClient needs a provider", () => {
    const Bad = () => {
      useClient();
      return null;
    };
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Bad />)).toThrow("ClientProvider");
  });
});
