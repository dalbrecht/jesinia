// SPDX-License-Identifier: AGPL-3.0-or-later
// The PR view's pure rules: reading a round's artifacts (agent output, schema-checked by the
// server, so read here only for shape), the book's outline, a patch's hunks, and the words
// for coverage and the verdict. Every string read here is the PR's or an agent's text: it is
// only ever rendered as text or through the safe Markdown renderer.
import type { PrFile, PrView } from "../api";

export type Depth = "quick" | "deep";
export type Rule = { id: string; layer: string };
export type Finding = {
  group: "correctness" | "design" | "style";
  severity: "critical" | "important" | "minor";
  path: string;
  start_line: number;
  end_line: number;
  explanation: string;
  rule: Rule | null;
};
export type HunkRef = { path: string; hunk: number };
export type Step = { title: string; body: string; hunks: HunkRef[] };
export type Skip = { path: string; hunk: number | null; reason: string };
export type WalkCoverage = {
  pct: number | null;
  files_covered: number;
  files_total: number;
  not_walked: Skip[];
  not_walked_total: number;
  nothing_to_walk: boolean;
};
export type Overview = { summary: string; purpose: string; sources: string[] };
export type BackgroundSection = { title: string; body: string; code: { path: string }[]; kb: string[] };
export type Score = { criterion: string; rule: Rule; score: number; note: string };
export type Question = { question: string; choices: string[]; answer: number; explanation: string };
export type Evidence = {
  ci: string;
  checks: { name: string; status: string }[];
  tests: string | null;
  findings: Record<string, number>;
  correctness: string | null;
  correctness_coverage: { pct: number | null } | null;
};
export type JobState = { state: string; error: string | null };

/** One of the author's comments in their previous review (spec §4.2), as Jesinia stored it. */
export type PreviousComment = {
  ref: string;
  body: string;
  path: string | null;
  line: number | null;
  side: "LEFT" | "RIGHT" | null;
  start_line: number | null;
  resolved: boolean;
  /** Where it sits on this head (I2): the path after any rename, the line its text is on now. */
  now_path: string | null;
  now_line: number | null;
  now_start_line: number | null;
  /** Its text changed since: shown as outdated, never placed by its old line. */
  outdated: boolean;
  /** Why it couldn't be placed on this head at all (unknown, not changed), when it couldn't. */
  unplaced?: string | null;
};
/** A step of the previous round's walkthrough, on this head's unchanged hunks. */
export type PreviousStep = {
  title: string;
  body: string;
  now_hunks: HunkRef[];
  changed: number;
  unplaced?: string | null;
};
/** A finding of the previous round, placed on this head's lines (or outdated). */
export type PreviousFinding = Finding & {
  now_path: string | null;
  now_start_line: number | null;
  now_end_line: number | null;
  outdated: boolean;
  unplaced?: string | null;
};
export type PreviousReview = {
  source: "round" | "github";
  head_sha: string;
  round_id: number | null;
  comments: PreviousComment[];
  steps: PreviousStep[];
  findings: PreviousFinding[];
  /** What couldn't be placed on this head, and why (Jesinia's words). */
  placement_error: string | null;
};
export type RequestStatus = "addressed" | "partly" | "not_addressed";
export type Request = { comment_ref: string; status: RequestStatus; explanation: string; hunks: HunkRef[] };
/** The re-review job's result as Jesinia kept it: known comments, known hunks, new steps only. */
export type Rereview = {
  requests: Request[];
  unanswered: string[];
  new_hunks: HunkRef[];
  steps: Step[];
  coverage: WalkCoverage | null;
};

/** A round's artifacts, by what the PR view shows; null where a job hasn't stored one. */
export type Artifacts = {
  overview: Overview | null;
  background: BackgroundSection[] | null;
  steps: Step[] | null;
  walk: WalkCoverage | null;
  /** The correctness pass's own coverage (what it verifiably read), when there is no walk. */
  read: WalkCoverage | null;
  findings: Finding[] | null;
  metrics: Score[] | null;
  quiz: Question[] | null;
  suggestion: { verdict: string; reasoning: string } | null;
  verdict: { verdict: string; reasons: string[] } | null;
  evidence: Evidence | null;
  jobs: Record<string, JobState>;
  nothingToWalk: boolean;
  /** A re-review's head (spec §4.2): the previous review it builds on, and the job's result. */
  isRereview: boolean;
  previous: PreviousReview | null;
  rereview: Rereview | null;
};

