// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, fakeClient, syncStatus } from "@jesinia/shell";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";

function renderSettings(over = {}) {
  const { client, calls } = fakeClient(baseRoutes(over));
  render(<App path="/settings/" client={client} />);
  return calls;
}

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
    renderSettings();
    expect(await screen.findByTestId("sync-mode")).toHaveTextContent("Commit and push");
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeInTheDocument();
    screen.getByRole("heading", { name: "Settings", level: 1 });
  });

  it("paused: Resume resumes", async () => {
    const resumed = syncStatus();
    renderSettings({
      "GET /api/kbsync/status": syncStatus({ paused: true }),
      "POST /api/kbsync/resume": resumed,
    });
    const buttons = await screen.findAllByRole("button", { name: "Resume" }); // banner + section
    expect(buttons.length).toBe(2);
    await userEvent.click(buttons[1] as HTMLElement);
    expect(screen.queryAllByRole("button", { name: "Resume" })).toHaveLength(0);
  });

  it("sync now shows the last result; init offered when git is off", async () => {
    renderSettings({
      "POST /api/kbsync/run": {
        result: { outcome: "pushed", message: "committed and pushed" },
        status: syncStatus({
          last_run: "2026-10-16T14:05:00+00:00",
          last_result: { outcome: "pushed", message: "committed and pushed" },
        }),
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(await screen.findByTestId("sync-last")).toHaveTextContent("committed and pushed");
  });

  it("offers Initialize git when sync is off, and reports failures", async () => {
    renderSettings({
      "GET /api/kbsync/status": syncStatus(
        {},
        { mode: "off", branch: null, upstream: null, problem: "x", problem_text: "No repo" },
      ),
      "POST /api/kbsync/init": () =>
        Response.json({ detail: "the KB folder is already a git repository" }, { status: 400 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Initialize git" }));
    expect((await screen.findAllByText("the KB folder is already a git repository")).length).toBeGreaterThan(0);
    expect(screen.getByText("No repo")).toBeInTheDocument();
  });

  it("lists runners, conflicts, and rotates the token after confirming", async () => {
    renderSettings({
      "GET /api/agents/runners": {
        "claude-code": { available: true, version: "2.1.301", detail: "ok" },
        codex: { available: false, version: null, detail: "not logged in: run codex login" },
      },
      "GET /api/kb/conflicts": [
        { kind: "duplicate_id", id: "abc", paths: ["Notes/a.md", "Notes/b.md"], detected_at: "x" },
        { kind: "missing", id: "def", paths: ["Notes/gone.md"], detected_at: "x" },
      ],
      "POST /api/auth/rotate": { ok: true },
    });
    expect(await screen.findByText("not logged in: run codex login", { selector: "span span" })).toBeInTheDocument();
    expect(screen.getByText("2 to review")).toBeInTheDocument();
    expect(screen.getByText("Notes/gone.md")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token" }));
    expect(await screen.findByText(/Token rotated/)).toBeInTheDocument();
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
    renderSettings({
      "POST /api/auth/rotate": { ok: true, agents_refreshed: false, warning: "Agents were not re-registered." },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Rotate token…" }));
    await userEvent.click(screen.getByRole("button", { name: "Rotate token" }));
    expect(await screen.findByText(/Agents were not re-registered/)).toBeInTheDocument();
  });
});
