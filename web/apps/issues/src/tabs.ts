// SPDX-License-Identifier: AGPL-3.0-or-later
// The open workspace tabs (design pass pattern 1), kept per browser profile. Storage can be
// missing or refuse access (a private window, blocked site data): tabs then last the session.

export const TABS_KEY = "jesinia.issues.tabs";
const MAX_TABS = 20;

/** An opened issue or epic (`kind: "epic"`, the epic layout): its ref and its label. */
export type OpenTab = { repo: string; number: number; label: string; kind?: "epic" };

function isTab(value: unknown): value is OpenTab {
  if (typeof value !== "object" || value === null) return false;
  const t = value as Record<string, unknown>;
  return (
    typeof t.repo === "string" &&
    typeof t.number === "number" &&
    typeof t.label === "string" &&
    (t.kind === undefined || t.kind === "epic")
  );
}

export function loadTabs(): OpenTab[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(TABS_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter(isTab).slice(0, MAX_TABS) : [];
  } catch {
    return []; // unreadable or corrupt: start with the fixed tabs alone
  }
}

export function saveTabs(tabs: OpenTab[]): void {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(tabs.slice(0, MAX_TABS)));
  } catch {
    // Storage refused: the tabs stay open for this session only.
  }
}

type Tabbed = { repo: string; number: number; kind?: string | undefined };
const isEpic = (t: Tabbed) => t.kind === "epic";

/** The same screen: an issue's and an epic's tabs differ; repo names compare without case, as
 * GitHub's (and the server's) do. */
export const sameTab = (a: Tabbed, b: Tabbed): boolean =>
  isEpic(a) === isEpic(b) && a.repo.toLowerCase() === b.repo.toLowerCase() && a.number === b.number;
