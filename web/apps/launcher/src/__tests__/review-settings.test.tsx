// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Review (plan Task 15, design pass): the pre-review setting and agent, the standards
// folders, watched repos with their per-repo overrides (automated reviewer, pre-review, metrics),
// and "What Jesinia thinks you know", where the author corrects the knowledge model.
import type { components } from "@jesinia/client";
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";

type Settings = components["schemas"]["ReviewSettingsBody"];
type Unit = components["schemas"]["UnitOut"];

const SETTINGS: Settings = {
  prereview: "triage",
  runner: "codex",
  standards_dir: "/Users/me/coding-standards",
  standards_user_dir: null,
  repos: [
    { repo: "acme/api", watched: true, suggested: false, prereview: null, automated_reviewer: null, metrics: null },
    {
      repo: "acme/web",
      watched: true,
      suggested: false,
      prereview: "everything",
      automated_reviewer: "copilot",
      metrics: ["Security", "Tests"],
    },
    { repo: "other/lib", watched: false, suggested: true, prereview: null, automated_reviewer: null, metrics: null },
  ],
};

const unit = (over: Partial<Unit>): Unit => ({
  unit: "repo/acme/web/src",
  kind: "path",
  repo: "acme/web",
  path: "src",
  tag: null,
  familiarity: 0.62,
  override: null,
  override_unit: null,
  exposures: { review_deep: 2, commit: 5 },
  last_exposure: "2026-10-03T12:00:00Z",
  changes_since_last: 120,
  distance: 0,
  ...over,
});

const UNITS: Unit[] = [
  unit({ unit: "repo/acme/web", path: "", familiarity: 1, override: "known", override_unit: "repo/acme/web" }),
  unit({
    unit: "repo/acme/web/src",
    familiarity: 1,
    override: "known",
    override_unit: "repo/acme/web",
  }),
  unit({
    unit: "repo/acme/api/billing",
    repo: "acme/api",
    path: "billing",
    familiarity: 0.3,
    exposures: {},
    last_exposure: null,
    changes_since_last: 0,
    distance: 0.5,
  }),
  unit({ unit: "tag/payments", kind: "tag", repo: null, path: "", tag: "payments", familiarity: 0, override: "unknown", override_unit: "tag/payments", exposures: {} }), // prettier-ignore
];

function renderReview(over: Routes = {}) {
  window.history.replaceState(null, "", "/settings/#review");
  const env = fakeClient(
    baseRoutes({
      "GET /api/review/settings": SETTINGS,
      "GET /api/review/knowledge": { units: UNITS, total: UNITS.length, truncated: false },
      "PUT /api/review/settings": async (req: Request) => Response.json(await req.clone().json()),
      ...over,
    }),
  );
  render(<App path="/settings/" client={env.client} />);
  return env.calls;
}

