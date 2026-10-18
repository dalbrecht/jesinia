// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes the Issues API answers with (`/api/issues`), for the app's tests.
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { act, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { ApplyBatch, IssueDetail, IssueRow, IssueView, Pending, Repo, Suggestion, SuggestionList } from "../api";
import { App } from "../App";

export const REPO = "acme/app";
export const NOW = "2026-10-18T12:00:00Z";
const canon = (name: string) => ({ name, aliases: [] });

export function repo(over: Partial<Repo> = {}): Repo {
  return {
    repo: REPO,
    watched: true,
    added_at: NOW,
    schema_source: "default",
    label_schema: {
      readiness: ["agent-ready", "human-needed", "information-needed", "design-needed", "blocked", "icebox"].map(canon),
      priority: ["priority: high", "priority: medium", "priority: low"].map(canon),
      type: ["bug", "enhancement", "documentation"].map(canon),
      areas: ["area:web", "area:api"].map(canon),
      epic: canon("epic"),
      initiative: canon("initiative"),
      free_prefixes: ["review:", "campaign:", "theme:"],
    },
    schema_errors: [],
    settings_schema: null,
    migrated: false,
    synced_at: NOW,
    sync_error: null,
    ...over,
  };
}

export function row(n: number, over: Partial<IssueRow> = {}): IssueRow {
  return {
    repo: REPO,
    number: n,
    title: `Issue ${n}`,
    url: `https://github.com/${REPO}/issues/${n}`,
    author: "octo",
    labels: ["bug", "area:web"],
    readiness: null,
    priority: "priority: medium",
    type: "bug",
    areas: ["area:web"],
    complete: false,
    missing: ["readiness"],
    conflicts: [],
    is_epic: false,
    sub_issue_count: 0,
    parent: null,
    milestone: null,
    created_at: NOW,
    updated_at: NOW,
    state: "open",
    linked_pr_state: null,
    ...over,
  };
}

export const ROW_1 = row(1, { title: "Fix login" });
export const ROW_2 = row(2, {
  title: "Add export",
  labels: ["enhancement", "agent-ready", "priority: high", "area:api"],
  readiness: "agent-ready",
  priority: "priority: high",
  type: "enhancement",
  areas: ["area:api"],
  complete: true,
  missing: [],
});

export function view(rows: IssueRow[] = [ROW_1, ROW_2], over: Partial<IssueView> = {}): IssueView {
  return { rows, synced_at: NOW, syncing: false, sync_error: null, ...over };
}

export function detail(over: Partial<IssueDetail> = {}): IssueDetail {
  const { title, ...base } = ROW_1;
  return {
    ...base,
    title,
    body: "Login **fails** on Safari.\n\n<script>alert(1)</script>",
    state_reason: null,
    inbox: true,
    children: null,
    comments: [
      { id: 7, author: "dev", body: "Seen it too.", created_at: NOW, url: "https://github.com/acme/app/issues/1#c7" },
    ],
    timeline: [
      {
        id: 8,
        event: "labeled",
        actor: "octo",
        created_at: NOW,
        label: "bug",
        state_reason: null,
        source: null,
        commit_id: null,
      },
    ],
    details_synced_at: NOW,
    ...over,
  };
}

export function suggestion(id: string, over: Partial<Suggestion> = {}): Suggestion {
  return {
    id,
    repo: REPO,
    n: 1,
    scope: null,
    kind: "label",
    payload: { dimension: "readiness", value: "agent-ready" },
    reason: "The steps to reproduce are clear.",
    state: "draft",
    created_at: NOW,
    updated_at: NOW,
    apply_error: null,
    ...over,
  };
}

export const S_READY = suggestion("s1");
export const S_DUP = suggestion("s2", {
  kind: "duplicate",
  payload: { repo: REPO, n: 2, similarity: 0.8 },
  reason: "Same crash.",
});

export function suggestions(list: Suggestion[] = [S_READY, S_DUP], over: Partial<SuggestionList> = {}): SuggestionList {
  return { suggestions: list, run: null, ask_run: null, ...over };
}

export function pending(id: string, over: Partial<Pending> = {}): Pending {
  return {
    id,
    repo: REPO,
    n: 1,
    change: { kind: "set_dimension", dimension: "priority", value: "priority: high" },
    source: "picker",
    created_at: NOW,
    ...over,
  };
}

export function batch(id: string, over: Partial<ApplyBatch> = {}): ApplyBatch {
  return {
    id,
    origin: "pending",
    status: "running",
    complete: false,
    repo_steps: [],
    issues: [
      {
        repo: REPO,
        n: 1,
        temp_id: null,
        steps: [{ kind: "labels", state: "skipped", cause: "not run yet", conflict: false }],
      },
    ],
    created: [],
    stopped: null,
    rate_limited: false,
    rate_limited_until: null,
    ...over,
  };
}

export const DONE_STEP = { kind: "labels" as const, state: "done" as const, cause: null, conflict: false };

/** A finished batch: every issue's steps done. */
export function finished(id: string, over: Partial<ApplyBatch> = {}): ApplyBatch {
  return batch(id, {
    status: "finished",
    complete: true,
    issues: [{ repo: REPO, n: 1, temp_id: null, steps: [DONE_STEP] }],
    ...over,
  });
}

/** The routes every Issues screen reads, with `over` replacing or adding some. */
export function issuesRoutes(over: Routes = {}): Routes {
  return baseRoutes({
    "GET /api/issues/repos": { repos: [repo()] },
    "GET /api/issues/inbox": view([ROW_1]),
    "GET /api/issues/backlog": view([ROW_1, ROW_2]),
    "GET /api/issues/pending": { changes: [] },
    "GET /api/issues/batches": { batches: [] },
    "GET /api/issues/suggestions": suggestions(),
    "GET /api/issues/item/acme/app/1": detail(),
    "POST /api/issues/item/acme/app/1/refresh": detail(),
    ...over,
  });
}

export const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

/** Render the app at `path` against `routes`. */
export function renderApp(routes: Routes, path = "/issues/") {
  window.history.replaceState(null, "", path);
  // Routes read a copy of each request, so a test can still read the body of the one recorded.
  const copying = Object.fromEntries(
    Object.entries(routes).map(([key, route]) => [
      key,
      typeof route === "function"
        ? (req: Request) => (route as (r: Request) => Response | Promise<Response>)(req.clone())
        : route,
    ]),
  );
  const fake = fakeClient(copying);
  const view = render(<App client={fake.client} />);
  return { ...fake, ...view };
}

/** The requests made to `method path`. */
export const callsTo = (calls: Request[], method: string, path: string) =>
  calls.filter((c) => c.method === method && new URL(c.url).pathname === path);

/** Let fake timers run `ms` and the promises they settle finish. */
export async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** A toast saying `text` (shown, and repeated in the shell's live region). */
export async function toast(text: RegExp): Promise<boolean> {
  return (await screen.findAllByText(text)).length > 0;
}
