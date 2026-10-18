// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Issues (spec §5, plan Task 14): watched repos, each repo's label schema (where it
// comes from, its aliases), the label setup and migration batches (previewed, then applied as an
// approval), and the runner that answers suggestion asks. Every save sends only what it changes,
// with its own Idempotency-Key; label names are anyone's to write and render with R62 tokens.
import type { components } from "@jesinia/client";
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";

type Repo = components["schemas"]["IssuesRepoOut"];
type Schema = components["schemas"]["LabelSchemaBody"];
type Plan = components["schemas"]["LabelPlanOut"];
type Applied = components["schemas"]["LabelApplyOut"];

const value = (name: string, aliases: string[] = []) => ({ name, aliases });
const SCHEMA: Schema = {
  readiness: [value("agent-ready"), value("design-needed", ["design needed"])],
  priority: [value("priority: high"), value("priority: low")],
  type: [value("bug")],
  areas: [value("area:cli", ["cli"])],
  epic: value("epic"),
  initiative: value("initiative"),
  free_prefixes: ["review:"],
};

function repo(over: Partial<Repo> = {}): Repo {
  return {
    repo: "acme/web",
    watched: true,
    added_at: "2026-10-01T00:00:00Z",
    schema_source: "default",
    label_schema: SCHEMA,
    schema_errors: [],
    settings_schema: null,
    migrated: false,
    synced_at: "2026-10-18T10:00:00Z",
    sync_error: null,
    unmapped_labels: [],
    ...over,
  };
}

const PLAN: Plan = {
  plan_id: "plan-1",
  repo: "acme/web",
  kind: "setup",
  expires_at: "2026-10-18T11:00:00Z",
  ops: [
    { op: "create", name: "epic", color: "5319e7", description: "", replacement: null, members: [], skip_cause: null },
  ],
};

const json = (body: unknown, status = 200) => Response.json(body, { status });

/** Settings opened at Issues, over `routes`; every request is recorded. */
function renderIssues(routes: Routes = {}, repos: Repo[] = [repo()]) {
  window.history.replaceState(null, "", "/settings/#issues");
  const { client, calls } = fakeClient(
    baseRoutes({
      "GET /api/agents/runners": {
        "claude-code": { available: true, version: "2", detail: "ok" },
        codex: { available: true, version: "1", detail: "ok" },
      },
      "GET /api/issues/repos": { repos },
      "GET /api/issues/settings": { runner: null },
      ...routes,
    }),
  );
  render(<App path="/settings/" client={client} />);
  return calls;
}

const section = async () => within(await screen.findByRole("region", { name: "Issues" }));
const repoGroup = async (name = "acme/web") => within(await (await section()).findByRole("group", { name }));
const sent = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);
const bodyOf = async (req: Request) => (await req.clone().json()) as unknown;

afterEach(() => window.history.replaceState(null, "", "/settings/"));

