// SPDX-License-Identifier: AGPL-3.0-or-later
// One drag surface per pane (spec §6.1). Each pane mounts its own DndProvider, so the keyboard
// never steps onto a target in a hidden pane. Every drop target carries what its drop does
// (`DropAction`) as its data, so the provider only dispatches: a board column stages a label, an
// epic stages sub-issue links, a theme adds itself locally, a queue band or Done zone answers
// through the queue's endpoints.
import { DndProvider, type DragItem, type DropTargetInfo } from "@jesinia/design";
import type { ReactNode } from "react";

export type DropAction = {
  drop: (items: readonly DragItem[], position: number | null) => void;
};

export const ISSUES = { one: "issue", other: "issues" };

const actionOf = (target: DropTargetInfo) => target.data as DropAction;

/** The pane's drag surface: drops go to the target's own action. */
export function Drops({ children }: { children: ReactNode }) {
  return (
    <DndProvider noun={ISSUES} onDrop={(items, target, position) => actionOf(target).drop(items, position)}>
      {children}
    </DndProvider>
  );
}