const REREVIEW_KINDS = new Set(["rereview", "changed_hunks_only"]);

type Json = Record<string, unknown>;
const record = (x: unknown): Json | null =>
  x !== null && typeof x === "object" && !Array.isArray(x) ? (x as Json) : null;
const list = <T>(x: unknown): T[] | null => (Array.isArray(x) ? (x as T[]) : null);

function coverageOf(x: unknown): WalkCoverage | null {
  const c = record(x);
  if (!c) return null;
  return {
    pct: typeof c.pct === "number" ? c.pct : null,
    files_covered: Number(c.files_covered ?? 0),
    files_total: Number(c.files_total ?? 0),
    not_walked: list<Skip>(c.not_walked) ?? [],
    not_walked_total: Number(c.not_walked_total ?? 0),
    nothing_to_walk: c.nothing_to_walk === true,
  };
}

/** Reads what the PR view shows out of `GET /prs/…`'s artifacts (shapes per `schemas/*.json`). */
export function readArtifacts(view: PrView): Artifacts {
  const a = view.artifacts as Json;
  const coverage = record(a.coverage);
  const found = [
    ...(list<Finding>(record(a.correctness)?.findings) ?? []),
    ...(list<Finding>(record(a.findings)?.findings) ?? []),
  ];
  const haveFindings = record(a.correctness) !== null || record(a.findings) !== null;
  const suggestion = record(a.recommendation);
  const verdict = record(a.verdict);
  const jobs: Record<string, JobState> = {};
  for (const [kind, entry] of Object.entries(record(a.jobs) ?? {})) {
    const e = record(entry);
    if (e) jobs[kind] = { state: String(e.state), error: typeof e.error === "string" ? e.error : null };
  }
  return {
    overview: (record(a.overview) as Overview | null) ?? null,
    background: list<BackgroundSection>(record(a.background)?.sections),
    steps: list<Step>(record(a.walkthrough)?.steps),
    walk: coverageOf(coverage?.walkthrough),
    read: coverageOf(coverage?.correctness),
    findings: haveFindings ? dedupe(found) : null,
    metrics: list<Score>(record(a.metrics)?.scores),
    quiz: list<Question>(record(a.quiz)?.questions),
    suggestion: suggestion ? { verdict: String(suggestion.verdict), reasoning: String(suggestion.reasoning) } : null,
    verdict: verdict ? { verdict: String(verdict.verdict), reasons: list<string>(verdict.reasons) ?? [] } : null,
    evidence: (record(a.evidence) as Evidence | null) ?? null,
    jobs,
    nothingToWalk: view.nothing_to_walk || coverage?.nothing_to_walk === true,
    isRereview:
      REREVIEW_KINDS.has(String(record(a.followup)?.kind)) || "rereview" in jobs || record(a.previous_review) !== null,
    previous: previousOf(a.previous_review),
    rereview: rereviewOf(a.rereview),
  };
}

function previousOf(x: unknown): PreviousReview | null {
  const p = record(x);
  if (!p) return null;
  return {
    source: p.source === "round" ? "round" : "github",
    head_sha: String(p.head_sha ?? ""),
    round_id: typeof p.round_id === "number" ? p.round_id : null,
    comments: list<PreviousComment>(p.comments) ?? [],
    steps: list<PreviousStep>(p.steps) ?? [],
    findings: list<PreviousFinding>(p.findings) ?? [],
    placement_error: typeof p.placement_error === "string" ? p.placement_error : null,
  };
}

function rereviewOf(x: unknown): Rereview | null {
  const r = record(x);
  if (!r) return null;
  return {
    requests: list<Request>(r.requests) ?? [],
    unanswered: list<string>(r.unanswered) ?? [],
    new_hunks: list<HunkRef>(r.new_hunks) ?? [],
    steps: list<Step>(record(r.new_changes_walkthrough)?.steps) ?? [],
    coverage: coverageOf(r.coverage),
  };
}

