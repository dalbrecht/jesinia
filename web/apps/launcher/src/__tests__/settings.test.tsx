// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, fakeClient, syncStatus } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";

/** Settings opened at a section (R27: the view pane shows one section at a time). */
function renderSettings(over = {}, hash = "#kb") {
  window.history.replaceState(null, "", `/settings/${hash}`);
  const { client, calls } = fakeClient(baseRoutes(over));
  render(<App path="/settings/" client={client} />);
  return calls;
}

/** Choose a section in the outline and wait for the view pane to show it. */
async function go(label: string) {
  const outline = screen.getByRole("navigation", { name: "Settings sections" });
  await userEvent.click(within(outline).getByRole("link", { name: label }));
  return within(await screen.findByRole("region", { name: label }));
}

afterEach(() => window.history.replaceState(null, "", "/settings/"));

describe("Settings", () => {
  it("invalid KB path shows the server's error", async () => {
    renderSettings({
      "PUT /api/kb/path": () => Response.json({ detail: "KB folder not found: /nope" }, { status: 400 }),
    });
    const field = await screen.findByLabelText("KB folder");
    await screen.findByDisplayValue("/Users/me/kb");
    await userEvent.clear(field);
    await userEvent.type(field, "/nope");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("KB folder not found: /nope")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");
  });

  it("saves a valid path and confirms", async () => {
    const calls = renderSettings({ "PUT /api/kb/path": { path: "/Users/me/kb2", indexed: 3 } });
    const field = await screen.findByLabelText("KB folder");
    await screen.findByDisplayValue("/Users/me/kb");
    await userEvent.clear(field);
    await userEvent.type(field, "/Users/me/kb2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findAllByText("KB folder saved. 3 pages indexed.")).length).toBeGreaterThan(0);
    const put = calls.find((c) => c.method === "PUT");
    expect(await put?.json()).toEqual({ path: "/Users/me/kb2" });
  });

  it("Resume appears only when paused", async () => {
    renderSettings({}, "#sync");
    expect(await screen.findByTestId("sync-mode")).toHaveTextContent("Commit and push");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeInTheDocument();
    screen.getByRole("heading", { name: "Settings", level: 1 });
  });

  it("paused: Resume resumes", async () => {
    const resumed = syncStatus();
    renderSettings(
      {
        "GET /api/kbsync/status": syncStatus({ paused: true }),
        "POST /api/kbsync/resume": resumed,
      },
      "#sync",
    );
    const buttons = await screen.findAllByRole("button", { name: "Resume" }); // banner + section
    expect(buttons.length).toBe(2);
    await userEvent.click(buttons[1] as HTMLElement);
    expect(screen.queryAllByRole("button", { name: "Resume" })).toHaveLength(0);
  });

  it("sync now shows the last result; init offered when git is off", async () => {
    renderSettings(
      {
        "POST /api/kbsync/run": {
          result: { outcome: "pushed", message: "committed and pushed" },
          status: syncStatus({
            last_run: "2026-10-16T14:05:00+00:00",
            last_result: { outcome: "pushed", message: "committed and pushed" },
          }),
        },
      },
      "#sync",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(await screen.findByTestId("sync-last")).toHaveTextContent("committed and pushed");
  });

  it("offers Initialize git when sync is off, and reports failures", async () => {
    renderSettings(
      {
        "GET /api/kbsync/status": syncStatus(
          {},
          { mode: "off", branch: null, upstream: null, problem: "x", problem_text: "No repo" },
        ),
        "POST /api/kbsync/init": () =>
          Response.json({ detail: "the KB folder is already a git repository" }, { status: 400 }),
      },
      "#sync",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Initialize git" }));
    expect((await screen.findAllByText("the KB folder is already a git repository")).length).toBeGreaterThan(0);
    expect(screen.getByText("No repo")).toBeInTheDocument();
  });

  it("lists runners, conflicts, and rotates the token after confirming", async () => {
    renderSettings(
      {
        "GET /api/agents/runners": {
          "claude-code": { available: true, version: "2.1.301", detail: "ok" },
          codex: { available: false, version: null, detail: "not logged in: run codex login" },
        },
        "GET /api/kb/conflicts": [
          { kind: "duplicate_id", id: "abc", paths: ["Notes/a.md", "Notes/b.md"], detected_at: "x" },
          { kind: "missing", id: "def", paths: ["Notes/gone.md"], detected_at: "x" },
        ],
        "POST /api/auth/rotate": { ok: true },
      },
      "#agents",
    );
    expect(await screen.findByText("not logged in: run codex login", { selector: "span span" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    const conflicts = await go("Conflicts");
    expect(await conflicts.findByText("2 to review")).toBeInTheDocument();
    expect(conflicts.getByText("Notes/gone.md")).toBeInTheDocument();
    await go("Security");
    await userEvent.click(screen.getByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token" }));
    expect(await screen.findByText(/Token rotated/)).toBeInTheDocument();
  });

  it("a KB folder load that gets no answer says so, and Try again reloads it", async () => {
    let down = true;
    renderSettings({
      "GET /api/kb/path": () =>
        down
          ? Promise.reject(new TypeError("Failed to fetch"))
          : Response.json({ path: "/Users/me/kb", problem: null, pages: 1 }),
    });
    const kb = within(await screen.findByRole("region", { name: "Knowledge base" }));
    expect(
      await kb.findByText(
        "Couldn't load the KB folder: Failed to fetch. Check that Jesinia is running, then try again.",
      ),
    ).toBeInTheDocument();
    down = false;
    await userEvent.click(kb.getByRole("button", { name: "Try again" }));
    expect(await kb.findByDisplayValue("/Users/me/kb")).toBeInTheDocument();
    expect(kb.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("a KB folder save that gets no answer says so and can be sent again", async () => {
    renderSettings({ "PUT /api/kb/path": () => Promise.reject(new TypeError("Failed to fetch")) });
    await screen.findByDisplayValue("/Users/me/kb");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "Couldn't save the KB folder: Failed to fetch. Check that Jesinia is running, then try again.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).not.toHaveAttribute("aria-busy");
  });

  it("a sync action that gets no answer says so and frees its button", async () => {
    renderSettings({ "POST /api/kbsync/run": () => Promise.reject(new TypeError("Failed to fetch")) }, "#sync");
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(
      (await screen.findAllByText("Couldn't sync: Failed to fetch. Check that Jesinia is running, then try again."))
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Sync now" })).not.toHaveAttribute("aria-busy");
  });

  it("shows the KB problem the server reports, and clears it after a successful save", async () => {
    let configured = false;
    renderSettings({
      "GET /api/kb/path": () =>
        Response.json(
          configured
            ? { path: "/Users/me/kb", problem: null, pages: 1 }
            : { path: null, problem: "no KB path configured", pages: 0 },
        ),
      "PUT /api/kb/path": () => {
        configured = true;
        return Response.json({ path: "/Users/me/kb", indexed: 1 });
      },
    });
    expect(await screen.findByText("no KB path configured")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("KB folder"), "/Users/me/kb");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByText("no KB path configured")).toBeNull());
    expect(screen.getByLabelText("KB folder")).not.toHaveAttribute("aria-invalid");
  });

  it("reports when agents could not be re-registered after rotation", async () => {
    renderSettings(
      {
        "POST /api/auth/rotate": { ok: true, agents_refreshed: false, warning: "Agents were not re-registered." },
      },
      "#security",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token" }));
    expect(await screen.findByText(/Agents were not re-registered/)).toBeInTheDocument();
  });
});
