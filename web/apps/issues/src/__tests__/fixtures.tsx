// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes the Issues API answers with (`/api/issues`), for the app's tests.
import { baseRoutes, fakeClient, type Routes } from "@jesinia/shell";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { expect, vi } from "vitest";
import type {
  ApplyBatch,
  Epic,
  EpicDetail,
  IssueDetail,
  IssueRow,
  IssueView,
  Member,
  Milestone,
  Pending,
  Queue,
  QueueItem,
  QueueSummary,
  Readiness,
  Repo,
  Suggestion,
  SuggestionList,
  Theme,
} from "../api";
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
    "GET /api/issues/themes": { themes: [] },
    "GET /api/issues/epics": { epics: [] },
    "GET /api/issues/queues": { queues: [] },
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

// --- epics, queues and themes (Task 13) --------------------------------------------------------

export function readiness(over: Partial<Readiness> = {}): Readiness {
  return { ready: false, blockers: [], cross_repo: false, warning: null, note: null, ...over };
}

export function epic(n: number, over: Partial<Epic> = {}): Epic {
  return {
    repo: REPO,
    number: n,
    title: `Epic ${n}`,
    url: `https://github.com/${REPO}/issues/${n}`,
    labels: ["epic", "priority: high"],
    parent: null,
    priority: "priority: high",
    is_initiative: false,
    lacks_label: false,
    done: 1,
    total: 3,
    readiness: readiness({ blockers: [{ repo: REPO, n: 1, reason: "needs readiness" }] }),
    updated_at: NOW,
    ...over,
  };
}

export function member(n: number, over: Partial<Member> = {}): Member {
  return {
    repo: REPO,
    number: n,
    cached: true,
    title: `Child ${n}`,
    url: `https://github.com/${REPO}/issues/${n}`,
    state: "open",
    readiness: "agent-ready",
    complete: true,
    blocker: null,
    warning: null,
    ...over,
  };
}

export function epicDetail(n: number, over: Partial<EpicDetail> = {}): EpicDetail {
  return {
    epic: epic(n),
    body: "The epic's body.",
    children: [member(3), member(4, { readiness: null, complete: false, blocker: "needs readiness" })],
    brief: null,
    default_brief: `# Epic ${n}\n\n## Children, in order\n\n1. ${REPO}#3\n2. ${REPO}#4`,
    checklist_items: 0,
    ...over,
  };
}

export function queueItem(n: number, over: Partial<QueueItem> = {}): QueueItem {
  return {
    kind: "issue",
    repo: REPO,
    n,
    position: 0,
    note: "",
    added_at: NOW,
    done_at: null,
    done_pending: null,
    title: `Issue ${n}`,
    state: "open",
    url: `https://github.com/${REPO}/issues/${n}`,
    priority: "priority: medium",
    pending_priority: null,
    readiness: readiness({ ready: true }),
    warning: null,
    ...over,
  };
}

export function queue(id: string, items: QueueItem[], over: Partial<Queue> = {}): Queue {
  const placed = items.map((item, i) => ({ ...item, position: i }));
  const values = ["priority: high", "priority: medium", "priority: low"];
  return {
    id,
    name: "Next up",
    description: "",
    position: 0,
    created_at: NOW,
    items: placed,
    done: [],
    bands: [
      ...values.map((value) => ({ value, items: placed.filter((i) => i.priority === value) })),
      { value: null, items: placed.filter((i) => i.priority === null) },
    ],
    ...over,
  };
}

export function queueSummary(id: string, over: Partial<QueueSummary> = {}): QueueSummary {
  return { id, name: "Next up", description: "", position: 0, open: 2, done: 0, warnings: 0, ...over };
}

export function theme(tag: string, over: Partial<Theme> = {}): Theme {
  return { tag, count: 1, publishable: true, ...over };
}

// --- driving the drag primitives by keyboard -----------------------------------------------------

export const said = () => document.querySelector("[data-dnd-announcer]")?.textContent ?? "";

export async function pickUp(name: string): Promise<UserEvent> {
  const user = userEvent.setup();
  (await screen.findByRole("button", { name })).focus();
  await user.keyboard("[Space]");
  await waitFor(() => expect(said()).toMatch(/^Picked up/));
  return user;
}

/** Arrow through the targets until the announcer says we are over (or refused by) `target`. */
export async function moveTo(user: UserEvent, target: string | RegExp) {
  const matches = (text: string) => (typeof target === "string" ? text === target : target.test(text));
  const reached = () =>
    matches(
      said()
        .replace(/^Over /, "")
        .replace(/\.$/, ""),
    ) ||
    (typeof target === "string" && said().startsWith(`Can't drop on ${target}:`));
  for (const key of ["[ArrowDown]", "[ArrowUp]"]) {
    for (let i = 0; i < 30; i += 1) {
      await user.keyboard(key);
      if (reached()) return said();
    }
  }
  throw new Error(`never reached ${String(target)}; last said: ${said()}`);
}

/** Never leave the keyboard sensor mid-drag for the next test. */
export async function settleDrag() {
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape", code: "Escape" });
}

/** A route that records each staged change and lists them back. */
export function stagingRoutes(staged: Pending[] = []): Routes {
  return {
    "GET /api/issues/pending": () => json({ changes: staged }),
    "POST /api/issues/pending": async (req: Request) => {
      const body = (await req.json()) as Pick<Pending, "repo" | "n" | "change" | "source">;
      const made = pending(`p${staged.length + 1}`, body);
      staged.push(made);
      return json(made, 201);
    },
  };
}

export function milestoneDetail(brief: string): Milestone {
  return {
    milestone: {
      repo: REPO,
      number: 5,
      title: "v1",
      state: "open",
      due_on: null,
      open_issues: 1,
      readiness: readiness(),
    },
    issues: [],
    brief,
  };
}
