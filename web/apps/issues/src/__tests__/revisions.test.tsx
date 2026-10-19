// SPDX-License-Identifier: AGPL-3.0-or-later
// Kept-alive panes stay current (Codex round 2 on PR #72): an add to a queue, or a theme added
// in one list, bumps that resource's shared revision, and every mounted pane that shows it reads
// it again, so a hidden Queues pane or another list's themes never show what was.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Queue, QueueItem } from "../api";
import { issuesRoutes, json, queue, queueItem, queueSummary, renderApp, ROW_1, theme, view } from "./fixtures";

const workspace = () => screen.getByRole("navigation", { name: "Workspace" });
const tabTo = (name: string) => userEvent.click(within(workspace()).getByRole("link", { name }));

describe("shared revisions", () => {
  it("an add from the Inbox shows in an already-open Queues pane, item and count", async () => {
    let items: QueueItem[] = [];
    const current = (): Queue => queue("q1", items);
    renderApp(
      issuesRoutes({
        "GET /api/issues/queues": () => json({ queues: [queueSummary("q1", { open: items.length })] }),
        "GET /api/issues/queues/q1": () => json(current()),
        "POST /api/issues/queues/q1/items": () => {
          items = [queueItem(1, { title: "Fix login" })];
          return json(current(), 201);
        },
      }),
      "/issues/queues",
    );
    const tabs = await screen.findByRole("tablist", { name: "Queues" });
    expect(within(tabs).getByRole("tab", { name: /Next up/ })).toHaveTextContent("Next up 0");

    await tabTo("Inbox");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add the selected issues to a queue" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Next up" }));

    await tabTo("Queues");
    await waitFor(() => expect(within(tabs).getByRole("tab", { name: /Next up/ })).toHaveTextContent("Next up 1"));
    expect(await screen.findByRole("button", { name: "Move acme/app#1 Fix login" })).toBeInTheDocument();
  });

  it("a theme added in the Inbox shows in an already-open Backlog's column, with its count", async () => {
    let count = 0;
    renderApp(
      issuesRoutes({
        "GET /api/issues/inbox": view([ROW_1]),
        "GET /api/issues/themes": () => json({ themes: count === 0 ? [] : [theme("onboarding", { count })] }),
        "POST /api/issues/themes/onboarding/issues": () => {
          count = 1;
          return json({ tag: "onboarding", added: [{ repo: "acme/app", n: 1 }] }, 201);
        },
      }),
      "/issues/backlog",
    );
    await screen.findByRole("list", { name: "Backlog" });
    await tabTo("Inbox");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add a theme to the selected issues" }));
    await userEvent.click(await screen.findByRole("textbox", { name: "Name a new theme" }));
    await userEvent.paste("onboarding");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await tabTo("Backlog");
    const columns = screen.getAllByRole("complementary", { name: "Themes" }).filter((c) => !c.closest("[hidden]"));
    expect(columns).toHaveLength(1);
    await waitFor(() =>
      expect(within(columns[0] as HTMLElement).getByRole("group", { name: "#onboarding" })).toHaveTextContent("1"),
    );
  });
});
