// SPDX-License-Identifier: AGPL-3.0-or-later
// The menu equivalents of drops are reachable by keyboard (spec §6.1): arrows move between
// choices, Escape closes and gives focus back, a refused choice can't be picked. Plus the pure
// helpers behind epics, queues and themes, and how an ask that fails reads.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionMenu } from "../ActionMenu";
import {
  containmentRefusal,
  describeSuggestion,
  groupsScope,
  issueSplitScope,
  readinessText,
  refInCause,
  setParentChange,
  splitScope,
} from "../api";
import { askStatus, ASK_POLL_MS } from "../drafts";
import {
  callsTo,
  epic,
  issuesRoutes,
  json,
  readiness,
  renderApp,
  suggestion,
  suggestions,
  tick,
  toast,
} from "./fixtures";

afterEach(() => {
  vi.useRealTimers();
});

describe("ActionMenu", () => {
  it("opens on its button, moves with the arrows, closes on Escape with focus back", async () => {
    const onPick = vi.fn();
    render(
      <ActionMenu
        label="Move to epic…"
        name="Move it to an epic"
        options={[
          { id: "a", label: "Epic A" },
          { id: "b", label: "Epic B", refusal: "An epic can't contain itself" },
          { id: "c", label: "Epic C", hint: "won't fit one PR" },
        ]}
        onPick={onPick}
      />,
    );
    const button = screen.getByRole("button", { name: "Move it to an epic" });
    await userEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    const items = screen.getAllByRole("menuitem");
    await waitFor(() => expect(items[0]).toHaveFocus());
    await userEvent.keyboard("{ArrowDown}");
    expect(items[1]).toHaveFocus();
    expect(items[1]).toHaveTextContent("Can't: An epic can't contain itself");
    await userEvent.keyboard("{Enter}");
    expect(onPick).not.toHaveBeenCalled();
    await userEvent.keyboard("{ArrowUp}{ArrowUp}");
    expect(items[2]).toHaveFocus();
    expect(items[2]).toHaveTextContent("won't fit one PR");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(button).toHaveFocus();
    await userEvent.click(button);
    await userEvent.keyboard("{Tab}");
    await userEvent.click(screen.getAllByRole("menuitem")[2] as HTMLElement);
    expect(onPick).toHaveBeenCalledWith("c");
    await userEvent.click(button);
    await userEvent.click(button);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("says when there is nothing to pick, and ignores other keys", async () => {
    render(<ActionMenu label="Add to queue…" name="Add it" options={[]} onPick={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Add it" }));
    expect(screen.getByText("Nothing to pick.")).toBeInTheDocument();
    await userEvent.keyboard("{ArrowDown}a");
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("closes when focus leaves it", async () => {
    render(
      <>
        <ActionMenu label="Pick" name="Pick one" options={[{ id: "a", label: "A" }]} onPick={() => {}} />
        <button type="button">Elsewhere</button>
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Pick one" }));
    await userEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("ActionMenu while its parent re-renders, and mouse picks (review I1, I2)", () => {
  it("an open menu neither reloads its choices nor moves focus when the parent re-renders", async () => {
    const load = vi.fn(() =>
      Promise.resolve([
        { id: "a", label: "A" },
        { id: "b", label: "B" },
      ]),
    );
    const menu = () => <ActionMenu label="Pick" name="Pick one" options={() => load()} onPick={() => {}} />;
    const { rerender } = render(menu());
    await userEvent.click(screen.getByRole("button", { name: "Pick one" }));
    await waitFor(() => expect(screen.getAllByRole("menuitem")[0]).toHaveFocus());
    await userEvent.keyboard("{ArrowDown}");
    rerender(menu()); // a fresh options function, as every caller passes
    await waitFor(() => expect(screen.getAllByRole("menuitem")).toHaveLength(2));
    expect(load).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole("menuitem")[1]).toHaveFocus();
    // Opening it again does load afresh.
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Pick one" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  });

  it("a blur with no next focus (Safari's mouse-down on a button) keeps it open so the click picks", async () => {
    const onPick = vi.fn();
    render(<ActionMenu label="Pick" name="Pick one" options={[{ id: "a", label: "A" }, { id: "b", label: "B" }]} onPick={onPick} />); // prettier-ignore
    await userEvent.click(screen.getByRole("button", { name: "Pick one" }));
    const second = screen.getAllByRole("menuitem")[1] as HTMLElement;
    fireEvent.pointerDown(second);
    fireEvent.blur(screen.getAllByRole("menuitem")[0] as HTMLElement, { relatedTarget: null });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.click(second);
    expect(onPick).toHaveBeenCalledWith("b");
  });

  it("a pointer-down outside closes it without taking focus back", async () => {
    render(
      <>
        <ActionMenu label="Pick" name="Pick one" options={[{ id: "a", label: "A" }]} onPick={() => {}} />
        <p>Outside</p>
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Pick one" }));
    fireEvent.pointerDown(screen.getByText("Outside"));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("helpers", () => {
  it("reads containment, refs in causes, readiness, scopes and staged parent links", () => {
    const parentOf = (ref: string) => ({ "acme/app#3": "acme/app#2", "acme/app#2": "acme/app#3" })[ref] ?? null;
    const target = { repo: "acme/app", number: 3, is_initiative: false };
    expect(containmentRefusal(target, { repo: "acme/app", number: 9 }, parentOf)).toBeNull(); // a loop in the cache ends
    expect(containmentRefusal(target, { repo: "acme/app", number: 2 }, parentOf)?.code).toBe("cycle");
    expect(refInCause("no ref here")).toBeNull();
    expect(readinessText(readiness())).toBe("Not ready");
    expect(groupsScope("Acme/App")).toBe("groups:acme/app");
    expect(issueSplitScope("Acme/App", 3)).toBe("split:acme/app#3");
    expect(splitScope(["q2", "q1", "q2"])).toBe("queue_split:q1,q2");
    expect(setParentChange(null, "acme/app#4")).toEqual({ kind: "set_parent", parent: null, expects_parent: true, expected_parent: { repo: "acme/app", n: 4 } }); // prettier-ignore
    expect(setParentChange({ repo: "acme/app", n: 4 }, "not a ref")).toMatchObject({ expected_parent: null });
  });

  it("describes every proposal kind in Jesinia's words", () => {
    const of = (kind: string, payload: Record<string, unknown> = {}) =>
      describeSuggestion(suggestion("x", { kind: kind as never, payload: payload as never }));
    expect(of("group", { epic: { repo: "a/b", n: 1 }, issues: [{}, {}] })).toBe("Add 2 issues to a/b#1");
    expect(of("group", {})).toBe("A new epic for 0 issues");
    expect(of("split", {})).toBe("Split undefined#undefined into 0 parts");
    expect(of("brief")).toBe("A delivery brief");
    expect(of("queue_next")).toBe("Items to add next");
    expect(of("queue_order")).toBe("A new order");
    expect(of("queue_split")).toBe("Which queue each item fits");
  });

  it("says how an ask ended", () => {
    const run = { scope: "s", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null };
    expect(askStatus(null)).toBeNull();
    expect(askStatus({ ...run, status: "failed" })).toBe("The ask failed: no cause given");
    expect(askStatus({ ...run, status: "done" })).toBe("Claude proposed nothing this time.");
    expect(askStatus({ ...run, status: "done", suggestion_id: "x" })).toBeNull();
  });
});

describe("asks that go wrong", () => {
  it("an ask that is refused says so; a poll that fails is read again; drafts that can't be read say so", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let refuse = true;
    let polls = 0;
    const { calls } = renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [epic(10)] },
        "POST /api/issues/epics/proposals": () =>
          refuse
            ? json({ error: "SuggestionRunnerUnavailableError", detail: "no runner" }, 503)
            : json({ scope: "groups:*", status: "running", cause: null, updated_at: "2026-10-18T12:00:00Z", suggestion_id: null }, 202), // prettier-ignore
        "GET /api/issues/suggestions": () => {
          polls += 1;
          if (polls === 1) return json(suggestions([]));
          if (polls === 2) return json({ error: "X", detail: "blip" }, 500);
          return json(suggestions([]));
        },
      }),
      "/issues/epics",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Ask Claude for groups" }));
    expect(await toast(/Couldn't ask Claude: no runner/)).toBe(true);
    refuse = false;
    await userEvent.click(screen.getByRole("button", { name: "Ask Claude for groups" }));
    await tick(ASK_POLL_MS);
    await tick(ASK_POLL_MS);
    await waitFor(() => expect(polls).toBeGreaterThanOrEqual(3));
    expect(await screen.findByText("Claude proposed nothing this time.")).toBeInTheDocument();
    expect(callsTo(calls, "POST", "/api/issues/epics/proposals")).toHaveLength(2);
  });

  it("drafts that can't be read say so", async () => {
    renderApp(
      issuesRoutes({
        "GET /api/issues/epics": { epics: [] },
        "GET /api/issues/suggestions": () => json({ error: "X", detail: "db locked" }, 500),
      }),
      "/issues/epics",
    );
    expect(await toast(/Couldn't read the drafts: db locked/)).toBe(true);
  });
});