const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const section = async () => within(await screen.findByRole("region", { name: "Review" }));
/** The section once its settings have loaded (its controls exist only then). */
const loaded = async () => {
  const review = await section();
  await review.findByRole("combobox", { name: "Pre-review" });
  return review;
};
const knowledge = async () => within(await screen.findByRole("region", { name: "What Jesinia thinks you know" }));
const lastPut = async (calls: Request[]) => {
  const puts = sent(calls, "PUT", "/api/review/settings");
  const put = puts[puts.length - 1] as Request;
  expect(put.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  return (await put.clone().json()) as Settings;
};
const toast = async (text: string | RegExp) =>
  (await screen.findAllByText(text)).find((el) => el.closest("[data-toast]") !== null);

afterEach(() => window.history.replaceState(null, "", "/settings/"));

describe("Settings → Review", () => {
  it("sits in the outline between Tags and RSS, and shows the saved settings", async () => {
    renderReview();
    const outline = screen.getByRole("navigation", { name: "Settings sections" });
    const labels = within(outline)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(labels.slice(3, 6)).toEqual(["Tags", "Review", "RSS"]);
    const review = await loaded();
    expect(review.getByRole("combobox", { name: "Pre-review" })).toHaveValue("triage");
    expect(review.getByRole("combobox", { name: "Agent" })).toHaveValue("codex");
    expect(review.getByLabelText("Coding-standards checkout")).toHaveValue("/Users/me/coding-standards");
    expect(review.getByLabelText("Your own standards folder")).toHaveValue("");
    const web = within(review.getByRole("group", { name: "acme/web" }));
    expect(web.getByRole("checkbox", { name: "Watched" })).toBeChecked();
    expect(web.getByRole("combobox", { name: "Pre-review for acme/web" })).toHaveValue("everything");
    expect(web.getByLabelText("Automated reviewer")).toHaveValue("copilot");
    expect(web.getByLabelText("Metrics")).toHaveValue("Security\nTests");
    const lib = within(review.getByRole("group", { name: "other/lib" }));
    expect(lib.getByText("Suggested")).toBeInTheDocument();
    expect(lib.getByRole("checkbox", { name: "Watched" })).not.toBeChecked();
  });

  it("saves the pre-review default and the agent as they change, with a fresh key each", async () => {
    const calls = renderReview();
    const review = await loaded();
    await userEvent.selectOptions(review.getByRole("combobox", { name: "Pre-review" }), "on_open");
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(1));
    // Only what changed (with the two required fields): no repo, no folder rides along (I1).
    expect(await lastPut(calls)).toEqual({ prereview: "on_open", runner: "codex", repos: [] });
    expect(await toast("Pre-review: on open only.")).toBeDefined();
    await userEvent.selectOptions(review.getByRole("combobox", { name: "Agent" }), "claude");
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(2));
    expect(await lastPut(calls)).toEqual({ prereview: "on_open", runner: "claude", repos: [] });
    const keys = sent(calls, "PUT", "/api/review/settings").map((c) => c.headers.get("Idempotency-Key"));
    expect(new Set(keys).size).toBe(2);
  });

  it("saves the standards folders, and shows why one is refused", async () => {
    let answer: (req: Request) => Promise<Response> = async (req) => Response.json(await req.clone().json());
    const calls = renderReview({ "PUT /api/review/settings": (req: Request) => answer(req) });
    const review = await loaded();
    const mine = review.getByLabelText("Your own standards folder");
    await userEvent.click(mine);
    await userEvent.paste("~/my-standards");
    await userEvent.click(review.getByRole("button", { name: "Save folders" }));
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(1));
    expect(await lastPut(calls)).toEqual({
      prereview: "triage",
      runner: "codex",
      repos: [],
      standards_dir: "/Users/me/coding-standards",
      standards_user_dir: "~/my-standards",
    });
    expect(await toast("Standards folders saved.")).toBeDefined();

    answer = async () =>
      Response.json(
        { error: "InvalidSettingError", detail: "a folder must be an absolute path or start with ~/: 'x'" },
        { status: 400 },
      );
    const checkout = review.getByLabelText("Coding-standards checkout");
    await userEvent.clear(checkout);
    await userEvent.click(checkout);
    await userEvent.paste("x");
    await userEvent.click(review.getByRole("button", { name: "Save folders" }));
    expect(await review.findByText("a folder must be an absolute path or start with ~/: 'x'")).toBeInTheDocument();
  });

  it("saves a repo's overrides, its metrics one per line, from the next round on", async () => {
    const calls = renderReview();
    const review = await loaded();
    const web = within(review.getByRole("group", { name: "acme/web" }));
    expect(web.getByText("Changes apply from the repo's next review round.")).toBeInTheDocument();
    await userEvent.click(web.getByRole("checkbox", { name: "Watched" }));
    await userEvent.selectOptions(web.getByRole("combobox", { name: "Pre-review for acme/web" }), "");
    const reviewer = web.getByLabelText("Automated reviewer");
    await userEvent.clear(reviewer);
    const metrics = web.getByLabelText("Metrics");
    await userEvent.clear(metrics);
    await userEvent.click(metrics);
    await userEvent.paste("Security\n\n  Docs  \n");
    await userEvent.click(web.getByRole("button", { name: "Save acme/web" }));
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(1));
    const body = await lastPut(calls);
    expect(body).not.toHaveProperty("standards_dir");
    expect(body).not.toHaveProperty("standards_user_dir");
    expect(body.repos).toEqual([
      {
        repo: "acme/web",
        watched: false,
        suggested: false,
        prereview: null,
        automated_reviewer: null,
        metrics: ["Security", "Docs"],
      },
    ]);
    expect(await toast("acme/web saved.")).toBeDefined();
  });

  it("a change made elsewhere while the page is open survives a save here (I1)", async () => {
    // The server, as the real one: repos not listed and folders left out keep their values.
    let server: Settings = structuredClone(SETTINGS);
    const calls = renderReview({
      "GET /api/review/settings": () => Response.json(server),
      "PUT /api/review/settings": async (req: Request) => {
        const body = (await req.clone().json()) as Settings;
        const repos = new Map((server.repos ?? []).map((r) => [r.repo, r]));
        for (const r of body.repos ?? []) repos.set(r.repo, { ...repos.get(r.repo), ...r });
        server = {
          ...server,
          prereview: body.prereview,
          runner: body.runner,
          ...("standards_dir" in body ? { standards_dir: body.standards_dir } : {}),
          ...("standards_user_dir" in body ? { standards_user_dir: body.standards_user_dir } : {}),
          repos: [...repos.values()],
        };
        return Response.json(server);
      },
    });
    const review = await loaded();
    // Confirmed in the queue (or another tab) after the page loaded.
    server = {
      ...server,
      standards_dir: "/Users/me/elsewhere",
      repos: (server.repos ?? []).map((r) => (r.repo === "other/lib" ? { ...r, watched: true, suggested: false } : r)),
    };
    await userEvent.selectOptions(review.getByRole("combobox", { name: "Pre-review" }), "everything");
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(1));
    expect(server.prereview).toBe("everything");
    expect(server.standards_dir).toBe("/Users/me/elsewhere");
    expect(server.repos?.find((r) => r.repo === "other/lib")).toMatchObject({ watched: true, suggested: false });
    // The page shows the server's answer, the other change included.
    const lib = within(review.getByRole("group", { name: "other/lib" }));
    await waitFor(() => expect(lib.getByRole("checkbox", { name: "Watched" })).toBeChecked());
    await waitFor(() => expect(review.getByLabelText("Coding-standards checkout")).toHaveValue("/Users/me/elsewhere"));
  });

  it("watching a suggested repo confirms it", async () => {
    const calls = renderReview();
    const lib = within((await loaded()).getByRole("group", { name: "other/lib" }));
    await userEvent.click(lib.getByRole("checkbox", { name: "Watched" }));
    await userEvent.click(lib.getByRole("button", { name: "Save other/lib" }));
    await waitFor(() => expect(sent(calls, "PUT", "/api/review/settings")).toHaveLength(1));
    const [repo] = (await lastPut(calls)).repos ?? [];
    expect(repo).toMatchObject({ repo: "other/lib", watched: true, suggested: false });
  });

  it("shows why the settings couldn't load, and Try again loads them", async () => {
    let fail = true;
    renderReview({
      "GET /api/review/settings": () =>
        fail ? Response.json({ detail: "module review is not ready" }, { status: 503 }) : Response.json(SETTINGS),
    });
    const review = await section();
    expect(
      await review.findByText(/Couldn't load the review settings: module review is not ready\./),
    ).toBeInTheDocument();
    fail = false;
    await userEvent.click(review.getByRole("button", { name: "Try again" }));
    expect(await review.findByRole("combobox", { name: "Pre-review" })).toHaveValue("triage");
  });
});

