// SPDX-License-Identifier: AGPL-3.0-or-later
// The PR view's pure rules: reading artifacts, patches, coverage words and the outline.
import { describe, expect, it } from "vitest";
import {
  coverageLine,
  findingNote,
  findingsInStep,
  notWalkedNote,
  outlineOf,
  parsePatch,
  pending,
  readArtifacts,
  type Finding,
  type WalkCoverage,
} from "../pr/model";
import { pairLines } from "../pr/Walkthrough";
import { prView } from "./fixtures";
import { ARTIFACTS, FILES, REVIEWED, reviewed } from "./pr-fixtures";

const cov = (over: Partial<WalkCoverage> = {}): WalkCoverage => ({
  pct: 82,
  files_covered: 1,
  files_total: 3,
  not_walked: [{ path: "a.lock", hunk: null, reason: "lockfile" }],
  not_walked_total: 1,
  nothing_to_walk: false,
  ...over,
});
const finding = (over: Partial<Finding> = {}): Finding => ({
  group: "correctness",
  severity: "minor",
  path: "src/billing/proration.py",
  start_line: 1,
  end_line: 1,
  explanation: "x",
  rule: null,
  ...over,
});

describe("parsePatch", () => {
  it("numbers lines, reads one-line headers, and keeps no-newline markers", () => {
    const [hunk] = parsePatch("ignored before a header\n@@ -3 +3 @@\n-a\n+b\n\\ No newline at end of file");
    expect(hunk?.oldStart).toBe(3);
    expect(hunk?.newEnd).toBe(3);
    expect(hunk?.lines).toEqual([
      { kind: "-", text: "a", old: 3, new: null },
      { kind: "+", text: "b", old: null, new: 3 },
      { kind: "\\", text: " No newline at end of file", old: null, new: null },
    ]);
  });

  it("pairs removals beside the additions that follow them", () => {
    const [hunk] = parsePatch("@@ -1,3 +1,2 @@\n x\n-a\n-b\n+c\n y");
    const pairs = pairLines(hunk?.lines ?? []);
    expect(pairs.map((p) => [p.left?.text ?? null, p.right?.text ?? null])).toEqual([
      ["x", "x"],
      ["a", "c"],
      ["b", null],
      ["y", "y"],
    ]);
  });
});

describe("readArtifacts", () => {
  it("reads nothing from an empty round", () => {
    const a = readArtifacts(prView(REVIEWED));
    expect(a).toMatchObject({ overview: null, steps: null, findings: null, walk: null, verdict: null, jobs: {} });
    expect(a.nothingToWalk).toBe(false);
  });

  it("tolerates partial shapes: odd job entries and coverage without counts", () => {
    const a = readArtifacts(
      prView(REVIEWED, {
        artifacts: { jobs: { quiz: "odd", metrics: { state: "failed" } }, coverage: { walkthrough: {} } },
      }),
    );
    expect(a.jobs).toEqual({ metrics: { state: "failed", error: null } });
    expect(a.walk).toEqual({
      pct: null,
      files_covered: 0,
      files_total: 0,
      not_walked: [],
      not_walked_total: 0,
      nothing_to_walk: false,
    });
  });

  it("shows a finding both passes reported once", () => {
    const same = ARTIFACTS.findings.findings[0];
    const a = readArtifacts(
      reviewed({ artifacts: { ...ARTIFACTS, correctness: { ...ARTIFACTS.correctness, findings: [same] } } }),
    );
    expect(a.findings).toHaveLength(2);
  });
});

describe("words", () => {
  it("coverage: nothing to walk, unknown, short and long", () => {
    expect(coverageLine(cov(), true)).toBe("Nothing to walk");
    expect(coverageLine(null, false)).toBeNull();
    expect(coverageLine(cov({ pct: null }), false)).toBeNull();
    expect(coverageLine(cov(), false, true)).toBe("Walkthrough covers 82% of changed lines · 1 of 3 files");
  });

  it("what was left out, cut lists and unknown shares", () => {
    expect(notWalkedNote(null)).toBe("");
    expect(notWalkedNote(cov())).toBe("1 file · 18%");
    expect(notWalkedNote(cov({ not_walked_total: 600, pct: null }))).toBe("1+ files");
  });

  it("a step's findings by their worst severity", () => {
    expect(findingNote([])).toBeNull();
    expect(findingNote([finding(), finding({ severity: "critical" })])).toEqual({ text: "1 critical", bad: true });
    expect(findingNote([finding(), finding()])).toEqual({ text: "2 minor", bad: false });
  });

  it("findings outside a step's files or hunks aren't the step's", () => {
    const step = {
      title: "t",
      body: "b",
      hunks: [
        { path: "src/billing/proration.py", hunk: 0 },
        { path: "gone.py", hunk: 0 },
      ],
    };
    expect(findingsInStep(step, [finding({ path: "other.py" })], FILES)).toEqual([]);
    expect(findingsInStep(step, [finding({ path: "gone.py" })], FILES)).toEqual([]);
    expect(findingsInStep(step, [finding({ start_line: 40, end_line: 41 })], FILES)).toEqual([]);
    expect(findingsInStep(step, [finding()], null)).toEqual([]);
  });
});

describe("outlineOf", () => {
  it("says where a missing walkthrough is: running, failed or not generated", () => {
    const of = (state: string | null) =>
      outlineOf(
        "quick",
        readArtifacts(
          prView(REVIEWED, { artifacts: state ? { jobs: { walkthrough: { state, error: "boom" } } } : {} }),
        ),
        null,
        null,
      ).find((e) => e.id === "walk-pending");
    expect(of("running")).toMatchObject({ note: "running…", bad: false });
    expect(of("failed")).toMatchObject({ note: "failed", bad: true });
    expect(of(null)).toMatchObject({ note: "not generated yet" });
  });

  it("leaves Not walked through out when there is nothing to walk; request changes reads bad", () => {
    const view = reviewed({
      nothing_to_walk: true,
      artifacts: { ...ARTIFACTS, verdict: { verdict: "request_changes", reasons: [] } },
    });
    const entries = outlineOf("deep", readArtifacts(view), FILES, 2);
    expect(entries.some((e) => e.id === "notwalked")).toBe(false);
    expect(entries.find((e) => e.id === "recommendation")).toMatchObject({ note: "request changes", bad: true });
    expect(entries.find((e) => e.id === "comments")).toMatchObject({ note: "2 drafts" });
  });

  it("pending pages: generated ones are not pending", () => {
    const a = readArtifacts(reviewed());
    expect(pending(a, "overview")).toBeNull();
    expect(pending(a, "step")).toBeNull();
    expect(pending(readArtifacts(prView(REVIEWED)), "recommendation")).toEqual({ state: "missing", error: null });
  });
});
