// SPDX-License-Identifier: AGPL-3.0-or-later
// Shapes of the server's untyped (dict) responses the shell reads.

export type ModuleHealth = { ok: boolean; detail: string };
export type Health = { ok: boolean; modules: Record<string, ModuleHealth> };

export type GitMode = "off" | "commit" | "push";
export type SyncOutcome = "skipped" | "committed" | "pushed" | "conflict" | "blocked" | "error";
export type SyncStatus = {
  state: {
    mode: GitMode;
    branch: string | null;
    upstream: string | null;
    ahead: number;
    behind: number;
    dirty: boolean;
    problem: string | null;
    problem_text: string | null;
  };
  paused: boolean;
  last_run: string | null;
  last_result: { outcome: SyncOutcome; message: string } | null;
  next_run: string | null;
  consecutive_failures: number;
};

export type RunnerInfo = { available: boolean; version: string | null; detail: string };
export type Runners = Record<string, RunnerInfo>;

export type Conflict = { kind: string; id: string; paths: string[]; detected_at: string };

export type KbPathInfo = { path: string | null; problem: string | null; pages: number };
