// SPDX-License-Identifier: AGPL-3.0-or-later
import { APPS, baseRoutes, fakeClient, syncStatus } from "@jesinia/shell";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { clock, greeting } from "../format";

describe("Launcher", () => {
  it("shows tiles, KB, sync and agents; Sync now reports the result", async () => {
    const { client } = fakeClient(
      baseRoutes({
        "GET /api/agents/runners": {
          "claude-code": { available: true, version: "2.1.301", detail: "ok" },
          codex: { available: false, version: null, detail: "not logged in" },
        },
        "POST /api/kbsync/run": {
          result: { outcome: "pushed", message: "committed and pushed" },
          status: syncStatus({ last_result: { outcome: "pushed", message: "committed and pushed" } }),
        },
      }),
    );
    render(<App path="/" client={client} />);
    expect(await screen.findByText("12 pages indexed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Notes/ })).toHaveAttribute("href", "/notes/");
    expect(screen.getByRole("link", { name: /RSS/ })).toHaveAttribute("href", "/rss/");
    expect(screen.getByRole("link", { name: /Review/ })).toHaveAttribute("href", "/review/");
    expect(screen.getByText("Issues").closest("[aria-disabled]")).not.toBeNull();
    expect(await screen.findByText("12 pages indexed")).toBeInTheDocument();
    expect(await screen.findByText("not logged in")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByTestId("last-result")).toHaveTextContent("committed and pushed");
  });

  it("KB missing: tiles are inert and Settings is offered", async () => {
    const { client } = fakeClient(
      baseRoutes({
        "GET /healthz": { ok: false, modules: { kb: { ok: false, detail: "KB folder not found: /x" } } },
        "GET /api/kbsync/status": syncStatus({}, { mode: "off" }),
        "POST /api/kbsync/run": { result: { outcome: "error", message: "boom" }, status: syncStatus() },
      }),
    );
    render(<App path="/" client={client} />);
    expect(await screen.findByText(/Set up your knowledge base/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Notes/ })).toBeNull();
    expect(screen.getAllByRole("link", { name: "Open Settings" })[0]).toHaveAttribute("href", "/settings/#kb");
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull();
  });

  it("a failed sync shows a danger toast; runners without a version still list", async () => {
    const { client } = fakeClient(
      baseRoutes({
        "GET /api/agents/runners": { fake: { available: true, version: null, detail: "ok" } },
        "POST /api/kbsync/run": { result: { outcome: "error", message: "push failed" }, status: syncStatus() },
      }),
    );
    render(<App path="/" client={client} />);
    expect(await screen.findByText("Fake runner")).toBeInTheDocument();
    expect(screen.getByText("available")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    expect((await screen.findAllByText("Sync: push failed")).length).toBeGreaterThan(0);
  });

  it("built apps get a link tile when the KB is healthy", async () => {
    const notes = APPS.find((a) => a.id === "notes");
    if (!notes) throw new Error("no notes app");
    const was = notes.built;
    notes.built = true;
    try {
      const { client } = fakeClient(baseRoutes());
      render(<App path="/" client={client} />);
      expect(await screen.findByRole("link", { name: /Notes/ })).toHaveAttribute("href", "/notes/");
    } finally {
      notes.built = was;
    }
  });

  it("formats times and greetings", () => {
    expect(clock(null)).toBe("—");
    expect(clock("nope")).toBe("—");
    expect(clock("2026-10-16T14:05:00Z")).toMatch(/\d/);
    expect(greeting(new Date(2026, 9, 16, 9))).toBe("Good morning");
    expect(greeting(new Date(2026, 9, 16, 14))).toBe("Good afternoon");
    expect(greeting(new Date(2026, 9, 16, 20))).toBe("Good evening");
  });
});
