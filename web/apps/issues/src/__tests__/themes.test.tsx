// SPDX-License-Identifier: AGPL-3.0-or-later
// Themes beside the list (spec §6.1, rulings IR22, IR56, IR59): Add theme… (an existing or a new
// tag), the filter, Publish (staged in the tray), and To epic as a draft diff to approve, with
// the closed-epic refusal offering a new epic (`new_epic=true`).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { batch, callsTo, issuesRoutes, json, pending, renderApp, ROW_1, ROW_2, theme, toast, view } from "./fixtures";

const bodies = async (calls: Request[]) => Promise.all(calls.map((c) => c.clone().json() as Promise<unknown>));
const column = () => screen.getByRole("complementary", { name: "Themes" });
const THEME = { tag: "onboarding", issues: [{ repo: "acme/app", n: 1, title: "Fix login", state: "open", url: null }] };
const PLAN = (over = {}) => ({
  tag: "onboarding",
  repo: "acme/app",
  epic: { kind: "create", title: "Onboarding", body: "" },
  digest: "b".repeat(64),
  children: [{ item: "acme/app#1", repo: "acme/app", n: 1, temp_id: null, title: "Fix login", is_new: false, warning: null }], // prettier-ignore
  skipped: [],
  ...over,
});

function routes(over = {}) {
  return issuesRoutes({
    "GET /api/issues/inbox": view([ROW_1, ROW_2]),
    "GET /api/issues/themes": {
      themes: [theme("onboarding", { count: 1 }), theme("x".repeat(3), { publishable: false })],
    },
    "GET /api/issues/themes/onboarding": THEME,
    ...over,
  });
}

