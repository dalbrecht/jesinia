// SPDX-License-Identifier: AGPL-3.0-or-later
// A reviewed PR as `GET /api/review/prs/…` answers it once every job has run (spec §3.1), and
// the drafts and post previews that go with it.
import type { components } from "@jesinia/client";
import type { PrView, QueueRow } from "../api";
import { prView, row } from "./fixtures";

type Drafts = components["schemas"]["DraftsOut"];
type Preview = components["schemas"]["PreviewOut"];

export const HEAD = "a".repeat(40);
export const NEWER = "9f8e7d6".padEnd(40, "0");
export const PATH = "/review/acme/web/3";
export const API = "/api/review/prs/acme/web/3";
export const XSS = "<img src=x onerror=alert(1)>";

export const PRORATION = [
  "@@ -0,0 +1,3 @@",
  "+def prorate(sub):",
  "+    used = 1",
  "+    return used",
  "@@ -10,3 +13,4 @@ def apply():",
  " # keep",
  "-    sub.plan = new",
  "+    amount = prorate(sub)",
  `+    ledger.post(amount)  # ${XSS}`,
  " done()",
].join("\n");

export const REVIEWED: QueueRow = row(3, {
  title: "billing: prorate mid-cycle",
  depth: "deep",
  recommendation: "needs_closer_look",
  size_lines: 14,
  files: 3,
  coverage_pct: 82,
});

const WALK_COVERAGE = {
  pct: 82,
  files_covered: 1,
  files_total: 3,
  lines_covered: 9,
  lines_total: 11,
  lines_auto_skipped: 2,
  reviewable_pct: 90,
  dropped_steps: 0,
  complete: true,
  incomplete_reason: null,
  not_walked: [
    { path: "poetry.lock", hunk: null, reason: "lockfile" },
    { path: "docs/billing.md", hunk: 0, reason: "judged_mechanical" },
  ],
  not_walked_total: 2,
  nothing_to_walk: false,
};

export const FINDINGS = [
  {
    group: "correctness",
    severity: "important",
    path: "src/billing/proration.py",
    start_line: 14,
    end_line: 15,
    explanation: "Rounding happens once on the difference.",
    rule: { id: "python.md › Money", layer: "base" },
  },
  {
    group: "style",
    severity: "minor",
    path: "src/billing/proration.py",
    start_line: 1,
    end_line: 1,
    explanation: "Name it `prorate_amount`.",
    rule: null,
  },
];

const DONE = { state: "done", error: null };

export const ARTIFACTS = {
  overview: {
    summary: "Charges the difference **at once**.",
    purpose: "Customers asked to change plans mid-cycle.",
    sources: ["PR body"],
  },
  background: {
    sections: [
      {
        title: "Billing today",
        body: "Plans change at renewal.",
        code: [{ path: "src/billing/" }],
        kb: ["billing.md"],
      },
    ],
  },
  walkthrough: {
    steps: [
      {
        title: "The new prorate function",
        body: "It computes the share.",
        hunks: [{ path: "src/billing/proration.py", hunk: 0 }],
      },
      {
        title: "Posting to the ledger",
        body: `The change is posted. ${XSS}`,
        hunks: [{ path: "src/billing/proration.py", hunk: 1 }],
      },
    ],
    skipped: [],
  },
  coverage: {
    pct: 82,
    source: "walkthrough",
    complete: true,
    incomplete_reason: null,
    nothing_to_walk: false,
    walkthrough: WALK_COVERAGE,
    correctness: { ...WALK_COVERAGE, pct: 90, not_walked: [] },
  },
  findings: { findings: FINDINGS },
  correctness: {
    summary: "Read every changed line.",
    checked: [{ path: "src/billing/proration.py", start_line: 1, end_line: 16 }],
    findings: [],
    tests: [{ path: "tests/test_proration.py", name: "test_prorate" }],
  },
  metrics: {
    scores: [
      {
        criterion: "Error handling",
        rule: { id: "python.md › Errors", layer: "base" },
        score: 7,
        note: "Mostly typed.",
      },
    ],
  },
  quiz: {
    questions: [
      {
        question: "When is the change charged?",
        choices: ["At renewal", "At once"],
        answer: 1,
        explanation: "Right away.",
      },
      {
        question: "What rounds?",
        choices: ["The difference", "Each side"],
        answer: 0,
        explanation: "Once, on the difference.",
      },
      { question: "Where is it posted?", choices: ["The ledger", "Nowhere"], answer: 0, explanation: "ledger.post." },
    ],
  },
  recommendation: { verdict: "needs_closer_look", reasoning: "One important finding." },
  verdict: { verdict: "needs_closer_look", reasons: ["the correctness pass found 1 important issue"] },
  evidence: {
    ci: "passing",
    checks: [{ name: "ci / test", status: "passing" }],
    tests: "1 test covers the change",
    findings: { important: 1, minor: 1 },
    correctness: "complete",
    depth: "deep",
    correctness_coverage: { pct: 90, reviewable_pct: 92, complete: true, claimed_pct: 95, claimed_reviewable_pct: 96 },
  },
  jobs: Object.fromEntries(
    ["correctness", "findings", "recommendation", "overview", "background", "walkthrough", "metrics", "quiz"].map(
      (k) => [k, DONE],
    ),
  ),
} satisfies Record<string, unknown>;

export const FILES = [
  {
    path: "src/billing/proration.py",
    status: "modified",
    additions: 6,
    deletions: 1,
    patch: PRORATION,
    previous_path: null,
  },
  {
    path: "poetry.lock",
    status: "modified",
    additions: 1,
    deletions: 1,
    patch: "@@ -1 +1 @@\n-a = 1\n+a = 2",
    previous_path: null,
  },
  {
    path: "docs/billing.md",
    status: "modified",
    additions: 1,
    deletions: 1,
    patch: "@@ -1 +1 @@\n-teh\n+the",
    previous_path: null,
  },
];

export const ROUND = {
  id: 3,
  head_sha: HEAD,
  base_sha: "b".repeat(40),
  depth: "deep" as const,
  started_at: "2026-10-17T10:00:00Z",
  finished_at: "2026-10-17T10:05:00Z",
  verdict: "needs_closer_look",
  error: null,
  posted_review_url: null,
  done: false,
};

export function reviewed(over: Partial<PrView> = {}, rowOver: Partial<QueueRow> = {}): PrView {
  return prView({ ...REVIEWED, ...rowOver }, { round: ROUND, artifacts: ARTIFACTS, files: FILES, ...over });
}

export function drafts(over: Partial<Drafts> = {}): Drafts {
  return {
    round_id: 3,
    summary: "Looks good overall.",
    drafts: [
      {
        id: 1,
        path: "src/billing/proration.py",
        body: "Rounding happens once.",
        line: 15,
        side: "RIGHT",
        start_line: 14,
        start_side: "RIGHT",
        origin_finding: "f1",
        outdated: false,
        outside_diff: false,
        in_body: false,
      },
    ],
    files_pending: false,
    findings_pending: false,
    seeds_dropped: 0,
    ...over,
  };
}

export function preview(over: Partial<Preview> = {}): Preview {
  return {
    round_id: 3,
    head_sha: HEAD,
    event: "APPROVE",
    body: "Looks good overall.",
    comments: [
      {
        path: "src/billing/proration.py",
        line: 15,
        side: "RIGHT",
        body: "nonblocking: rounding",
        start_line: 14,
        start_side: "RIGHT",
      },
    ],
    warnings: [],
    allowed_events: ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
    digest: "digest-1",
    notes: [],
    pr_head_sha: HEAD,
    ...over,
  };
}
