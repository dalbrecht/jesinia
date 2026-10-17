// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ClientProvider, fakeClient, TagPicker, type Routes, type TagPickerHandle } from "../index";

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
  function Host({ subject, context }: { subject: string; context: string }) {
    const [tags, setTags] = useState<string[]>(initial);
    return (
      <TagPicker
        text={subject}
        context={context}
        value={tags}
        onChange={(next) => {
          changes(next);
          setTags(next);
        }}
      />
    );
  }
  const view = (subject: string, context: string) => (
    <ClientProvider client={env.client}>
      <Host subject={subject} context={context} />
    </ClientProvider>
  );
  const { rerender, unmount } = render(view(text, "page-1"));
  return {
    ...env,
    changes,
    unmount,
    rerender: (subject: string, context = "page-1") => rerender(view(subject, context)),
  };
}

/** A response the test releases by hand, to act while the request is in flight. */
function deferred() {
  let release: (response: Response) => void = () => {};
  const response = new Promise<Response>((resolve) => {
    release = resolve;
  });
  return { route: () => response, release };
}

const suggestNow = () => userEvent.click(screen.getByRole("button", { name: "Suggest tags" }));
const suggests = (calls: Request[]) => calls.filter((c) => c.url.includes("/api/kb/tags/suggest"));

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
    await suggestNow();
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

  it("keeps focus inside the picker after a suggestion is picked", async () => {
    setup({
      "POST /api/kb/tags/suggest": [
        { tag: "alpha", new: false },
        { tag: "beta", new: false },
      ],
    });
    await suggestNow();
    await userEvent.click(await screen.findByRole("button", { name: "#alpha" }));
    expect(screen.getByRole("button", { name: "#beta" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "#beta" }));
    expect(screen.getByRole("textbox", { name: "Add a tag" })).toHaveFocus();
  });

  it("focuses the near-match offer when a picked new suggestion has one", async () => {
    setup({
      "POST /api/kb/tags/suggest": [{ tag: "sqlites", new: true }],
      "GET /api/kb/tags/check": check({ sqlites: { normalised: "sqlites", ...NOT_FOUND, near: ["sqlite"] } }),
    });
    await suggestNow();
    await userEvent.click(await screen.findByRole("button", { name: "#sqlites new" }));
    expect(await screen.findByRole("button", { name: "Use #sqlite" })).toHaveFocus();
  });

  it("keeps focus inside the picker after a chip is removed", async () => {
    setup({}, { initial: ["one", "two"] });
    await userEvent.click(screen.getByRole("button", { name: "Remove #one" }));
    expect(screen.getByRole("button", { name: "Remove #two" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Remove #two" }));
    expect(screen.getByRole("textbox", { name: "Add a tag" })).toHaveFocus();
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
    await suggestNow();
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

  it("shows the cause and clears loading when the check request is rejected", async () => {
    const { changes } = setup({
      "GET /api/kb/tags/check": () => Promise.reject(new Error("network down")),
    });
    const box = screen.getByRole("textbox", { name: "Add a tag" });
    await userEvent.type(box, "wal{Enter}");
    expect(await screen.findByText(/network down/)).toBeInTheDocument();
    expect(changes).not.toHaveBeenCalled();
    expect(box).toHaveValue("wal");
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
  });

  it("shows the server's message when suggestions fail with an error response", async () => {
    setup({ "POST /api/kb/tags/suggest": () => Response.json({ detail: "Agent unavailable." }, { status: 503 }) });
    await suggestNow();
    expect(await screen.findByText("Agent unavailable.")).toBeInTheDocument();
  });

  it("clears the suggesting state when the suggest request is rejected", async () => {
    setup({ "POST /api/kb/tags/suggest": () => Promise.reject(new Error("offline")) });
    await suggestNow();
    expect(await screen.findByText(/offline/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Suggest tags" })).toBeEnabled();
  });

  it("asks for no suggestions without text", async () => {
    const { calls } = setup({}, { text: "  " });
    expect(screen.getByRole("textbox", { name: "Add a tag" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Suggest tags" })).toBeDisabled();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toHaveLength(0);
    expect(screen.queryByRole("list", { name: "Tags" })).not.toBeInTheDocument();
  });

  it("asks for suggestions only when the user does, and drops them when the text changes", async () => {
    const { calls, rerender } = setup({ "POST /api/kb/tags/suggest": [{ tag: "sqlite", new: false }] });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggests(calls)).toHaveLength(0); // a new reply alone never runs the advisor
    rerender("A later reply.");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggests(calls)).toHaveLength(0);
    await suggestNow();
    expect(await screen.findByRole("button", { name: "#sqlite" })).toBeInTheDocument();
    expect(await suggests(calls)[0]?.json()).toEqual({ text: "A later reply.", limit: 5 });
    rerender("Another reply.");
    expect(screen.queryByRole("group", { name: "Suggested tags" })).not.toBeInTheDocument();
    expect(suggests(calls)).toHaveLength(1);
  });

  it("drops suggestions that arrive after the text changed", async () => {
    const slow = deferred();
    const { rerender } = setup({ "POST /api/kb/tags/suggest": slow.route });
    await suggestNow();
    rerender("Another reply.");
    slow.release(Response.json([{ tag: "stale", new: false }]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("button", { name: "#stale" })).not.toBeInTheDocument();
  });

  it("adds nothing when the context switches before the check answers", async () => {
    const slow = deferred();
    const { changes, rerender } = setup({ "GET /api/kb/tags/check": slow.route });
    await userEvent.type(screen.getByRole("textbox", { name: "Add a tag" }), "wal-mode{Enter}");
    rerender("Notes on another page.", "page-2");
    slow.release(Response.json({ normalised: "wal-mode", ...NOT_FOUND }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(changes).not.toHaveBeenCalled();
    expect(screen.queryByRole("list", { name: "Tags" })).not.toBeInTheDocument();
  });

  it("adds nothing when it unmounts before the check answers", async () => {
    const slow = deferred();
    const { changes, unmount } = setup({ "GET /api/kb/tags/check": slow.route });
    await userEvent.type(screen.getByRole("textbox", { name: "Add a tag" }), "wal-mode{Enter}");
    unmount();
    slow.release(Response.json({ normalised: "wal-mode", ...NOT_FOUND }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(changes).not.toHaveBeenCalled();
  });

  it("still adds a checked tag when only the text changed meanwhile", async () => {
    const slow = deferred();
    const { changes, rerender } = setup({ "GET /api/kb/tags/check": slow.route });
    await userEvent.type(screen.getByRole("textbox", { name: "Add a tag" }), "wal-mode{Enter}");
    rerender("A newer reply on the same page.");
    slow.release(Response.json({ normalised: "wal-mode", ...NOT_FOUND }));
    await waitFor(() => expect(changes).toHaveBeenLastCalledWith(["wal-mode"]));
  });

  it("suggestOnOpen asks once as it opens (Keep), never again on its own", async () => {
    const env = fakeClient({ "POST /api/kb/tags/suggest": [{ tag: "sqlite", new: false }] });
    const view = (text: string) => (
      <ClientProvider client={env.client}>
        <TagPicker text={text} value={[]} onChange={() => {}} suggestOnOpen />
      </ClientProvider>
    );
    const { rerender } = render(view("Post about SQLite."));
    expect(await screen.findByRole("button", { name: "#sqlite" })).toBeInTheDocument();
    expect(await suggests(env.calls)[0]?.json()).toEqual({ text: "Post about SQLite.", limit: 5 });
    rerender(view("Post about SQLite, with a note."));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(suggests(env.calls)).toHaveLength(1);
  });

  it("commitDraft adds the typed tag for a caller about to save, or stops on a near-match", async () => {
    const env = fakeClient({
      "GET /api/kb/tags/check": check({
        "wal-mode": { normalised: "wal-mode", ...NOT_FOUND },
        sqllite: { normalised: "sqllite", ...NOT_FOUND, near: ["sqlite"] },
        sqlite3: { normalised: "sqlite3", exists: true, canonical: "sqlite", near: [], same_meaning: null },
      }),
    });
    const picker = createRef<TagPickerHandle>();
    function Host() {
      const [tags, setTags] = useState<string[]>(["db"]);
      return (
        <ClientProvider client={env.client}>
          <TagPicker ref={picker} text="x" value={tags} onChange={setTags} />
        </ClientProvider>
      );
    }
    render(<Host />);
    const commit = async () => {
      let out: string[] | null = null;
      await act(async () => {
        out = (await picker.current?.commitDraft()) ?? null;
      });
      return out;
    };
    expect(await commit()).toEqual(["db"]); // nothing typed: the tags as they are
    const box = screen.getByRole("textbox", { name: "Add a tag" });
    await userEvent.type(box, "wal-mode");
    expect(await commit()).toEqual(["db", "wal-mode"]);
    await userEvent.type(box, "sqlite3");
    expect(await commit()).toEqual(["db", "wal-mode", "sqlite"]);
    await userEvent.type(box, "sqllite");
    expect(await commit()).toBeNull(); // the author decides on a near-match first
    expect(screen.getByText("#sqllite looks like an existing tag. Use #sqlite?")).toBeInTheDocument();
    await userEvent.type(box, "bad one");
    expect(await commit()).toBeNull(); // and on an invalid tag
  });
});