/** The two passes may report the same finding: it is shown once (never merged across groups). */
function dedupe(findings: Finding[]): Finding[] {
  const seen = new Set<string>();
  return findings.filter((f) => {
    const key = [f.group, f.severity, f.path, f.start_line, f.end_line, f.explanation].join("\u0000");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// --- diffs ----------------------------------------------------------------------------------

export type DiffLine = { kind: "+" | "-" | " " | "\\"; text: string; old: number | null; new: number | null };
export type Hunk = {
  header: string;
  oldStart: number;
  oldEnd: number;
  newStart: number;
  newEnd: number;
  lines: DiffLine[];
};

/** A hunk header's numbers ("@@ -10,3 +13,4 @@ …"), read without a backtracking regex. */
function readHeader(raw: string): { oldStart: number; oldCount: number; newStart: number; newCount: number } | null {
  if (!raw.startsWith("@@ -")) return null;
  const end = raw.indexOf(" @@", 4);
  const [before, after] = (end < 0 ? "" : raw.slice(4, end)).split(" +");
  if (before === undefined || after === undefined) return null;
  const [oldStart, oldCount] = before.split(",").map(Number);
  const [newStart, newCount] = after.split(",").map(Number);
  if (!Number.isInteger(oldStart) || !Number.isInteger(newStart)) return null;
  return {
    oldStart: oldStart as number,
    oldCount: Number.isInteger(oldCount) ? (oldCount as number) : 1,
    newStart: newStart as number,
    newCount: Number.isInteger(newCount) ? (newCount as number) : 1,
  };
}

/** How many patches were parsed: the guard that big PRs parse once per round (N3). */
export const parseStats = { parses: 0 };

/** A GitHub patch's hunks, numbered as Jesinia cites them (0-based, in order), with line numbers. */
export function parsePatch(patch: string): Hunk[] {
  parseStats.parses += 1;
  const hunks: Hunk[] = [];
  let current: Hunk | null = null;
  let oldLine = 0;
  let newLine = 0;
  for (const raw of patch.split("\n")) {
    const h = readHeader(raw);
    if (h) {
      oldLine = h.oldStart;
      newLine = h.newStart;
      const count = h.newCount;
      current = {
        header: raw,
        oldStart: oldLine,
        oldEnd: oldLine + Math.max(h.oldCount, 1) - 1,
        newStart: newLine,
        newEnd: newLine + Math.max(count, 1) - 1,
        lines: [],
      };
      hunks.push(current);
      continue;
    }
    if (!current) continue;
    const kind = raw[0];
    const text = raw.slice(1);
    if (kind === "+") current.lines.push({ kind: "+", text, old: null, new: newLine++ });
    else if (kind === "-") current.lines.push({ kind: "-", text, old: oldLine++, new: null });
    else if (kind === "\\") current.lines.push({ kind: "\\", text, old: null, new: null });
    else current.lines.push({ kind: " ", text, old: oldLine++, new: newLine++ });
  }
  return hunks;
}

// Parsed once per patch (I4): a patch's hunks are the same whoever asks, and a big PR's
// outline, steps and diffs ask often. Bounded so a long session doesn't keep every patch.
const PARSED = new Map<string, Hunk[]>();
const PARSED_MAX = 500;

/** `parsePatch`, memoised by the patch's text. */
export function hunksOf(patch: string): Hunk[] {
  let hunks = PARSED.get(patch);
  if (!hunks) {
    hunks = parsePatch(patch);
    if (PARSED.size >= PARSED_MAX) PARSED.delete(PARSED.keys().next().value as string);
    PARSED.set(patch, hunks);
  }
  return hunks;
}

const INDEXES = new WeakMap<PrFile[], Map<string, PrFile>>();

/** A file by its path, through an index built once per file list. */
export function fileOf(files: PrFile[] | null | undefined, path: string): PrFile | null {
  if (!files) return null;
  let index = INDEXES.get(files);
  if (!index) {
    index = new Map(files.map((f) => [f.path, f]));
    INDEXES.set(files, index);
  }
  return index.get(path) ?? null;
}

/**
 * Each step's findings, in one pass (I4): every finding is placed on the hunks of its file whose
 * lines it overlaps (new lines; old lines on a removed file, M9), then each step gathers its hunks'.
 */
export function findingsByStep(steps: Step[], findings: Finding[], files: PrFile[] | null | undefined): Finding[][] {
  const byHunk = new Map<string, Finding[]>();
  for (const f of findings) {
    const file = fileOf(files, f.path);
    if (!file?.patch) continue;
    const removed = file.status === "removed";
    hunksOf(file.patch).forEach((h, i) => {
      const [start, end] = removed ? [h.oldStart, h.oldEnd] : [h.newStart, h.newEnd];
      if (f.start_line > end || f.end_line < start) return;
      const key = `${f.path}\u0000${i}`;
      byHunk.set(key, [...(byHunk.get(key) ?? []), f]);
    });
  }
  return steps.map((step) => {
    const mine = new Set<Finding>();
    for (const ref of step.hunks) for (const f of byHunk.get(`${ref.path}\u0000${ref.hunk}`) ?? []) mine.add(f);
    return [...mine];
  });
}

/** A step's id: its title and hunks (M1), so a read tick stays on the step a rerun keeps. */
export function stepId(step: Step): string {
  const text = `${step.title}|${step.hunks.map((h) => `${h.path}#${h.hunk}`).join(",")}`;
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  return `step-${hash.toString(36)}`;
}

// --- hidden and direction-changing characters (Trojan Source, I6) ---------------------------

export type Run = { text: string } | { code: string; kind: "bidi" | "invisible" | "control" };

function hiddenKind(cp: number): "bidi" | "invisible" | "control" | null {
  if (
    (cp >= 0x202a && cp <= 0x202e) ||
    (cp >= 0x2066 && cp <= 0x2069) ||
    cp === 0x200e ||
    cp === 0x200f ||
    cp === 0x061c
  )
    return "bidi";
  if (
    (cp >= 0x200b && cp <= 0x200d) ||
    (cp >= 0x2060 && cp <= 0x2064) ||
    [0xfeff, 0x00ad, 0x180e, 0x034f, 0x115f, 0x1160, 0x3164, 0xffa0].includes(cp)
  )
    return "invisible";
  if ((cp < 0x20 && cp !== 0x09) || (cp >= 0x7f && cp <= 0x9f)) return "control";
  return null;
}

const code = (cp: number) => `U+${cp.toString(16).toUpperCase().padStart(4, "0")}`;

/**
 * Text split into plain runs and the characters that hide or reorder what is read: bidi
 * controls, zero-width and invisible characters, and control characters (a tab, and the CR of a
 * CRLF line ending, are ordinary). Each becomes a token the UI shows visibly.
 */
export function revealHidden(text: string): Run[] {
  const out: Run[] = [];
  let plain = "";
  const chars = [...text];
  chars.forEach((ch, i) => {
    const cp = ch.codePointAt(0) as number;
    const kind = cp === 0x0d && i === chars.length - 1 ? null : hiddenKind(cp);
    if (kind === null) {
      plain += ch;
      return;
    }
    if (plain) out.push({ text: plain });
    plain = "";
    out.push({ code: code(cp), kind });
  });
  if (plain || out.length === 0) out.push({ text: plain });
  return out;
}

/** Text with each hidden character as its token, for accessible names and plain strings. */
export function visibleText(text: string): string {
  return revealHidden(text)
    .map((r) => ("code" in r ? `⟪${r.code}⟫` : r.text))
    .join("");
}

export function hasHidden(text: string): boolean {
  return revealHidden(text).some((r) => "code" in r);
}

/** Whether a hunk (or a file's patch) has any hidden or direction-changing character. */
export const hunkHasHidden = (hunk: Hunk) => hasHidden(hunk.header) || hunk.lines.some((l) => hasHidden(l.text));

/** The findings that fall in a step: on one of its files, within one of its hunks' new lines. */
export function findingsInStep(step: Step, findings: Finding[], files: PrFile[] | null | undefined): Finding[] {
  return findingsByStep([step], findings, files)[0] ?? [];
}

// --- words ----------------------------------------------------------------------------------

/** Why a file or hunk wasn't walked through (spec §3.1), in words. */
export const REASONS: Record<string, string> = {
  generated: "generated",
  lockfile: "lockfile",
  vendored: "vendored",
  binary: "binary",
  rename_only: "rename only",
  format_only: "whitespace or format only",
  judged_mechanical: "judged mechanical by the agent",
  metadata_only: "metadata only",
  omitted: "left out without a reason",
  unchanged: "unchanged since your last review",
};

export const SEVERITY: Record<string, string> = { critical: "Critical", important: "Important", minor: "Minor" };
export const GROUPS = ["correctness", "design", "style"] as const;
export const GROUP_LABEL: Record<string, string> = { correctness: "Correctness", design: "Design", style: "Style" };

export const lines = (f: { start_line: number; end_line: number }) =>
  f.start_line === f.end_line ? `${f.start_line}` : `${f.start_line}–${f.end_line}`;

/** The coverage line (spec §3.1): computed by Jesinia, never the agent's; never 100% for nothing. */
export function coverageLine(cov: WalkCoverage | null, nothingToWalk: boolean, long = false): string | null {
  if (nothingToWalk) return "Nothing to walk";
  if (!cov || cov.pct === null) return null;
  const what = long ? `${Math.round(cov.pct)}% of changed lines` : `${Math.round(cov.pct)}%`;
  return `Walkthrough covers ${what} · ${cov.files_covered} of ${cov.files_total} files`;
}

/** "2 files · 18%": what was left out, and its share of the change. */
export function notWalkedNote(cov: WalkCoverage | null): string {
  if (!cov) return "";
  const files = new Set(cov.not_walked.map((s) => s.path)).size;
  // The list keeps at most 500 entries beside its full count: a cut list says so.
  const more = cov.not_walked_total > cov.not_walked.length ? "+" : "";
  const share = cov.pct === null ? "" : ` · ${Math.round(100 - cov.pct)}%`;
  return `${files}${more} ${files === 1 && !more ? "file" : "files"}${share}`;
}

/** "1 important", "2 minor": a step's findings by their worst severity. */
export function findingNote(findings: Finding[]): { text: string; bad: boolean } | null {
  if (findings.length === 0) return null;
  for (const sev of ["critical", "important"] as const) {
    const n = findings.filter((f) => f.severity === sev).length;
    if (n) return { text: `${n} ${sev}`, bad: true };
  }
  return { text: `${findings.length} minor`, bad: false };
}

// --- the outline ----------------------------------------------------------------------------

export type EntryKind =
  | "explanation"
  | "overview"
  | "background"
  | "step"
  | "notwalked"
  | "findings"
  | "metrics"
  | "quiz"
  | "comments"
  | "recommendation"
  | "requests"
  | "newchanges";

export type Entry = {
  id: string;
  kind: EntryKind;
  part: string;
  label: string;
  /** For a step: its index in the walkthrough. */
  step?: number;
  note?: string;
  bad?: boolean;
};

/** The job whose output an entry shows (for "running…" and "failed"). */
export const JOB_OF: Partial<Record<EntryKind, string>> = {
  explanation: "overview",
  overview: "overview",
  background: "background",
  notwalked: "walkthrough",
  findings: "findings",
  metrics: "metrics",
  quiz: "quiz",
  recommendation: "recommendation",
  requests: "rereview",
  newchanges: "rereview",
};

const has = (a: Artifacts, kind: EntryKind): boolean => {
  switch (kind) {
    case "explanation":
    case "overview":
      return a.overview !== null;
    case "background":
      return a.background !== null;
    case "metrics":
      return a.metrics !== null;
    case "quiz":
      return a.quiz !== null;
    case "findings":
      return a.findings !== null;
    case "recommendation":
      return a.verdict !== null || a.suggestion !== null;
    case "requests":
    case "newchanges":
      return a.rereview !== null;
    default:
      return true;
  }
};

/** Where a missing page's job is: running, failed (with its cause), or not generated yet. */
export function pending(
  a: Artifacts,
  kind: EntryKind,
): { state: "running" | "failed" | "missing"; error: string | null } | null {
  if (has(a, kind)) return null;
  const job = a.jobs[JOB_OF[kind] ?? ""];
  if (job?.state === "running") return { state: "running", error: null };
  if (job?.state === "failed") return { state: "failed", error: job.error };
  return { state: "missing", error: null };
}

const VERDICT_SHORT: Record<string, string> = {
  approve: "approve",
  request_changes: "request changes",
  needs_closer_look: "closer look",
};

/**
 * The book's outline (design pass pattern 3). Deep: before you read, the walkthrough as
 * chapters with their findings, what wasn't walked, and after you read. Quick: the
 * explanation, the walk, findings and the recommendation (spec §3.1).
 */
export function outlineOf(
  depth: Depth,
  a: Artifacts,
  files: PrFile[] | null | undefined,
  draftCount: number | null,
): Entry[] {
  const out: Entry[] = [];
  const add = (e: Entry) => {
    const p = pending(a, e.kind);
    const note = p
      ? p.state === "running"
        ? "running…"
        : p.state === "failed"
          ? "failed"
          : "not generated yet"
      : e.note;
    out.push({ ...e, ...(note !== undefined ? { note } : {}), bad: p?.state === "failed" || e.bad === true });
  };
  const walkPart = a.nothingToWalk
    ? "Walkthrough · nothing to walk"
    : a.walk?.pct != null
      ? `Walkthrough · ${Math.round(a.walk.pct)}% of the change`
      : "Walkthrough";
  if (depth === "deep") {
    add({ id: "overview", kind: "overview", part: "Before you read", label: "Overview" });
    add({ id: "background", kind: "background", part: "Before you read", label: "Background" });
  } else {
    add({ id: "explanation", kind: "explanation", part: "Before you read", label: "Explanation" });
  }
  if (a.isRereview) {
    // Spec §4.2: what happened to your requests, then what is new, before the full walk.
    const since = "Since your last review";
    // Open: not addressed, or given no status (unknown is never counted as done, M1).
    const open = a.rereview
      ? a.rereview.requests.filter((r) => r.status !== "addressed").length + a.rereview.unanswered.length
      : 0;
    add({
      id: "requests",
      kind: "requests",
      part: since,
      label: "Your requests",
      ...(a.rereview ? { note: `${open} open`, bad: open > 0 } : {}),
    });
    add({ id: "newchanges", kind: "newchanges", part: since, label: "New since your last look" });
  }
  const perStep = findingsByStep(a.steps ?? [], a.findings ?? [], files);
  const ids = new Set<string>();
  (a.steps ?? []).forEach((step, i) => {
    let id = stepId(step);
    if (ids.has(id)) id = `${id}-${i}`; // two identical steps still get their own entries
    ids.add(id);
    const note = findingNote(perStep[i] ?? []);
    out.push({
      id,
      kind: "step",
      part: walkPart,
      label: `${i + 1}. ${step.title}`,
      step: i,
      ...(note ? { note: note.text, bad: note.bad } : {}),
    });
  });
  if (a.steps === null) {
    const job = a.jobs.walkthrough;
    out.push({
      id: "walk-pending",
      kind: "notwalked",
      part: walkPart,
      label: "Walkthrough",
      note: job?.state === "running" ? "running…" : job?.state === "failed" ? "failed" : "not generated yet",
      bad: job?.state === "failed",
    });
  } else if (!a.nothingToWalk) {
    out.push({
      id: "notwalked",
      kind: "notwalked",
      part: walkPart,
      label: "Not walked through",
      note: notWalkedNote(a.walk),
    });
  }
  const after = "After you read";
  const counts = (a.findings ?? []).reduce(
    (acc, f) => (f.severity === "minor" ? { ...acc, minor: acc.minor + 1 } : { ...acc, serious: acc.serious + 1 }),
    { serious: 0, minor: 0 },
  );
  add({
    id: "findings",
    kind: "findings",
    part: after,
    label: "Findings",
    note: `${counts.serious} · ${counts.minor}`,
    bad: counts.serious > 0,
  });
  if (depth === "deep") {
    add({ id: "metrics", kind: "metrics", part: after, label: "Metrics" });
    const n = a.quiz?.length ?? 0;
    add({ id: "quiz", kind: "quiz", part: after, label: "Quiz", note: `${n} questions` });
    const drafts = draftCount === null ? undefined : `${draftCount} ${draftCount === 1 ? "draft" : "drafts"}`;
    add({ id: "comments", kind: "comments", part: after, label: "Your comments", ...(drafts ? { note: drafts } : {}) });
  }
  const verdict = a.verdict?.verdict;
  add({
    id: "recommendation",
    kind: "recommendation",
    part: after,
    label: "Recommendation",
    ...(verdict ? { note: VERDICT_SHORT[verdict] ?? verdict, bad: verdict === "request_changes" } : {}),
  });
  return out;
}

/** The short commit SHA the posting dialog names. */
export const short = (sha: string) => sha.slice(0, 7);
