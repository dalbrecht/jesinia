// SPDX-License-Identifier: AGPL-3.0-or-later
export type AppInfo = { id: string; name: string; href: string; description: string; built: boolean };

/** The suite, in launcher order. Unbuilt apps show as "Coming soon". */
export const APPS: AppInfo[] = [
  { id: "notes", name: "Notes", href: "/notes/", description: "Search the KB and think about a page with your agent.", built: true },
  { id: "rss", name: "RSS", href: "/rss/", description: "Read feeds, engage a post, keep what matters.", built: true },
  { id: "review", name: "Review", href: "/review/", description: "Understand a pull request before you approve it.", built: false },
  { id: "issues", name: "Issues", href: "/issues/", description: "Triage issues across your repos with agent help.", built: false },
]; // prettier-ignore

export const RUNNER_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", fake: "Fake runner" };
