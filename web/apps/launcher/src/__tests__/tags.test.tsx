// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, fakeClient } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";

const CATALOG = [
  { tag: "sqlite", meaning: "SQLite databases", aliases: ["sqlite3"], retired: false, count: 4 },
  { tag: "python", meaning: "Python language", aliases: [], retired: false, count: 9 },
  { tag: "old", meaning: "Legacy", aliases: [], retired: true, count: 0 },
];
const UNCATALOGED = [{ tag: "rust", count: 2 }];

function renderTags(over = {}) {
  const { client, calls } = fakeClient(
    baseRoutes({
      "GET /api/kb/tags": CATALOG,
      "GET /api/kb/tags/uncataloged": UNCATALOGED,
      ...over,
    }),
  );
  render(<App path="/settings/" client={client} />);
  return calls;
}

const posted = async (calls: Request[], path: string) => {
  const call = calls.find((c) => c.method === "POST" && new URL(c.url).pathname === path);
  return call ? await call.clone().json() : undefined;
};

const keyOf = (call: Request) => call.headers.get("Idempotency-Key");
const callsTo = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);

const section = async () => within(await screen.findByRole("region", { name: "Tags" }));

describe("Settings → Tags", () => {
  it("lists the catalog with counts, status and aliases, and links the section", async () => {
    renderTags();
    const tags = await section();
    expect(await tags.findByText("#sqlite")).toBeInTheDocument();
    expect(tags.getByText("SQLite databases")).toBeInTheDocument();
    expect(tags.getByText(/sqlite3/)).toBeInTheDocument();
    expect(tags.getByText("4 pages")).toBeInTheDocument();
    expect(tags.getByText("Retired")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tags" })).toHaveAttribute("href", "#tags");
  });

  it("filters by name or meaning and sorts by name or count", async () => {
    renderTags();
    const tags = await section();
    await tags.findByText("#sqlite");
    const names = () => tags.getAllByTestId("tag-name").map((n) => n.textContent);
    expect(names()).toEqual(["#old", "#python", "#sqlite"]);
    await userEvent.click(tags.getByRole("button", { name: "Sort by count" }));
    expect(names()).toEqual(["#python", "#sqlite", "#old"]);
    await userEvent.click(tags.getByRole("button", { name: "Sort by name" }));
    await userEvent.type(tags.getByLabelText("Filter tags"), "legacy");
    expect(names()).toEqual(["#old"]);
    await userEvent.clear(tags.getByLabelText("Filter tags"));
    await userEvent.type(tags.getByLabelText("Filter tags"), "zzz");
    expect(await tags.findByText("No tags match that filter.")).toBeInTheDocument();
  });

  it("shows an empty state and a load error", async () => {
    renderTags({ "GET /api/kb/tags": [], "GET /api/kb/tags/uncataloged": [] });
    const tags = await section();
    expect(await tags.findByText("No tags yet. Add one above.")).toBeInTheDocument();
    expect(tags.getByText("Every tag in your pages is in the catalog.")).toBeInTheDocument();
  });

  it("explains when the catalog can't be loaded", async () => {
    renderTags({
      "GET /api/kb/tags": () => Response.json({ detail: "Set a KB folder first." }, { status: 400 }),
    });
    const tags = await section();
    expect(await tags.findByText("Set a KB folder first.")).toBeInTheDocument();
  });

  it("rename preview lists pages and reserved pages as not updated; Apply posts the revisions", async () => {
    const calls = renderTags({
      "POST /api/kb/tags/rename/preview": {
        pages: [
          { id: "p1", title: "Intro to SQL", rev: "r1" },
          { id: "p2", title: "Backups", rev: "r2" },
        ],
        skipped_reserved: ["private/diary.md"],
      },
      "POST /api/kb/tags/rename": { updated: ["p1", "p2"], skipped_changed: [] },
    });
    const tags = await section();
    const row = (await tags.findByText("#sqlite")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Rename #sqlite" }));
    await userEvent.type(dialog.getByLabelText("New name"), "sqlite-db");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    expect(await dialog.findByText("Intro to SQL")).toBeInTheDocument();
    expect(dialog.getByText("Backups")).toBeInTheDocument();
    expect(dialog.getByText("Not updated (private folder)")).toBeInTheDocument();
    expect(dialog.getByText("private/diary.md")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Apply" }));
    expect((await screen.findAllByText("Renamed #sqlite to #sqlite-db in 2 pages.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/rename")).toEqual({
      old: "sqlite",
      new: "sqlite-db",
      revs: { p1: "r1", p2: "r2" },
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("reports pages that changed since the preview, and singular counts", async () => {
    renderTags({
      "POST /api/kb/tags/rename/preview": { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
      "POST /api/kb/tags/rename": { updated: ["p1"], skipped_changed: ["p2", "p3"] },
    });
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("New name"), "py");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    await dialog.findByText("A");
    expect(dialog.getByText("No pages are skipped.")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Apply" }));
    expect(
      (await screen.findAllByText("Renamed #python to #py in 1 page. 2 changed since the preview and were skipped."))
        .length,
    ).toBeGreaterThan(0);
  });

  it("shows preview and apply errors, and Cancel closes the dialog", async () => {
    let attempt = 0;
    renderTags({
      "POST /api/kb/tags/rename/preview": () =>
        attempt++ === 0
          ? Response.json({ detail: "Tag names can't be empty." }, { status: 400 })
          : Response.json({ pages: [], skipped_reserved: [] }),
      "POST /api/kb/tags/rename": () => Response.json({ detail: "Index is busy." }, { status: 503 }),
    });
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("New name"), "py");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    expect(await dialog.findByText("Tag names can't be empty.")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    await dialog.findByText("No pages use this tag yet.");
    await userEvent.click(dialog.getByRole("button", { name: "Apply" }));
    expect(await dialog.findByText("Index is busy.")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("changing the new name invalidates an earlier preview", async () => {
    renderTags({
      "POST /api/kb/tags/rename/preview": { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
    });
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("New name"), "py");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    await dialog.findByText("A");
    await userEvent.type(dialog.getByLabelText("New name"), "x");
    expect(dialog.queryByText("A")).toBeNull();
    expect(dialog.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("merge combines per-source previews and posts one merge", async () => {
    const calls = renderTags({
      "POST /api/kb/tags/rename/preview": async (req: Request) => {
        const body = (await req.json()) as { old: string };
        return Response.json(
          body.old === "sqlite"
            ? { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: ["x.md"] }
            : { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
        );
      },
      "POST /api/kb/tags/merge": { updated: ["p1"], skipped_changed: [] },
    });
    const tags = await section();
    const row = (await tags.findByText("#sqlite")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Merge into…" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Merge #sqlite into…" }));
    await userEvent.type(dialog.getByLabelText("Merge into"), "python");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    expect(await dialog.findAllByText("A")).toHaveLength(1);
    await userEvent.click(dialog.getByRole("button", { name: "Apply" }));
    expect((await screen.findAllByText("Merged #sqlite into #python in 1 page.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/merge")).toEqual({
      sources: ["sqlite"],
      into: "python",
      revs: { p1: "r1" },
    });
  });

  it("merge shows the preview error from any source", async () => {
    renderTags({
      "POST /api/kb/tags/rename/preview": () => Response.json({ detail: "Nope." }, { status: 400 }),
    });
    const tags = await section();
    const row = (await tags.findByText("#sqlite")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Merge into…" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("Merge into"), "python");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    expect(await dialog.findByText("Nope.")).toBeInTheDocument();
  });

  it("retire posts and refreshes; a failure is shown", async () => {
    let ok = false;
    const calls = renderTags({
      "POST /api/kb/tags/retire": () =>
        ok ? new Response(null, { status: 204 }) : Response.json({ detail: "Can't retire." }, { status: 400 }),
    });
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Retire" }));
    expect((await screen.findAllByText("Can't retire.")).length).toBeGreaterThan(0);
    ok = true;
    await userEvent.click(within(row).getByRole("button", { name: "Retire" }));
    expect((await screen.findAllByText("Retired #python.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/retire")).toEqual({ tag: "python" });
    expect(
      within(tags.getByText("#old").closest("li") as HTMLElement).queryByRole("button", { name: "Retire" }),
    ).toBeNull();
  });

  it("a 409 near-duplicate on add offers 'Use #sqlite?' with a one-click accept", async () => {
    const calls = renderTags({
      "POST /api/kb/tags": () =>
        Response.json({ error: "near_duplicate", detail: "Looks like #sqlite.", near: ["sqlite"] }, { status: 409 }),
    });
    const tags = await section();
    await tags.findByText("#sqlite");
    await userEvent.type(tags.getByLabelText("New tag"), "sqlite3");
    await userEvent.type(tags.getByLabelText("Meaning"), "databases");
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    expect(await tags.findByText("Use #sqlite?")).toBeInTheDocument();
    expect(await posted(calls, "/api/kb/tags")).toEqual({ tag: "sqlite3", meaning: "databases", force: false });
    await userEvent.click(tags.getByRole("button", { name: "Use #sqlite" }));
    expect(tags.queryByText("Use #sqlite?")).toBeNull();
    expect(tags.getByLabelText("New tag")).toHaveValue("");
  });

  it("'Add anyway' forces the add", async () => {
    let forced = false;
    const calls = renderTags({
      "POST /api/kb/tags": async (req: Request) => {
        const body = (await req.json()) as { force: boolean };
        if (body.force) {
          forced = true;
          return new Response(null, { status: 204 });
        }
        return Response.json({ error: "near_duplicate", detail: "Close.", near: ["sqlite", "sql"] }, { status: 409 });
      },
    });
    const tags = await section();
    await tags.findByText("#sqlite");
    await userEvent.type(tags.getByLabelText("New tag"), "sqlite3");
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    await tags.findByText("Use #sqlite?");
    await userEvent.click(tags.getByRole("button", { name: "Add anyway" }));
    await waitFor(() => expect(forced).toBe(true));
    expect((await screen.findAllByText("Added #sqlite3.")).length).toBeGreaterThan(0);
    expect(calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/kb/tags")).length).toBeGreaterThan(1);
  });

  it("a 409 exact duplicate or a 400 shows the server's message", async () => {
    let status = 409;
    renderTags({
      "POST /api/kb/tags": () => Response.json({ error: "duplicate", detail: "#sqlite already exists." }, { status }),
    });
    const tags = await section();
    await tags.findByText("#sqlite");
    await userEvent.type(tags.getByLabelText("New tag"), "sqlite");
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    expect(await tags.findByText("#sqlite already exists.")).toBeInTheDocument();
    status = 400;
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    expect(await tags.findByText("#sqlite already exists.")).toBeInTheDocument();
  });

  it("adopts and ignores uncataloged tags", async () => {
    let remaining = UNCATALOGED;
    const calls = renderTags({
      "GET /api/kb/tags/uncataloged": () => Response.json(remaining),
      "POST /api/kb/tags/adopt": () => {
        remaining = [];
        return new Response(null, { status: 204 });
      },
      "POST /api/kb/tags/ignore": () => Response.json({ detail: "Can't ignore." }, { status: 400 }),
    });
    const tags = await section();
    const row = (await tags.findByText("#rust")).closest("li") as HTMLElement;
    expect(within(row).getByText("2 pages")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Ignore" }));
    expect((await screen.findAllByText("Can't ignore.")).length).toBeGreaterThan(0);
    await userEvent.click(within(row).getByRole("button", { name: "Adopt" }));
    expect((await screen.findAllByText("Adopted #rust.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/adopt")).toEqual({ tag: "rust" });
    await waitFor(() => expect(tags.queryByText("#rust")).toBeNull());
  });

  it("ignoring removes the tag; uncataloged Merge into… opens the merge dialog", async () => {
    let remaining = UNCATALOGED;
    renderTags({
      "GET /api/kb/tags/uncataloged": () => Response.json(remaining),
      "POST /api/kb/tags/ignore": () => {
        remaining = [];
        return new Response(null, { status: 204 });
      },
    });
    const tags = await section();
    const row = (await tags.findByText("#rust")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Merge into…" }));
    await screen.findByRole("dialog", { name: "Merge #rust into…" });
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(row).getByRole("button", { name: "Ignore" }));
    expect((await screen.findAllByText("Ignored #rust.")).length).toBeGreaterThan(0);
    await waitFor(() => expect(tags.queryByText("#rust")).toBeNull());
  });

  it("traps focus in the dialog, inerts the page, closes on Escape and restores focus", async () => {
    renderTags();
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    const invoker = within(row).getByRole("button", { name: "Rename" });
    await userEvent.click(invoker);
    const dialogEl = await screen.findByRole("dialog");
    const dialog = within(dialogEl);
    expect(dialog.getByLabelText("New name")).toHaveFocus();
    expect(document.getElementById("root") ?? document.body.firstElementChild).toHaveAttribute("inert");
    await userEvent.type(dialog.getByLabelText("New name"), "py");
    await userEvent.tab({ shift: true });
    expect(dialog.getByRole("button", { name: "Preview" })).toHaveFocus();
    await userEvent.tab();
    expect(dialog.getByLabelText("New name")).toHaveFocus();
    for (let i = 0; i < 6; i++) {
      await userEvent.tab();
      expect(dialogEl.contains(document.activeElement)).toBe(true);
    }
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(invoker).toHaveFocus();
    expect(document.querySelector("[inert]")).toBeNull();
  });

  it("Escape closes even when focus is on the dialog's own button", async () => {
    renderTags();
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    dialog.getByRole("button", { name: "Cancel" }).focus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Enter in the field runs Preview", async () => {
    const calls = renderTags({
      "POST /api/kb/tags/rename/preview": { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
    });
    const tags = await section();
    const row = (await tags.findByText("#python")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("New name"), "py{Enter}");
    expect(await dialog.findByText("A")).toBeInTheDocument();
    expect(await posted(calls, "/api/kb/tags/rename/preview")).toEqual({ old: "python", new: "py" });
  });

  it("merges several tags into one with a single request and combined revisions", async () => {
    const calls = renderTags({
      "POST /api/kb/tags/rename/preview": async (req: Request) => {
        const body = (await req.json()) as { old: string };
        return Response.json(
          body.old === "sqlite"
            ? { pages: [{ id: "p1", title: "From sqlite", rev: "r1" }], skipped_reserved: [] }
            : { pages: [{ id: "p2", title: "From old", rev: "r2" }], skipped_reserved: ["x.md"] },
        );
      },
      "POST /api/kb/tags/merge": { updated: ["p1", "p2"], skipped_changed: [] },
    });
    const tags = await section();
    const row = (await tags.findByText("#sqlite")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Merge into…" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("Also merge"), "#old{Enter}");
    expect(dialog.getByText("#old")).toBeInTheDocument();
    await userEvent.type(dialog.getByLabelText("Also merge"), "temp");
    await userEvent.click(dialog.getByRole("button", { name: "Add" }));
    await userEvent.click(dialog.getByRole("button", { name: "Remove #temp" }));
    await userEvent.type(dialog.getByLabelText("Also merge"), "old");
    await userEvent.click(dialog.getByRole("button", { name: "Add" }));
    await userEvent.type(dialog.getByLabelText("Merge into"), "python");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    expect(await dialog.findByText("From sqlite")).toBeInTheDocument();
    expect(dialog.getByText("From old")).toBeInTheDocument();
    expect(dialog.getByText("x.md")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Apply" }));
    expect((await screen.findAllByText("Merged #sqlite, #old into #python in 2 pages.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/merge")).toEqual({
      sources: ["sqlite", "old"],
      into: "python",
      revs: { p1: "r1", p2: "r2" },
    });
  });

  it("Restore shows on retired rows only and posts restore", async () => {
    const calls = renderTags({ "POST /api/kb/tags/restore": () => new Response(null, { status: 204 }) });
    const tags = await section();
    const python = (await tags.findByText("#python")).closest("li") as HTMLElement;
    expect(within(python).queryByRole("button", { name: "Restore" })).toBeNull();
    const old = tags.getByText("#old").closest("li") as HTMLElement;
    await userEvent.click(within(old).getByRole("button", { name: "Restore" }));
    expect((await screen.findAllByText("Restored #old.")).length).toBeGreaterThan(0);
    expect(await posted(calls, "/api/kb/tags/restore")).toEqual({ tag: "old" });
  });

  it("every mutating call sends an Idempotency-Key; reads and previews do not", async () => {
    const empty = () => new Response(null, { status: 204 });
    const calls = renderTags({
      "POST /api/kb/tags": empty,
      "POST /api/kb/tags/retire": empty,
      "POST /api/kb/tags/adopt": empty,
      "POST /api/kb/tags/ignore": empty,
      "POST /api/kb/tags/rename/preview": { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
      "POST /api/kb/tags/rename": { updated: ["p1"], skipped_changed: [] },
      "POST /api/kb/tags/merge": { updated: ["p1"], skipped_changed: [] },
    });
    const tags = await section();
    await userEvent.type(await tags.findByLabelText("New tag"), "graphs");
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    await screen.findAllByText("Added #graphs.");
    const python = tags.getByText("#python").closest("li") as HTMLElement;
    await userEvent.click(within(python).getByRole("button", { name: "Retire" }));
    await screen.findAllByText("Retired #python.");
    const rust = tags.getByText("#rust").closest("li") as HTMLElement;
    await userEvent.click(within(rust).getByRole("button", { name: "Adopt" }));
    await userEvent.click(within(rust).getByRole("button", { name: "Ignore" }));
    await screen.findAllByText("Ignored #rust.");
    for (const mode of ["Rename", "Merge into…"]) {
      const sqlite = tags.getByText("#sqlite").closest("li") as HTMLElement;
      await userEvent.click(within(sqlite).getByRole("button", { name: mode }));
      const dialog = within(await screen.findByRole("dialog"));
      await userEvent.type(dialog.getByRole("textbox", { name: /New name|Merge into/ }), "db");
      await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
      await userEvent.click(await dialog.findByRole("button", { name: "Apply" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    }
    const writes = ["", "/retire", "/adopt", "/ignore", "/rename", "/merge"].map((p) => `/api/kb/tags${p}`);
    const keys = writes.map((path) => {
      const sent = callsTo(calls, "POST", path);
      expect(sent, path).toHaveLength(1);
      return keyOf(sent[0] as Request);
    });
    keys.forEach((key) => expect(key).toMatch(/^[0-9a-f-]{36}$/));
    expect(new Set(keys).size).toBe(keys.length);
    const reads = calls.filter((c) => !writes.includes(new URL(c.url).pathname));
    expect(reads.length).toBeGreaterThan(0);
    reads.forEach((c) => expect(keyOf(c)).toBeNull());
  });

  it("a retry after a server error reuses the action's key; an answered action starts fresh", async () => {
    let status = 503;
    const calls = renderTags({
      "POST /api/kb/tags/retire": () =>
        status === 204 ? new Response(null, { status }) : Response.json({ detail: "Not now." }, { status }),
    });
    const tags = await section();
    const python = (await tags.findByText("#python")).closest("li") as HTMLElement;
    const retire = () => userEvent.click(within(python).getByRole("button", { name: "Retire" }));
    await retire();
    await screen.findAllByText("Not now.");
    status = 400;
    await retire();
    await waitFor(() => expect(callsTo(calls, "POST", "/api/kb/tags/retire")).toHaveLength(2));
    status = 204;
    await retire();
    await screen.findAllByText("Retired #python.");
    const sqlite = tags.getByText("#sqlite").closest("li") as HTMLElement;
    await userEvent.click(within(sqlite).getByRole("button", { name: "Retire" }));
    await screen.findAllByText("Retired #sqlite.");
    const keys = callsTo(calls, "POST", "/api/kb/tags/retire").map(keyOf);
    expect(keys).toHaveLength(4);
    expect(keys[1]).toBe(keys[0]); // retried after the 503: same action, same key
    expect(keys[2]).not.toBe(keys[1]); // the 400 was an answer: clicking again is a new action
    expect(keys[3]).not.toBe(keys[2]);
  });

  it("keeps the key when the network fails, so the next try is the same action", async () => {
    let fail = true;
    const calls = renderTags({
      "POST /api/kb/tags/ignore": () => {
        if (fail) return Promise.reject(new TypeError("offline"));
        return new Response(null, { status: 204 });
      },
    });
    const tags = await section();
    const rust = (await tags.findByText("#rust")).closest("li") as HTMLElement;
    await userEvent.click(within(rust).getByRole("button", { name: "Ignore" }));
    expect((await screen.findAllByText("That didn't work.")).length).toBeGreaterThan(0);
    fail = false;
    await userEvent.click(within(rust).getByRole("button", { name: "Ignore" }));
    await screen.findAllByText("Ignored #rust.");
    const keys = callsTo(calls, "POST", "/api/kb/tags/ignore").map(keyOf);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("an offline add stops loading, shows the cause, and the retry reuses the key", async () => {
    let fail = true;
    const calls = renderTags({
      "POST /api/kb/tags": () =>
        fail ? Promise.reject(new TypeError("offline")) : new Response(null, { status: 204 }),
    });
    const tags = await section();
    await userEvent.type(await tags.findByLabelText("New tag"), "graphs");
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    expect(await tags.findByText("Couldn't add that tag: offline")).toBeInTheDocument();
    expect(tags.getByRole("button", { name: "Add tag" })).not.toHaveAttribute("aria-busy");
    fail = false;
    await userEvent.click(tags.getByRole("button", { name: "Add tag" }));
    await screen.findAllByText("Added #graphs.");
    const keys = callsTo(calls, "POST", "/api/kb/tags").map(keyOf);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("an offline apply stops loading and shows the cause", async () => {
    renderTags({
      "POST /api/kb/tags/rename/preview": { pages: [{ id: "p1", title: "A", rev: "r1" }], skipped_reserved: [] },
      "POST /api/kb/tags/rename": () => Promise.reject(new TypeError("offline")),
    });
    const tags = await section();
    const row = (await tags.findByText("#sqlite")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const dialog = within(await screen.findByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("New name"), "db");
    await userEvent.click(dialog.getByRole("button", { name: "Preview" }));
    await userEvent.click(await dialog.findByRole("button", { name: "Apply" }));
    expect(await dialog.findByText("Couldn't apply that change: offline")).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "Apply" })).not.toHaveAttribute("aria-busy");
  });
});