describe("adding a theme", () => {
  it("Add theme… adds an existing tag or a new one to the selected issues; one they all had says so", async () => {
    let had = false;
    const { calls } = renderApp(
      routes({
        "POST /api/issues/themes/onboarding/issues": () => json({ tag: "onboarding", added: had ? [] : [{ repo: "acme/app", n: 1 }] }, 201), // prettier-ignore
        "POST /api/issues/themes/sync/issues": () => json({ error: "ThemeNearDuplicateError", detail: "near #sync-reliability" }, 409), // prettier-ignore
      }),
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add a theme to the selected issues" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "#onboarding" }));
    expect(await toast(/Added #onboarding to acme\/app#1/)).toBe(true);
    had = true;
    await userEvent.click(screen.getByRole("button", { name: "Add a theme to the selected issues" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "#onboarding" }));
    expect(await toast(/acme\/app#1 had #onboarding already/)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Add a theme to the selected issues" }));
    await userEvent.click(await screen.findByRole("textbox", { name: "Name a new theme" }));
    await userEvent.paste("#sync");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await toast(/Couldn't add #sync: That's very close to a tag you already have/)).toBe(true);
    expect(callsTo(calls, "POST", "/api/issues/themes/sync/issues")).toHaveLength(1);
  });

  it("the column's new-theme field adds to the selection; an Undo that fails says so", async () => {
    const { calls } = renderApp(
      routes({
        "POST /api/issues/themes/polish/issues": () =>
          json({ tag: "polish", added: [{ repo: "acme/app", n: 2 }] }, 201),
        "POST /api/issues/themes/polish/issues/remove": () => json({ error: "X", detail: "down" }, 503),
      }),
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select acme/app#2" }));
    await userEvent.click(within(column()).getByRole("textbox", { name: "New theme" }));
    await userEvent.paste("polish");
    await userEvent.click(within(column()).getByRole("button", { name: "Add to selected" }));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/themes/polish/issues"))).toEqual([{ issues: [{ repo: "acme/app", n: 2 }] }]); // prettier-ignore
    await userEvent.click(await screen.findByRole("button", { name: "Undo" }));
    expect(await toast(/Couldn't undo the theme: down/)).toBe(true);
  });

  it("a theme filters the list, and Show all clears it; a long tag stays local", async () => {
    renderApp(routes());
    const filter = await within(await screen.findByRole("complementary", { name: "Themes" })).findByRole("button", { name: "Filter by #onboarding" }); // prettier-ignore
    expect(screen.getByText(/Too long for a GitHub label/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish #xxx as a label" })).toBeDisabled();
    await screen.findByText("Add export");
    await userEvent.click(filter);
    await waitFor(() => expect(screen.queryByText("Add export")).toBeNull());
    expect(screen.getByText("Showing the issues with #onboarding")).toBeInTheDocument();
    await userEvent.click(filter);
    expect(await screen.findByText("Add export")).toBeInTheDocument();
    await userEvent.click(filter);
    await userEvent.click(await screen.findByRole("button", { name: "Show all" }));
    expect(await screen.findByText("Add export")).toBeInTheDocument();
  });

  it("themes that can't be read, or a filter that can't, say why", async () => {
    renderApp(
      routes({
        "GET /api/issues/themes": { themes: [theme("onboarding")] },
        "GET /api/issues/themes/onboarding": () => json({ error: "ThemeNotFoundError", detail: "no such theme" }, 404),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Filter by #onboarding" }));
    expect(await toast(/Couldn't filter by #onboarding: no such theme/)).toBe(true);
  });
});

describe("publishing and To epic", () => {
  it("Publish stages the label in the tray; a repo with nothing to publish says so", async () => {
    let nothing = false;
    const ensure = pending("e1", { n: null, change: { kind: "ensure_label", name: "theme:onboarding", repo: "acme/app", color: "ededed", description: "" }, source: "theme" }); // prettier-ignore
    const added = pending("a1", { change: { kind: "add_label", name: "theme:onboarding" }, source: "theme" });
    renderApp(
      routes({
        "GET /api/issues/pending": () => json({ changes: nothing ? [ensure, added] : [] }),
        "POST /api/issues/themes/onboarding/publish": () => {
          if (nothing) return json({ error: "NothingToPublishError", detail: "all labelled" }, 409);
          nothing = true;
          return json({ tag: "onboarding", repo: "acme/app", label: "theme:onboarding", ensure, added: [added], already: [] }, 201); // prettier-ignore
        },
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Publish #onboarding as a label" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    expect(await toast(/Staged the label theme:onboarding for 1 issue in acme\/app/)).toBe(true);
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(within(tray).getByText("Create label theme:onboarding")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Publish #onboarding as a label" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    expect(await toast(/Every issue of the theme in that repo has its label already/)).toBe(true);
  });

  it("To epic previews the plan as a diff and approves it as one batch, followed in the tray", async () => {
    let earlier = true;
    const { calls } = renderApp(
      routes({
        "GET /api/issues/themes/onboarding/epic": () => json(PLAN({ skipped: [{ item: "other/x#2", reason: "another owner" }] })), // prettier-ignore
        "GET /api/issues/pending/b4": batch("b4", { origin: "epic", status: "interrupted" }),
        "POST /api/issues/themes/onboarding/epic": () =>
          earlier
            ? json({ error: "EarlierBatchError", detail: "retry batch b4, or abandon it", batch_id: "b4" }, 409)
            : json(batch("b5", { origin: "epic" }), 202),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Turn #onboarding into an epic" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    const plan = await screen.findByRole("region", { name: "#onboarding to an epic in acme/app" });
    expect(plan).toHaveTextContent("Create the epic Onboarding");
    expect(plan).toHaveTextContent("Link acme/app#1 Fix login");
    expect(plan).toHaveTextContent("Skipped other/x#2: another owner");
    await userEvent.click(within(plan).getByRole("button", { name: "Approve" }));
    expect(await within(plan).findByRole("alert")).toHaveTextContent(/retry batch b4/);
    const tray = await screen.findByRole("region", { name: "Pending changes" });
    expect(await within(tray).findByText("Earlier batch: retry or abandon it first")).toBeInTheDocument();
    earlier = false;
    await userEvent.click(within(plan).getByRole("button", { name: "Approve" }));
    expect(await within(tray).findByText("Theme #onboarding to an epic in acme/app")).toBeInTheDocument();
    expect(await bodies(callsTo(calls, "POST", "/api/issues/themes/onboarding/epic"))).toEqual([
      { repo: "acme/app", digest: PLAN().digest, new_epic: false },
      { repo: "acme/app", digest: PLAN().digest, new_epic: false },
    ]);
  });

  it("a closed theme epic offers to reopen it on GitHub or ask for a new epic (new_epic=true)", async () => {
    const { calls } = renderApp(
      routes({
        "GET /api/issues/themes/onboarding/epic": (req: Request) =>
          new URL(req.url).searchParams.get("new_epic") === "true"
            ? json(PLAN())
            : json({ error: "ThemeEpicClosedError", detail: "the theme #onboarding's epic acme/app#40 is closed; reopen it or ask for a new epic" }, 409), // prettier-ignore
        "POST /api/issues/themes/onboarding/epic": () => json(batch("b6", { origin: "epic" }), 202),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Turn #onboarding into an epic" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    const refused = await screen.findByRole("region", { name: "#onboarding to an epic" });
    expect(refused).toHaveTextContent(/The theme's epic is closed: reopen it on GitHub, or ask for a new epic/);
    expect(within(refused).getByRole("link", { name: "Reopen acme/app#40 on GitHub" })).toHaveAttribute("href", "https://github.com/acme/app/issues/40"); // prettier-ignore
    await userEvent.click(within(refused).getByRole("button", { name: "Ask for a new epic" }));
    const plan = await screen.findByRole("region", { name: "#onboarding to an epic in acme/app" });
    await userEvent.click(within(plan).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/issues/themes/onboarding/epic")).toHaveLength(1));
    expect(await bodies(callsTo(calls, "POST", "/api/issues/themes/onboarding/epic"))).toEqual([{ repo: "acme/app", digest: PLAN().digest, new_epic: true }]); // prettier-ignore
  });

  it("an epic closed between preview and approval keeps the plan with the same choices; Cancel and Close dismiss", async () => {
    renderApp(
      routes({
        "GET /api/issues/themes/onboarding/epic": () => json(PLAN({ epic: { kind: "existing", repo: "acme/app", n: 40 }, children: [] })), // prettier-ignore
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Turn #onboarding into an epic" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    const plan = await screen.findByRole("region", { name: "#onboarding to an epic in acme/app" });
    expect(plan).toHaveTextContent("Link under the theme's epic acme/app#40");
    expect(plan).toHaveTextContent("Nothing to link");
    expect(within(plan).getByRole("button", { name: "Approve" })).toBeDisabled();
    await userEvent.click(within(plan).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("region", { name: "#onboarding to an epic in acme/app" })).toBeNull();
  });

  it("a refused approval of a closed epic keeps the plan and offers the new epic", async () => {
    renderApp(
      routes({
        "GET /api/issues/themes/onboarding/epic": () => json(PLAN()),
        "POST /api/issues/themes/onboarding/epic": () =>
          json({ error: "ThemeEpicClosedError", detail: "the theme #onboarding's epic acme/app#40 is closed" }, 409),
      }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Turn #onboarding into an epic" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    const plan = await screen.findByRole("region", { name: "#onboarding to an epic in acme/app" });
    await userEvent.click(within(plan).getByRole("button", { name: "Approve" }));
    expect(await within(plan).findByRole("link", { name: "Reopen acme/app#40 on GitHub" })).toBeInTheDocument();
    expect(within(plan).getByRole("button", { name: "Ask for a new epic" })).toBeInTheDocument();
  });

  it("a plan that can't be read for another reason only offers Close", async () => {
    renderApp(routes({ "GET /api/issues/themes/onboarding/epic": () => json({ error: "X", detail: "no children" }, 409) })); // prettier-ignore
    await userEvent.click(await screen.findByRole("button", { name: "Turn #onboarding into an epic" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "acme/app" }));
    const refused = await screen.findByRole("region", { name: "#onboarding to an epic" });
    expect(within(refused).queryByRole("button", { name: "Ask for a new epic" })).toBeNull();
    await userEvent.click(within(refused).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("region", { name: "#onboarding to an epic" })).toBeNull();
  });
});
