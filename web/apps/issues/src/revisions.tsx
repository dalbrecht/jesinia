// SPDX-License-Identifier: AGPL-3.0-or-later
// One revision per shared resource (Codex round 2 on PR #72). Panes stay mounted while hidden
// (they keep their place), so a change made in one pane would leave another showing what was: a
// change bumps its resource's revision, and every mounted pane that reads that resource reads it
// again when the revision moves.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type Resource = "queues" | "themes";

type Revisions = { revision: (resource: Resource) => number; bump: (resource: Resource) => void };

// Without a provider (a pane rendered on its own) nothing is shared: revision 0, bump a no-op.
const RevisionsContext = createContext<Revisions>({ revision: () => 0, bump: () => undefined });

export function RevisionsProvider({ children }: { children: ReactNode }) {
  const [counts, setCounts] = useState<Record<Resource, number>>({ queues: 0, themes: 0 });
  const bump = useCallback((resource: Resource) => setCounts((c) => ({ ...c, [resource]: c[resource] + 1 })), []);
  const value = useMemo<Revisions>(() => ({ revision: (resource) => counts[resource], bump }), [counts, bump]);
  return <RevisionsContext.Provider value={value}>{children}</RevisionsContext.Provider>;
}

/** `resource`'s revision (read it in an effect's dependencies) and a bump for changes to it. */
export function useRevision(resource: Resource): { revision: number; bump: () => void } {
  const { revision, bump } = useContext(RevisionsContext);
  const current = revision(resource);
  const bumpThis = useCallback(() => bump(resource), [bump, resource]);
  return { revision: current, bump: bumpThis };
}
