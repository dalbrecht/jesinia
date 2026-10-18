// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";
import {
  ActionKeys,
  ageOf,
  coverageText,
  errorCause,
  isAuthMissing,
  prPath,
  prRefFromText,
  ReviewRequestError,
  routeOf,
  sectionOf,
  splitCode,
} from "../api";
import { AUTH_MISSING, row } from "./fixtures";

describe("sectionOf", () => {
  it("files rows by what the author can do", () => {
    expect(sectionOf(row(1))).toBe("ready");
    expect(sectionOf(row(1, { prereview_state: "not_started", ci_state: "passing" }))).toBe("ready");
    expect(sectionOf(row(1, { prereview_state: "not_started", ci_state: "pending" }))).toBe("waiting");
    expect(sectionOf(row(1, { prereview_state: "waiting_ci" }))).toBe("waiting");
    expect(sectionOf(row(1, { loaded: true }))).toBe("loaded");
    expect(sectionOf(row(1, { kind: "rebase" }))).toBe("approved");
    expect(sectionOf(row(1, { fetch_error: "boom" }))).toBe("waiting");
    expect(sectionOf(row(1, { suggested: true }))).toBe("waiting");
    expect(sectionOf(row(1, { suggested: true, loaded: true }))).toBe("loaded");
  });
});

describe("routes", () => {
  it("maps a PR to its route and back", () => {
    expect(prPath("acme/web", 7)).toBe("/review/acme/web/7");
    expect(routeOf("/review/acme/web/7")).toEqual({ repo: "acme/web", number: 7 });
    expect(routeOf("/review/acme/web/7/")).toEqual({ repo: "acme/web", number: 7 });
    expect(routeOf("/review/")).toBeNull();
    expect(routeOf("/review/acme/web/0")).toBeNull();
    expect(routeOf("/review/acme/web/x")).toBeNull();
  });

  it("recognises a pasted PR for ⌘K", () => {
    expect(prRefFromText("https://github.com/acme/web/pull/7")).toBe("acme/web#7");
    expect(prRefFromText("https://github.com/acme/web/pull/7/files")).toBe("acme/web#7");
    expect(prRefFromText(" acme/web#7 ")).toBe("acme/web#7");
    expect(prRefFromText("/review/acme/web/7")).toBe("acme/web#7");
    expect(prRefFromText("billing")).toBeNull();
  });
});

describe("text helpers", () => {
  it("coverage: Nothing to walk, a percentage, or nothing", () => {
    expect(coverageText(row(1, { coverage_pct: 100, nothing_to_walk: true }))).toBe("Nothing to walk");
    expect(coverageText(row(1, { coverage_pct: 87.6 }))).toBe("walk 88%");
    expect(coverageText(row(1, { coverage_pct: null }))).toBeNull();
  });

  it("splits backticked commands out of a cause", () => {
    expect(splitCode("run `gh auth login` again")).toEqual([
      { code: false, text: "run " },
      { code: true, text: "gh auth login" },
      { code: false, text: " again" },
    ]);
    expect(splitCode("plain")).toEqual([{ code: false, text: "plain" }]);
  });

  it("tells auth problems from other sync failures", () => {
    expect(isAuthMissing(AUTH_MISSING)).toBe(true);
    expect(isAuthMissing("GitHub rejected the token: run `gh auth login` again")).toBe(true);
    expect(isAuthMissing("github.com unreachable")).toBe(false);
    expect(isAuthMissing(null)).toBe(false);
  });

  it("ages", () => {
    const now = new Date("2026-10-17T12:00:00Z");
    expect(ageOf("2026-10-17T11:59:30Z", now)).toBe("just opened");
    expect(ageOf("2026-10-17T11:15:00Z", now)).toBe("45 min old");
    expect(ageOf("2026-10-17T07:00:00Z", now)).toBe("5 h old");
    expect(ageOf("2026-10-16T12:00:00Z", now)).toBe("1 day old");
    expect(ageOf("2026-10-14T12:00:00Z", now)).toBe("3 days old");
    expect(ageOf("not a date", now)).toBe("");
  });

  it("errors read as sentences with their cause", () => {
    expect(errorCause(new ReviewRequestError("GitHub is down", 503))).toBe("GitHub is down.");
    expect(errorCause(new ReviewRequestError("Done!", 400))).toBe("Done!");
    expect(errorCause(new TypeError("Failed to fetch"))).toMatch(/Jesinia didn't answer \(Failed to fetch\)/);
    expect(errorCause("odd")).toMatch(/Jesinia didn't answer \(odd\)/);
  });
});

describe("ActionKeys", () => {
  it("reuses a key while an action's outcome is unknown, and starts fresh once answered", () => {
    const keys = new ActionKeys();
    const first = keys.keyFor("load:acme/web#7");
    expect(keys.keyFor("load:acme/web#7")).toBe(first);
    keys.settle("load:acme/web#7", new TypeError("network"));
    expect(keys.keyFor("load:acme/web#7")).toBe(first);
    keys.settle("load:acme/web#7", new ReviewRequestError("GitHub is down", 503));
    expect(keys.keyFor("load:acme/web#7")).toBe(first);
    keys.settle("load:acme/web#7", new ReviewRequestError("bad ref", 400));
    const second = keys.keyFor("load:acme/web#7");
    expect(second).not.toBe(first);
    keys.settle("load:acme/web#7");
    expect(keys.keyFor("load:acme/web#7")).not.toBe(second);
    expect(keys.keyFor("other")).not.toBe(keys.keyFor("load:acme/web#7"));
  });
});