describe("Settings → Issues: repos", () => {
  it("is in the outline after RSS and lists each repo with where its schema comes from", async () => {
    renderIssues({}, [repo(), repo({ repo: "acme/api", schema_source: "repo", watched: false })]);
    const outline = screen.getByRole("navigation", { name: "Settings sections" });
    const names = within(outline)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(names.indexOf("Issues")).toBe(names.indexOf("RSS") + 1);
    const web = await repoGroup();
    expect(web.getByText("The default label convention")).toBeInTheDocument();
    expect(web.getByRole("checkbox", { name: "Watched" })).toBeChecked();
    const api = await repoGroup("acme/api");
    expect(api.getByText(/the repo's .jesinia\/labels.yml/)).toBeInTheDocument();
    expect(api.getByRole("checkbox", { name: "Watched" })).not.toBeChecked();
  });

  it("adds a repo by sending only watched, with an idempotency key", async () => {
    const calls = renderIssues({
      "PATCH /api/issues/repos/acme/new": () => json(repo({ repo: "acme/new" })),
    });
    const issues = await section();
    await userEvent.type(issues.getByRole("textbox", { name: "Add a repo" }), "acme/new");
    await userEvent.click(issues.getByRole("button", { name: "Watch" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/new")).toHaveLength(1));
    const [req] = sent(calls, "PATCH", "/api/issues/repos/acme/new");
    expect(await bodyOf(req as Request)).toEqual({ watched: true });
    expect(req?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await issues.findByRole("group", { name: "acme/new" })).toBeInTheDocument();
  });

  it("refuses a repo that isn't owner/name before sending anything", async () => {
    const calls = renderIssues();
    const issues = await section();
    await userEvent.type(issues.getByRole("textbox", { name: "Add a repo" }), "just-a-name");
    await userEvent.click(issues.getByRole("button", { name: "Watch" }));
    expect(await issues.findByText("Write the repo as owner/name.")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(0);
  });

  it("unwatches with a save of watched alone, never the schema", async () => {
    const calls = renderIssues({
      "PATCH /api/issues/repos/acme/web": () => json(repo({ watched: false })),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("checkbox", { name: "Watched" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    expect(await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)).toEqual({ watched: false });
    await waitFor(() => expect(web.getByRole("checkbox", { name: "Watched" })).not.toBeChecked());
  });

  it("shows a failed save's cause and keeps what the server has", async () => {
    renderIssues({
      "PATCH /api/issues/repos/acme/web": () =>
        json({ error: "GitHubUnavailable", detail: "GitHub is unreachable" }, 503),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("checkbox", { name: "Watched" }));
    expect((await screen.findAllByText("Couldn't save acme/web: GitHub is unreachable")).length).toBeGreaterThan(0);
    expect(web.getByRole("checkbox", { name: "Watched" })).toBeChecked();
  });

  it("explains a load that failed and loads again on Try again", async () => {
    let fail = true;
    renderIssues({
      "GET /api/issues/repos": () =>
        fail ? json({ error: "x", detail: "issues module not started" }, 503) : json({ repos: [repo()] }),
    });
    const issues = await section();
    expect(await issues.findByText(/Couldn't load the Issues settings: issues module not started/)).toBeInTheDocument();
    fail = false;
    await userEvent.click(issues.getByRole("button", { name: "Try again" }));
    expect(await issues.findByRole("group", { name: "acme/web" })).toBeInTheDocument();
  });

  it("shows a repo's sync problem and labels.yml errors", async () => {
    renderIssues({}, [repo({ sync_error: "rate limited", schema_errors: ["readiness: not a list"] })]);
    const web = await repoGroup();
    expect(web.getByText("Last sync failed: rate limited")).toBeInTheDocument();
    expect(web.getByText("readiness: not a list")).toBeInTheDocument();
  });
});

describe("Settings → Issues: the label schema and its aliases", () => {
  it("saves edited aliases as the Settings schema alone", async () => {
    const calls = renderIssues({
      "PATCH /api/issues/repos/acme/web": async (req: Request) => {
        const body = (await req.clone().json()) as { settings_schema: Schema };
        return json(
          repo({
            schema_source: "settings",
            settings_schema: body.settings_schema,
            label_schema: body.settings_schema,
          }),
        );
      },
    });
    const web = await repoGroup();
    const aliases = web.getByRole("textbox", { name: "Aliases of design-needed" });
    await userEvent.clear(aliases);
    await userEvent.type(aliases, "design needed{Enter}needs design");
    await userEvent.click(web.getByRole("button", { name: "Save labels for acme/web" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    const body = (await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)) as {
      settings_schema: Schema;
    };
    expect(Object.keys(body)).toEqual(["settings_schema"]);
    expect(body.settings_schema.readiness[1]).toEqual(value("design-needed", ["design needed", "needs design"]));
    expect(body.settings_schema.epic).toEqual(value("epic"));
    // The saved schema starts the form again from what the server kept.
    expect(await (await repoGroup()).findByText("Made here in Settings")).toBeInTheDocument();
  });

  it("adds and removes an area", async () => {
    const calls = renderIssues({ "PATCH /api/issues/repos/acme/web": () => json(repo()) });
    const web = await repoGroup();
    await userEvent.type(web.getByRole("textbox", { name: "New area" }), "area:wiki");
    await userEvent.click(web.getByRole("button", { name: "Add area" }));
    await userEvent.click(web.getByRole("button", { name: "Remove area:cli" }));
    await userEvent.click(web.getByRole("button", { name: "Save labels for acme/web" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    const body = (await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)) as {
      settings_schema: Schema;
    };
    expect(body.settings_schema.areas).toEqual([value("area:wiki")]);
  });

  it("goes back to the default only after a confirm, and Undo puts the Settings schema back", async () => {
    const calls = renderIssues(
      {
        "PATCH /api/issues/repos/acme/web": async (req: Request) => {
          const body = (await req.clone().json()) as { settings_schema: Schema | null };
          return body.settings_schema === null
            ? json(repo())
            : json(repo({ schema_source: "settings", settings_schema: body.settings_schema }));
        },
      },
      [repo({ schema_source: "settings", settings_schema: SCHEMA })],
    );
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Use the default for acme/web" }));
    // Nothing is sent until the author confirms; Cancel keeps everything.
    const confirm = await screen.findByRole("alertdialog", { name: "Use the default for acme/web?" });
    expect(confirm).toHaveTextContent("Your aliases and areas will be removed.");
    await userEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(0);

    await userEvent.click(web.getByRole("button", { name: "Use the default for acme/web" }));
    const again = await screen.findByRole("alertdialog", { name: "Use the default for acme/web?" });
    await userEvent.click(within(again).getByRole("button", { name: "Use the default" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    expect(await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)).toEqual({
      settings_schema: null,
    });
    const undo = (await screen.findAllByRole("button", { name: "Undo" }))[0] as HTMLElement;
    await userEvent.click(undo);
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(2));
    expect(await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[1] as Request)).toEqual({
      settings_schema: SCHEMA,
    });
  });

  it("lists the repo's labels the schema doesn't map, and picks one as an alias by its exact name", async () => {
    const calls = renderIssues({ "PATCH /api/issues/repos/acme/web": () => json(repo()) }, [
      repo({ unmapped_labels: ["wontfix", "Needs Design", `x${String.fromCodePoint(0x202e)}y`] }),
    ]);
    const web = await repoGroup();
    const unmapped = web.getByRole("list", { name: "Labels on GitHub not in the schema" });
    expect(within(unmapped).getAllByRole("listitem")).toHaveLength(3);
    expect(within(unmapped).getByText("⟪U+202E⟫")).toBeInTheDocument();
    await userEvent.selectOptions(web.getByRole("combobox", { name: "Alias of… for Needs Design" }), "design-needed");
    expect(web.getByRole("textbox", { name: "Aliases of design-needed" })).toHaveValue("design needed\nNeeds Design");
    expect(within(unmapped).getAllByRole("listitem")).toHaveLength(2);
    await userEvent.click(web.getByRole("button", { name: "Save labels for acme/web" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    const body = (await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)) as {
      settings_schema: Schema;
    };
    expect(body.settings_schema.readiness[1]).toEqual(value("design-needed", ["design needed", "Needs Design"]));
  });

  it("shows a labels.yml repo's unmapped labels without offering to map them here", async () => {
    renderIssues({}, [repo({ schema_source: "repo", unmapped_labels: ["wontfix"] })]);
    const web = await repoGroup();
    expect(web.getByRole("list", { name: "Labels on GitHub not in the schema" })).toHaveTextContent("wontfix");
    expect(web.queryByRole("combobox", { name: "Alias of… for wontfix" })).toBeNull();
    expect(web.getByText(/add them as aliases in .jesinia\/labels.yml/)).toBeInTheDocument();
  });

  it("shows a labels.yml schema read-only", async () => {
    renderIssues({}, [repo({ schema_source: "repo" })]);
    const web = await repoGroup();
    expect(web.queryByRole("button", { name: "Save labels for acme/web" })).toBeNull();
    expect(web.queryByRole("textbox", { name: "Aliases of design-needed" })).toBeNull();
    expect(web.getByText("design needed")).toBeInTheDocument();
  });

  it("renders hidden characters in label names as visible tokens (R62)", async () => {
    const sneaky = { ...SCHEMA, type: [value("bug", [`b${String.fromCodePoint(0x202e)}ug`])] };
    renderIssues({}, [repo({ schema_source: "repo", label_schema: sneaky })]);
    const web = await repoGroup();
    expect(web.getByText("⟪U+202E⟫")).toBeInTheDocument();
  });
});

describe("Settings → Issues: label setup and migration", () => {
  it("previews the setup, then applies that plan with a key and shows each step", async () => {
    const applied: Applied = {
      repo: "acme/web",
      kind: "setup",
      complete: true,
      steps: [{ op: PLAN.ops[0] as Plan["ops"][number], state: "done", cause: null, failed_issues: [] }],
      note: null,
      stopped: null,
      rate_limited: false,
      rate_limited_until: null,
    };
    const calls = renderIssues({
      "POST /api/issues/labels/acme/web/preview": () => json(PLAN),
      "POST /api/issues/labels/acme/web/apply": () => json(applied),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    const plan = await web.findByRole("region", { name: "Setup for acme/web" });
    expect(within(plan).getByText("Create")).toBeInTheDocument();
    expect(within(plan).getByText("epic")).toBeInTheDocument();
    await userEvent.click(within(plan).getByRole("button", { name: "Apply setup" }));
    await waitFor(() => expect(sent(calls, "POST", "/api/issues/labels/acme/web/apply")).toHaveLength(1));
    const [req] = sent(calls, "POST", "/api/issues/labels/acme/web/apply");
    expect(await bodyOf(req as Request)).toEqual({ kind: "setup", plan_id: "plan-1" });
    expect(req?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await within(plan).findByText("Setup applied: every step done.")).toBeInTheDocument();
    expect(within(plan).getByText("Done")).toBeInTheDocument();
    expect(await bodyOf(sent(calls, "POST", "/api/issues/labels/acme/web/preview")[0] as Request)).toEqual({
      kind: "setup",
    });
  });

  it("says nothing needs doing for an empty plan", async () => {
    renderIssues({ "POST /api/issues/labels/acme/web/preview": () => json({ ...PLAN, ops: [] }) });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    expect(await web.findByText("Nothing to do: every canonical label exists.")).toBeInTheDocument();
    expect(web.queryByRole("button", { name: "Apply setup" })).toBeNull();
  });

  it("previews a migration's live-listed moves and shows a comma refusal clearly", async () => {
    const migration: Plan = {
      ...PLAN,
      kind: "migrate",
      ops: [
        {
          op: "delete",
          name: "design needed",
          color: null,
          description: null,
          replacement: "design-needed",
          members: [3, 4],
          skip_cause: null,
        },
        {
          op: "delete",
          name: "needs, info",
          color: null,
          description: null,
          replacement: "information-needed",
          members: [5],
          skip_cause: "not deleted: GitHub can't list the issues carrying a label whose name has a comma",
        },
      ],
    };
    renderIssues({ "POST /api/issues/labels/acme/web/preview": () => json(migration) });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview migration for acme/web" }));
    const plan = await web.findByRole("region", { name: "Migration for acme/web" });
    expect(within(plan).getByText(/listed again, live from GitHub/)).toBeInTheDocument();
    const rows = within(plan).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Move 2 issues from design needed to design-needed, then delete design needed");
    expect(rows[1]).toHaveTextContent("Skipped");
    expect(rows[1]).toHaveTextContent("has a comma");
  });

  it("shows a refused preview's cause (labels that compare as one name)", async () => {
    renderIssues({
      "POST /api/issues/labels/acme/web/preview": () =>
        json(
          { error: "LabelNameCollisionError", detail: "Bug and bug compare as one name; rename one on GitHub" },
          409,
        ),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview migration for acme/web" }));
    expect(await web.findByRole("alert")).toHaveTextContent("Bug and bug compare as one name; rename one on GitHub");
  });

  it("shows where GitHub stopped an apply, and an outdated plan asks for a fresh preview", async () => {
    const stopped: Applied = {
      repo: "acme/web",
      kind: "setup",
      complete: false,
      steps: [{ op: PLAN.ops[0] as Plan["ops"][number], state: "failed", cause: "rate limited", failed_issues: [] }],
      note: null,
      stopped: "GitHub's rate limit stopped the batch",
      rate_limited: true,
      rate_limited_until: null,
    };
    let answer = json(stopped, 503);
    renderIssues({
      "POST /api/issues/labels/acme/web/preview": () => json(PLAN),
      "POST /api/issues/labels/acme/web/apply": () => answer,
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    const plan = await web.findByRole("region", { name: "Setup for acme/web" });
    await userEvent.click(within(plan).getByRole("button", { name: "Apply setup" }));
    expect(await within(plan).findByText(/GitHub's rate limit stopped the batch/)).toBeInTheDocument();
    expect(within(plan).getByText("Failed: rate limited")).toBeInTheDocument();

    answer = json({ error: "LabelPlanOutdatedError", detail: "the repo's labels changed; preview again" }, 409);
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    const fresh = await web.findByRole("region", { name: "Setup for acme/web" });
    await userEvent.click(within(fresh).getByRole("button", { name: "Apply setup" }));
    expect(await within(fresh).findByRole("alert")).toHaveTextContent("the repo's labels changed; preview again");
  });
});

describe("Settings → Issues: the edges", () => {
  it("says when there are no repos, and why a repo couldn't be added", async () => {
    renderIssues(
      { "PATCH /api/issues/repos/acme/gone": () => json({ error: "GitHubRequestError", detail: "Not Found" }, 502) },
      [],
    );
    const issues = await section();
    expect(issues.getByText("No repos yet: add one above.")).toBeInTheDocument();
    await userEvent.type(issues.getByRole("textbox", { name: "Add a repo" }), "acme/gone");
    await userEvent.click(issues.getByRole("button", { name: "Watch" }));
    expect(await issues.findByText("Couldn't watch acme/gone: Not Found")).toBeInTheDocument();
  });

  it("explains a request that got no answer", async () => {
    renderIssues({
      "POST /api/issues/labels/acme/web/preview": () => Promise.reject(new TypeError("Failed to fetch")),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    expect(await web.findByRole("alert")).toHaveTextContent("Failed to fetch. Check that Jesinia is running");
  });

  it("explains a load that got no answer", async () => {
    renderIssues({ "GET /api/issues/repos": () => Promise.reject(new TypeError("Failed to fetch")) });
    const issues = await section();
    expect(await issues.findByText(/Couldn't load the Issues settings: Failed to fetch/)).toBeInTheDocument();
    expect(issues.queryByText(/Restart Jesinia/)).toBeNull();
  });

  it("keeps the aliases typed for an area, says a migrated repo is, and lists a plain delete", async () => {
    const calls = renderIssues(
      {
        "PATCH /api/issues/repos/acme/web": async (req: Request) => {
          const body = (await req.clone().json()) as { settings_schema: Schema };
          return json(repo({ migrated: true, schema_source: "settings", settings_schema: body.settings_schema }));
        },
        "POST /api/issues/labels/acme/web/preview": () =>
          json({
            ...PLAN,
            kind: "migrate",
            ops: [
              {
                op: "delete",
                name: "old",
                color: null,
                description: null,
                replacement: null,
                members: [],
                skip_cause: null,
              },
            ],
          }),
      },
      [repo({ migrated: true, settings_schema: SCHEMA, schema_source: "settings" })],
    );
    const web = await repoGroup();
    expect(web.getByText(/This repo has been migrated/)).toBeInTheDocument();
    const cli = web.getByRole("textbox", { name: "Aliases of area:cli" });
    await userEvent.type(cli, "{Enter}command line");
    await userEvent.click(web.getByRole("button", { name: "Add area" })); // empty: nothing added
    await userEvent.click(web.getByRole("button", { name: "Save labels for acme/web" }));
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/repos/acme/web")).toHaveLength(1));
    const body = (await bodyOf(sent(calls, "PATCH", "/api/issues/repos/acme/web")[0] as Request)) as {
      settings_schema: Schema;
    };
    expect(body.settings_schema.areas).toEqual([value("area:cli", ["cli", "command line"])]);
    const saved = await repoGroup(); // the saved schema starts the form again
    await userEvent.click(saved.getByRole("button", { name: "Preview migration for acme/web" }));
    const plan = await saved.findByRole("region", { name: "Migration for acme/web" });
    expect(within(plan).getByRole("listitem")).toHaveTextContent("Delete old");
  });

  it("says nothing needs migrating", async () => {
    renderIssues({ "POST /api/issues/labels/acme/web/preview": () => json({ ...PLAN, kind: "migrate", ops: [] }) });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview migration for acme/web" }));
    expect(await web.findByText("Nothing to do: no alias exists on GitHub.")).toBeInTheDocument();
  });
});

describe("Settings → Issues: the suggestion runner", () => {
  it("shows a runner save that failed", async () => {
    renderIssues({
      "PATCH /api/issues/settings": () => json({ error: "x", detail: "issues module not started" }, 503),
    });
    const issues = await section();
    await userEvent.selectOptions(await issues.findByRole("combobox", { name: "Suggestions runner" }), "codex");
    expect((await screen.findAllByText("Couldn't save the runner: issues module not started")).length).toBeGreaterThan(
      0,
    );
  });

  it("offers the available runners and saves only the runner", async () => {
    const calls = renderIssues({
      "PATCH /api/issues/settings": async (req: Request) => json(await req.clone().json()),
    });
    const issues = await section();
    const picker = await issues.findByRole("combobox", { name: "Suggestions runner" });
    expect(picker).toHaveValue("");
    await userEvent.selectOptions(picker, "codex");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/settings")).toHaveLength(1));
    const [req] = sent(calls, "PATCH", "/api/issues/settings");
    expect(await bodyOf(req as Request)).toEqual({ runner: "codex" });
    expect(req?.headers.get("Idempotency-Key")).toBeTruthy();
    await waitFor(() => expect(picker).toHaveValue("codex"));
    await userEvent.selectOptions(picker, "");
    await waitFor(() => expect(sent(calls, "PATCH", "/api/issues/settings")).toHaveLength(2));
    expect(await bodyOf(sent(calls, "PATCH", "/api/issues/settings")[1] as Request)).toEqual({ runner: null });
  });
});

describe("Settings → Issues: final review minors", () => {
  it("M1: names hidden characters in accessible names and in untrusted causes", async () => {
    const rlo = String.fromCodePoint(0x202e);
    const sneaky = { ...SCHEMA, areas: [value(`area:a${rlo}b`)] };
    renderIssues({}, [
      repo({
        schema_source: "settings",
        settings_schema: sneaky,
        label_schema: sneaky,
        sync_error: `bad${rlo}sync`,
        schema_errors: [`areas[1]: x${rlo}y`],
      }),
    ]);
    const web = await repoGroup();
    expect(web.getByRole("textbox", { name: "Aliases of area:a⟪U+202E⟫b" })).toBeInTheDocument();
    expect(web.getByRole("button", { name: "Remove area:a⟪U+202E⟫b" })).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "acme/web" });
    expect(group).toHaveTextContent("Last sync failed: bad⟪U+202E⟫sync");
    expect(group).toHaveTextContent("areas[1]: x⟪U+202E⟫y");
  });

  it("M2: a failed settings read is a load error, never a wrong Automatic; the option says Claude Code", async () => {
    let fail = true;
    renderIssues({
      "GET /api/issues/settings": () =>
        fail ? json({ error: "x", detail: "settings unreadable" }, 500) : json({ runner: "codex" }),
    });
    const issues = await section();
    expect(await issues.findByText(/Couldn't load the Issues settings: settings unreadable/)).toBeInTheDocument();
    fail = false;
    await userEvent.click(issues.getByRole("button", { name: "Try again" }));
    const picker = await issues.findByRole("combobox", { name: "Suggestions runner" });
    expect(picker).toHaveValue("codex");
    expect(within(picker).getByRole("option", { name: "Automatic (Claude Code when available)" })).toBeInTheDocument();
  });

  it("M3: a refused apply offers Preview again instead of replaying the refusal", async () => {
    const calls = renderIssues({
      "POST /api/issues/labels/acme/web/preview": () => json(PLAN),
      "POST /api/issues/labels/acme/web/apply": () =>
        json({ error: "LabelPlanExpiredError", detail: "the plan expired; preview again" }, 409),
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    const plan = await web.findByRole("region", { name: "Setup for acme/web" });
    await userEvent.click(within(plan).getByRole("button", { name: "Apply setup" }));
    expect(await within(plan).findByRole("alert")).toHaveTextContent("the plan expired; preview again");
    expect(within(plan).queryByRole("button", { name: "Apply setup" })).toBeNull();
    await userEvent.click(within(plan).getByRole("button", { name: "Preview again" }));
    await waitFor(() => expect(sent(calls, "POST", "/api/issues/labels/acme/web/preview")).toHaveLength(2));
  });

  it("M3: an apply that got no answer keeps Apply, with the same key, to retry", async () => {
    let attempt = 0;
    const calls = renderIssues({
      "POST /api/issues/labels/acme/web/preview": () => json(PLAN),
      "POST /api/issues/labels/acme/web/apply": () =>
        attempt++ === 0
          ? Promise.reject(new TypeError("Failed to fetch"))
          : json({ repo: "acme/web", kind: "setup", complete: true, steps: [], note: null, stopped: null, rate_limited: false, rate_limited_until: null }), // prettier-ignore
    });
    const web = await repoGroup();
    await userEvent.click(web.getByRole("button", { name: "Preview setup for acme/web" }));
    const plan = await web.findByRole("region", { name: "Setup for acme/web" });
    await userEvent.click(within(plan).getByRole("button", { name: "Apply setup" }));
    expect(await within(plan).findByRole("alert")).toHaveTextContent("Failed to fetch");
    await userEvent.click(within(plan).getByRole("button", { name: "Apply setup" }));
    await within(plan).findByText("Setup applied: every step done.");
    const keys = sent(calls, "POST", "/api/issues/labels/acme/web/apply").map((r) => r.headers.get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("M4: the watched box and the runner wait for their save", async () => {
    let release: (r: Response) => void = () => undefined;
    renderIssues({
      "PATCH /api/issues/repos/acme/web": () => new Promise<Response>((resolve) => (release = resolve)),
      "PATCH /api/issues/settings": () => new Promise<Response>(() => undefined),
    });
    const web = await repoGroup();
    const box = web.getByRole("checkbox", { name: "Watched" });
    await userEvent.click(box);
    expect(box).toBeDisabled();
    release(json(repo({ watched: false })));
    await waitFor(() => expect(web.getByRole("checkbox", { name: "Watched" })).not.toBeDisabled());
    const picker = (await section()).getByRole("combobox", { name: "Suggestions runner" });
    await userEvent.selectOptions(picker, "codex");
    expect(picker).toBeDisabled();
  });

  it("M5: Add area refuses a name without area: and a duplicate", async () => {
    renderIssues();
    const web = await repoGroup();
    const field = web.getByRole("textbox", { name: "New area" });
    await userEvent.type(field, "wiki");
    await userEvent.click(web.getByRole("button", { name: "Add area" }));
    expect(await web.findByText("An area starts with area:.")).toBeInTheDocument();
    await userEvent.clear(field);
    await userEvent.type(field, "AREA:CLI");
    await userEvent.click(web.getByRole("button", { name: "Add area" }));
    expect(await web.findByText("area:cli is already an area.")).toBeInTheDocument();
    expect(web.getAllByRole("button", { name: /^Remove area:/ })).toHaveLength(1);
  });

  it("M6: the previews wait while alias edits are unsaved", async () => {
    renderIssues();
    const web = await repoGroup();
    await userEvent.type(web.getByRole("textbox", { name: "Aliases of agent-ready" }), "ready");
    expect(web.getByRole("button", { name: "Preview migration for acme/web" })).toBeDisabled();
    expect(web.getByRole("button", { name: "Preview setup for acme/web" })).toBeDisabled();
    expect(web.getByText("Save labels first: the previews plan from the saved schema.")).toBeInTheDocument();
  });
});