describe("Settings → Review when a write or a read fails", () => {
  it("says why the settings got no answer at all, without a restart hint", async () => {
    renderReview({
      "GET /api/review/settings": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const review = await section();
    expect(
      await review.findByText(
        "Couldn't load the review settings: Failed to fetch. Check that Jesinia is running, then try again.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the saved pre-review and says why a change was refused", async () => {
    renderReview({
      "PUT /api/review/settings": () =>
        Response.json({ error: "InvalidSettingError", detail: "not an agent runner name" }, { status: 400 }),
    });
    const review = await loaded();
    const prereview = review.getByRole("combobox", { name: "Pre-review" });
    await userEvent.selectOptions(prereview, "everything");
    expect(await toast("Couldn't save: not an agent runner name")).toBeDefined();
    expect(prereview).toHaveValue("triage");
  });

  it("says why a repo couldn't be saved when Jesinia doesn't answer", async () => {
    renderReview({
      "PUT /api/review/settings": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const web = within((await loaded()).getByRole("group", { name: "acme/web" }));
    await userEvent.click(web.getByRole("button", { name: "Save acme/web" }));
    expect(
      await toast("Couldn't save acme/web: Failed to fetch. Check that Jesinia is running, then try again."),
    ).toBeDefined();
  });

  it("says why the knowledge model couldn't load, and Try again reads it", async () => {
    let answer: () => Response = () => Response.json({ detail: "module review is not ready" }, { status: 503 });
    renderReview({ "GET /api/review/knowledge": () => answer() });
    const known = await knowledge();
    expect(
      await known.findByText(/Couldn't load what Jesinia thinks you know: module review is not ready\./),
    ).toBeInTheDocument();
    answer = () => {
      throw new TypeError("Failed to fetch");
    };
    await userEvent.click(known.getByRole("button", { name: "Try again" }));
    expect(await known.findByText(/Failed to fetch\. Check that Jesinia is running/)).toBeInTheDocument();
    answer = () => Response.json({ units: UNITS, total: UNITS.length, truncated: false });
    await userEvent.click(known.getByRole("button", { name: "Try again" }));
    expect(await known.findAllByRole("listitem")).toHaveLength(4);
  });
});

describe("What Jesinia thinks you know", () => {
  it("lists each unit with its familiarity and where it comes from", async () => {
    renderReview();
    const known = await knowledge();
    expect(await known.findByText("KB pages aren't counted yet.")).toBeInTheDocument();
    const src = within(known.getByRole("listitem", { name: "acme/web / src" }));
    expect(src.getByRole("meter", { name: "acme/web / src: 100% familiar" })).toBeInTheDocument();
    expect(src.getByText("Known: marked on acme/web")).toBeInTheDocument();
    expect(src.getByRole("combobox", { name: "Correct acme/web / src" })).toHaveValue("");
    const root = within(known.getByRole("listitem", { name: "acme/web" }));
    expect(root.getByText("Known: you marked it")).toBeInTheDocument();
    expect(root.getByRole("combobox", { name: "Correct acme/web" })).toHaveValue("known");
    const billing = within(known.getByRole("listitem", { name: "acme/api / billing" }));
    expect(billing.getByText("No record of its own: inferred from nearby areas")).toBeInTheDocument();
    const topic = within(known.getByRole("listitem", { name: "#payments" }));
    expect(topic.getByRole("combobox", { name: "Correct #payments" })).toHaveValue("unknown");
    expect(known.getAllByRole("listitem")).toHaveLength(4);
  });

  it("describes a unit's evidence in words", async () => {
    renderReview({
      "GET /api/review/knowledge": {
        units: [unit({ exposures: { review_deep: 2, commit: 1, quiz_pass: 1 }, changes_since_last: 120 })],
        total: 1,
        truncated: false,
      },
    });
    const src = within((await knowledge()).getByRole("listitem", { name: "acme/web / src" }));
    expect(
      src.getByText("2 deep reviews, 1 commit, 1 quiz passed · last Oct 3, 2026 · 120 lines changed since"),
    ).toBeInTheDocument();
  });

  it("words other evidence, and a mark without a stated folder as the unit's own", async () => {
    renderReview({
      "GET /api/review/knowledge": {
        units: [
          unit({ exposures: { review_quick: 1, quiz_fail: 2 }, last_exposure: null, changes_since_last: 0 }),
          unit({ unit: "repo/acme/web/docs", path: "docs", override: "unknown", override_unit: null }),
        ],
        total: 2,
        truncated: false,
      },
    });
    const known = await knowledge();
    const src = within(await known.findByRole("listitem", { name: "acme/web / src" }));
    expect(src.getByText("1 quick review, 2 quizzes failed")).toBeInTheDocument();
    const docs = within(known.getByRole("listitem", { name: "acme/web / docs" }));
    expect(docs.getByText("Unknown: you marked it")).toBeInTheDocument();
    expect(docs.getByRole("combobox", { name: "Correct acme/web / docs" })).toHaveValue("unknown");
  });

  it("marks a unit known, unknown or cleared, each with its own key", async () => {
    const answers: Unit[] = [
      unit({ unit: "repo/acme/api/billing", repo: "acme/api", path: "billing", familiarity: 1, override: "known", override_unit: "repo/acme/api/billing" }), // prettier-ignore
      unit({ unit: "repo/acme/api/billing", repo: "acme/api", path: "billing", familiarity: 0.3, distance: 0.5, exposures: {} }), // prettier-ignore
    ];
    // The server's model: each correction is read back by the next GET, as the real one does.
    let units = UNITS;
    const calls = renderReview({
      "GET /api/review/knowledge": () => Response.json({ units, total: units.length, truncated: false }),
      "PUT /api/review/knowledge/repo/acme/api/billing": () => {
        const answer = answers.shift() as Unit;
        units = units.map((u) => (u.unit === answer.unit ? answer : u));
        return Response.json(answer);
      },
    });
    const known = await knowledge();
    const billing = within(await known.findByRole("listitem", { name: "acme/api / billing" }));
    const correct = billing.getByRole("combobox", { name: "Correct acme/api / billing" });
    await userEvent.selectOptions(correct, "known");
    await waitFor(() => expect(billing.getByText("Known: you marked it")).toBeInTheDocument());
    expect(billing.getByRole("meter", { name: "acme/api / billing: 100% familiar" })).toBeInTheDocument();
    await userEvent.selectOptions(correct, "");
    await waitFor(() =>
      expect(billing.getByText("No record of its own: inferred from nearby areas")).toBeInTheDocument(),
    );
    const puts = sent(calls, "PUT", "/api/review/knowledge/repo/acme/api/billing");
    expect(await Promise.all(puts.map((p) => p.clone().json()))).toEqual([{ override: "known" }, { override: null }]);
    const keys = puts.map((p) => p.headers.get("Idempotency-Key"));
    expect(keys.every((k) => /^[0-9a-f-]{36}$/.test(k ?? ""))).toBe(true);
    expect(new Set(keys).size).toBe(2);
  });

  it("never lets an older reload overwrite a newer one, and keeps the list when a reload fails (M6)", async () => {
    const billing = (over: Partial<Unit>) => unit({ unit: "repo/acme/api/billing", repo: "acme/api", path: "billing", ...over }); // prettier-ignore
    const payments = (over: Partial<Unit>) => unit({ unit: "tag/payments", kind: "tag", repo: null, path: "", tag: "payments", exposures: {}, ...over }); // prettier-ignore
    const billingKnown = billing({ familiarity: 1, override: "known", override_unit: "repo/acme/api/billing" });
    const paymentsKnown = payments({ familiarity: 1, override: "known", override_unit: "tag/payments" });
    const first = { units: [billing({}), payments({ override: "unknown", override_unit: "tag/payments" })], total: 2, truncated: false }; // prettier-ignore
    let release: () => void = () => undefined;
    const answers: (() => Response | Promise<Response>)[] = [
      () => Response.json(first),
      // The first correction's reload answers late, with only that correction.
      () =>
        new Promise<Response>((done) => {
          release = () => done(Response.json({ ...first, units: [billingKnown, first.units[1]] }));
        }),
      () => Response.json({ ...first, units: [billingKnown, paymentsKnown] }),
      () => {
        throw new TypeError("Failed to fetch");
      },
    ];
    renderReview({
      "GET /api/review/knowledge": () => (answers.shift() as () => Response)(),
      "PUT /api/review/knowledge/repo/acme/api/billing": () => Response.json(billingKnown),
      "PUT /api/review/knowledge/tag/payments": () => Response.json(paymentsKnown),
    });
    const known = await knowledge();
    const topic = within(await known.findByRole("listitem", { name: "#payments" }));
    const bill = within(known.getByRole("listitem", { name: "acme/api / billing" }));
    await userEvent.selectOptions(bill.getByRole("combobox", { name: "Correct acme/api / billing" }), "known");
    await waitFor(() => expect(bill.getByText("Known: you marked it")).toBeInTheDocument());
    await userEvent.selectOptions(topic.getByRole("combobox", { name: "Correct #payments" }), "known");
    await waitFor(() => expect(topic.getByText("Known: you marked it")).toBeInTheDocument());
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(topic.getByText("Known: you marked it")).toBeInTheDocument();
    // A failed reload after a correction keeps the list and says why.
    await userEvent.selectOptions(bill.getByRole("combobox", { name: "Correct acme/api / billing" }), "");
    expect(await toast(/Couldn't read the knowledge model again: Failed to fetch/)).toBeDefined();
    expect(known.getAllByRole("listitem")).toHaveLength(2);
  });

  it("puts a correction back and says why when it is refused", async () => {
    renderReview({
      "PUT /api/review/knowledge/tag/payments": () =>
        Response.json({ error: "InvalidUnitError", detail: "not a unit" }, { status: 400 }),
    });
    const topic = within((await knowledge()).getByRole("listitem", { name: "#payments" }));
    const correct = topic.getByRole("combobox", { name: "Correct #payments" });
    await userEvent.selectOptions(correct, "known");
    expect(await toast("Couldn't correct #payments: not a unit")).toBeDefined();
    await waitFor(() => expect(correct).toHaveValue("unknown"));
  });

  it("renders folder names as text, with hidden characters made visible", async () => {
    renderReview({
      "GET /api/review/knowledge": {
        units: [unit({ unit: "repo/acme/web/<b>x</b>\u202Eevil", path: "<b>x</b>\u202Eevil" })],
        total: 1,
        truncated: false,
      },
    });
    const known = await knowledge();
    const item = await known.findByRole("listitem", { name: "acme/web / <b>x</b>⟪U+202E⟫evil" });
    expect(within(item).getByText("⟪U+202E⟫")).toBeInTheDocument();
    expect(item.querySelector("b")).toBeNull();
  });

  it("says when there is nothing yet, and when the list is cut", async () => {
    renderReview({ "GET /api/review/knowledge": { units: [], total: 0, truncated: false } });
    expect(await (await knowledge()).findByText(/Nothing yet: Jesinia learns from your commits/)).toBeInTheDocument();
  });

  it("notes a cut list", async () => {
    renderReview({ "GET /api/review/knowledge": { units: UNITS, total: 900, truncated: true, offset: 0 } });
    expect(await (await knowledge()).findByText("Showing 4 of 900 areas.")).toBeInTheDocument();
  });

  it("reaches every unit page by page, and a later page's unit can be corrected (I4)", async () => {
    const all: Unit[] = [
      ...Array.from({ length: 300 }, (_, i) =>
        unit({ unit: `repo/acme/api/a${i}`, repo: "acme/api", path: `a${i}`, familiarity: 0.5 }),
      ),
      ...Array.from({ length: 300 }, (_, i) =>
        unit({ unit: `repo/acme/web/w${i}`, repo: "acme/web", path: `w${i}`, familiarity: 0.5 }),
      ),
    ];
    const calls = renderReview({
      "GET /api/review/knowledge": (request: Request) => {
        const url = new URL(request.url);
        const offset = Number(url.searchParams.get("offset") ?? 0);
        const limit = Number(url.searchParams.get("limit") ?? 500);
        const units = all.slice(offset, offset + limit);
        return Response.json({ units, total: all.length, truncated: offset + units.length < all.length, offset });
      },
      "PUT /api/review/knowledge/repo/acme/web/w299": () =>
        Response.json({ ...all[599], familiarity: 1, override: "known", override_unit: "repo/acme/web/w299" }),
    });
    const known = await knowledge();
    expect(await known.findByText(/Showing \d+ of 600 areas\./)).toBeInTheDocument();
    expect(known.queryByRole("listitem", { name: "acme/web / w299" })).toBeNull();
    while (known.queryByRole("button", { name: "Show more" })) {
      await userEvent.click(known.getByRole("button", { name: "Show more" }));
      await waitFor(() => expect(known.queryByText(/Loading more/)).toBeNull());
    }
    const last = within(await known.findByRole("listitem", { name: "acme/web / w299" }));
    await userEvent.selectOptions(last.getByRole("combobox", { name: "Correct acme/web / w299" }), "known");
    expect(sent(calls, "PUT", "/api/review/knowledge/repo/acme/web/w299")).toHaveLength(1);
    const offsets = sent(calls, "GET", "/api/review/knowledge").map((r) => new URL(r.url).searchParams.get("offset"));
    expect(offsets).toContain("200");
  });
});
