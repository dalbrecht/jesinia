// SPDX-License-Identifier: AGPL-3.0-or-later
// Each opened epic gets its own workspace tab (spec §6, PR 4 final review I1): from the Epics
// list, from an issue's "In owner/repo#n", or from its address after a reload, it is always the
// epic layout (ordered children and the brief), never the plain issue view.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { detail, epic, epicDetail, issuesRoutes, json, renderApp, settleDrag } from "./fixtures";

afterEach(async () => {
  await settleDrag();
  localStorage.clear();
});

const EPIC = "/api/issues/epics/acme/app/10";
const routes = (over = {}) =>
  issuesRoutes({
    "GET /api/issues/epics": { epics: [epic(10, { title: "Onboarding" })] },
    [`GET ${EPIC}`]: () => json(epicDetail(10, { epic: epic(10, { title: "Onboarding" }) })),
    ...over,
  });
const workspace = () => screen.getByRole("navigation", { name: "Workspace" });

describe("an epic's own tab", () => {
  it("opens from the Epics list as a routable workspace tab with the epic layout", async () => {
    renderApp(routes(), "/issues/epics");
    await userEvent.click(await screen.findByRole("button", { name: "Onboarding" }));
    expect(await screen.findByRole("region", { name: "Children" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/issues/epics/acme/app/10"));
    const tab = within(workspace()).getByRole("link", { name: /acme\/app#10/ });
    expect(tab).toHaveAttribute("aria-current", "page");
    expect(within(workspace()).getByRole("button", { name: "Close acme/app#10" })).toBeInTheDocument();
  });

  it("comes back as the epic layout after a reload of its address", async () => {
    renderApp(routes(), "/issues/epics/acme/app/10");
    expect(await screen.findByRole("region", { name: "Children" })).toBeInTheDocument();
    expect(within(workspace()).getByRole("link", { name: /acme\/app#10/ })).toHaveAttribute("aria-current", "page");
  });

  it("opens from an issue's epic link as the epic layout, not the issue view", async () => {
    renderApp(
      routes({
        "GET /api/issues/item/acme/app/1": detail({ parent: "acme/app#10" }),
        "POST /api/issues/item/acme/app/1/refresh": detail({ parent: "acme/app#10" }),
      }),
      "/issues/acme/app/1",
    );
    const section = await screen.findByRole("region", { name: "Epic" });
    await userEvent.click(within(section).getByRole("button", { name: "acme/app#10" }));
    expect(await screen.findByRole("region", { name: "Children" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/issues/epics/acme/app/10"));
  });

  it("goes back to the Epics list from All epics", async () => {
    renderApp(routes(), "/issues/epics/acme/app/10");
    await screen.findByRole("region", { name: "Children" });
    await userEvent.click(screen.getByRole("button", { name: "← All epics" }));
    await waitFor(() => expect(window.location.pathname).toBe("/issues/epics"));
  });
});
