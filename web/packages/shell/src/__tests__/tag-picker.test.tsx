// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ClientProvider, fakeClient, TagPicker, type Routes } from "../index";

const NOT_FOUND = { exists: false, canonical: null, near: [], same_meaning: null };

function check(answers: Record<string, object>) {
  return (req: Request) => {
    const tag = (new URL(req.url).searchParams.get("tag") ?? "").toLowerCase();
    const answer = answers[tag];
    if (answer === undefined) return Response.json({ detail: `${tag} is not a valid tag` }, { status: 400 });
    return Response.json(answer);
  };
}

function setup(routes: Routes, { text = "Notes about SQLite in WAL mode.", initial = [] as string[] } = {}) {
  const changes = vi.fn();
  const env = fakeClient(routes);
  function Host() {
    const [tags, setTags] = useState<string[]>(initial);
    return (
      <TagPicker
        text={text}
        value={tags}
        onChange={(next) => {
          changes(next);
          setTags(next);
        }}
      />
    );
  }
  render(
    <ClientProvider client={env.client}>
      <Host />
    </ClientProvider>,
  );
  return { ...env, changes };
}

const checks = (calls: Request[]) => calls.filter((c) => c.url.includes("/api/kb/tags/check"));

describe("TagPicker", () => {
  it("renders suggestions with a new badge and adds the one picked", async () => {
    const { calls, changes } = setup({
      "POST /api/kb/tags/suggest": [
        { tag: "sqlite", new: false },
        { tag: "wal-mode", new: true },
      ],
      "GET /api/kb/tags/check": check({ "wal-mode": { normalised: "wal-mode", ...NOT_FOUND } }),
    });
    const known = await screen.findByRole("button", { name: "#sqlite" });
    expect(screen.getByRole("button", { name: "#wal-mode new" })).toBeInTheDocument();
    const suggest = calls.find((c) => c.url.endsWith("/api/kb/tags/suggest"));
    expect(await suggest?.json()).toEqual({ text: "Notes about SQLite in WAL mode.", limit: 5 });
    await userEvent.click(known);
    expect(changes).toHaveBeenLastCalledWith(["sqlite"]);
    expect(checks(calls)).toHaveLength(0); // a catalog tag needs no check
    expect(screen.queryByRole("button", { name: "#sqlite" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "#wal-mode new" }));
    await waitFor(() => expect(changes).toHaveBeenLastCalledWith(["sqlite", "wal-mode"]));
    const chips = screen.getByRole("list", { name: "Tags" });
    expect(chips).toHaveTextContent("#sqlite");
    expect(chips).toHaveTextContent("#wal-modenew");
    await userEvent.click(screen.getByRole("button", { name: "Remove #sqlite" }));
    expect(changes).toHaveBeenLastCalledWith(["wal-mode"]);
  });

  it("offers the existing tag for a near-match, and uses it when accepted", async () => {
    const { changes } = setup({
      "POST /api/kb/tags/suggest": [],
      "GET /api/kb/tags/check": check({
        sqllite: { normalised: "sqllite", ...NOT_FOUND, near: ["sqlite"] },
        db: { normalised: "db", ...NOT_FOUND, same_meaning: "database" },
      }),
    });
    const box = screen.getByRole("textbox", { name: "Add a tag" });
    await userEvent.type(box, "#SQLlite{Enter}");
    expect(await screen.findByText("#sqllite looks like an existing tag. Use #sqlite?")).toBeInTheDocument();
    expect(changes).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Use #sqlite" }));
    expect(changes).toHaveBeenLastCalledWith(["sqlite"]);
    expect(screen.queryByText(/Use #sqlite\?/)).not.toBeInTheDocument();
    await userEvent.type(box, "db");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("#db looks like an existing tag. Use #database?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/Use #database\?/)).not.toBeInTheDocument();
    expect(changes).toHaveBeenCalledTimes(1);
  });

  it("adds catalog tags by their canonical name, new ones with a badge, and reports invalid tags", async () => {
    const { calls, changes } = setup(
      {
        "POST /api/kb/tags/suggest": () => Response.json({ detail: "boom" }, { status: 500 }),
        "GET /api/kb/tags/check": check({
          sqlite3: { normalised: "sqlite3", exists: true, canonical: "sqlite", near: [], same_meaning: null },
          "wal-mode": { normalised: "wal-mode", ...NOT_FOUND },
        }),
      },
      { initial: ["sqlite"] },
    );
    const box = screen.getByRole("textbox", { name: "Add a tag" });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    await userEvent.type(box, "sqlite3{Enter}");
    await waitFor(() => expect(checks(calls)).toHaveLength(1));
    expect(changes).not.toHaveBeenCalled(); // already picked: nothing changes
    expect(box).toHaveValue("");
    await userEvent.type(box, "wal-mode{Enter}");
    await waitFor(() => expect(changes).toHaveBeenLastCalledWith(["sqlite", "wal-mode"]));
    expect(screen.getByRole("list", { name: "Tags" })).toHaveTextContent("#wal-modenew");
    await userEvent.type(box, "no good{Enter}");
    expect(await screen.findByText("no good is not a valid tag")).toBeInTheDocument();
    expect(box).toHaveValue("no good");
    await userEvent.type(box, "!");
    expect(screen.queryByText("no good is not a valid tag")).not.toBeInTheDocument();
    await userEvent.type(box, "   ");
    await userEvent.clear(box);
    await userEvent.type(box, " {Enter}");
    expect(checks(calls)).toHaveLength(3);
    expect(screen.queryByRole("group", { name: "Suggested tags" })).not.toBeInTheDocument();
  });

  it("asks for no suggestions without text", async () => {
    const { calls } = setup({}, { text: "  " });
    expect(screen.getByRole("textbox", { name: "Add a tag" })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);
    expect(screen.queryByRole("list", { name: "Tags" })).not.toBeInTheDocument();
  });
});
