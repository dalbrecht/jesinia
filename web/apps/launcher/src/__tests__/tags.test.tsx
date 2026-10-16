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
});
