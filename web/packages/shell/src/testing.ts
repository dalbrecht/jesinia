// SPDX-License-Identifier: AGPL-3.0-or-later
// Test helpers: a JesiniaClient backed by an in-memory route table.
import { createClient, type JesiniaClient } from "@jesinia/client";
import type { Health, SyncStatus } from "./types";

export type Route = (req: Request) => Response | Promise<Response>;
export type Routes = Record<string, Route | object>;

/** Routes keyed by "METHOD /path"; a plain object is returned as JSON. */
export function fakeClient(routes: Routes): { client: JesiniaClient; calls: Request[] } {
  const calls: Request[] = [];
  const client = createClient({
    baseUrl: "http://127.0.0.1:4870",
    fetch: async (req: Request) => {
      calls.push(req);
      const key = `${req.method} ${new URL(req.url).pathname}`;
      const route = routes[key];
      if (route === undefined) return Response.json({ detail: `no route ${key}` }, { status: 404 });
      return typeof route === "function" ? (route as Route)(req) : Response.json(route);
    },
  });
  return { client, calls };
}

export const HEALTHY: Health = {
  ok: true,
  modules: { kb: { ok: true, detail: "12 pages indexed" }, kbsync: { ok: true, detail: "mode push" } },
};

export function syncStatus(over: Partial<SyncStatus> = {}, state: Partial<SyncStatus["state"]> = {}): SyncStatus {
  return {
    state: {
      mode: "push",
      branch: "main",
      upstream: "origin/main",
      ahead: 0,
      behind: 0,
      dirty: false,
      problem: null,
      problem_text: null,
      ...state,
    },
    paused: false,
    last_run: null,
    last_result: null,
    next_run: "2026-10-16T18:05:00+00:00",
    consecutive_failures: 0,
    ...over,
  };
}

export function baseRoutes(over: Routes = {}): Routes {
  return {
    "GET /healthz": HEALTHY,
    "GET /api/kbsync/status": syncStatus(),
    "GET /api/kb/conflicts": [],
    "GET /api/agents/runners": { claude: { available: true, version: "2.1.301", detail: "ok" } },
    "GET /api/kb/path": { path: "/Users/me/kb", problem: null, pages: 12 },
    ...over,
  };
}
