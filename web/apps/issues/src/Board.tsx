// SPDX-License-Identifier: AGPL-3.0-or-later
// The board view of the Inbox and Backlog (spec §6.1, §6.2). Readiness and priority columns are
// drop targets: a drop stages that label in the tray (a drag is the author's own action, so it
// waits for Apply). Status columns are derived from GitHub and refuse drops with the reason.
// The drag primitives give every drop a keyboard and screen-reader equivalent (Space, arrows).
//
// The drag surface (`Drops`) is the list's, so the board's cards also drop onto the themes beside it.
import { Badge, Draggable, DropTarget, toneFor, type DragItem, type Refusal } from "@jesinia/design";
import { VisibleText, visibleText } from "@jesinia/shell";
import { useMemo } from "react";
import { refOf, schemaValues, STATUS_COLUMNS, statusOf, type Dimension, type IssueRow, type Repo } from "./api";
import type { DropAction } from "./drops";
import s from "./issues.module.css";
import { useTray } from "./Tray";

export type Grouping = "readiness" | "priority" | "status";

const NONE = "\u0000none";

type Column = { id: string; label: string; value: string | null; rows: IssueRow[] };

const DERIVED: Refusal = {
  code: "derived",
  reason: "Status comes from GitHub (its state, linked PRs and labels), so a drop can't set it",
};

export const itemOf = (row: IssueRow) => ({
  id: refOf(row),
  label: `${refOf(row)} ${visibleText(row.title)}`,
  data: row,
});
export const rowsOf = (items: readonly DragItem[]) => items.map((item) => item.data as IssueRow);

/** Whether `row`'s repo schema has `value` for `dimension` (an unknown repo has none). */
function hasValue(repos: Repo[], row: IssueRow, dimension: Dimension, value: string): boolean {
  const repo = repos.find((r) => r.repo.toLowerCase() === row.repo.toLowerCase());
  return repo?.label_schema[dimension].some((c) => c.name === value) ?? false;
}

/** Why a column refuses these issues, or null when it takes them (ruling IR17's shape). */
export function columnRefusal(
  repos: Repo[],
  grouping: Grouping,
  value: string | null,
  rows: readonly IssueRow[],
): Refusal | null {
  if (grouping === "status") return DERIVED;
  if (value === null) {
    return { code: "no_value", reason: `Pick a value: taking the ${grouping} off isn't a change Jesinia stages` };
  }
  const lacking = rows.find((row) => !hasValue(repos, row, grouping, value));
  if (lacking) return { code: "not_in_schema", reason: `${lacking.repo}'s label schema has no ${value}` };
  if (rows.every((row) => row[grouping] === value)) return { code: "unchanged", reason: `Already ${value}` };
  return null;
}

function columnsFor(rows: IssueRow[], repos: Repo[], grouping: Grouping): Column[] {
  if (grouping === "status") {
    return STATUS_COLUMNS.map((c) => ({ id: c.id, label: c.label, value: c.id, rows: rows.filter((r) => statusOf(r) === c.id) })); // prettier-ignore
  }
  const values = schemaValues(repos, grouping);
  for (const row of rows) {
    const value = row[grouping];
    if (value !== null && !values.includes(value)) values.push(value);
  }
  return [
    { id: NONE, label: `No ${grouping}`, value: null, rows: rows.filter((r) => r[grouping] === null) },
    ...values.map((value) => ({ id: value, label: value, value, rows: rows.filter((r) => r[grouping] === value) })),
  ];
}

type BoardProps = {
  rows: IssueRow[];
  repos: Repo[];
  grouping: Grouping;
  /** The list's multi-selection, by ref: picking one of them up drags them all. */
  selected: ReadonlySet<string>;
  onOpen: (row: IssueRow) => void;
};

export function Board({ rows, repos, grouping, selected, onOpen }: BoardProps) {
  const tray = useTray();
  const group = useMemo(() => rows.filter((row) => selected.has(refOf(row))).map(itemOf), [rows, selected]);
  const columns = columnsFor(rows, repos, grouping);

  const dropOn = (dimension: Dimension, value: string) => (dropped: readonly DragItem[]) => {
    // One after another, then one read of the tray, so it shows every change staged.
    const changes = rowsOf(dropped)
      .filter((row) => row[dimension] !== value)
      .map((row) => ({ repo: row.repo, n: row.number, change: { kind: "set_dimension" as const, dimension, value }, source: "drag" as const })); // prettier-ignore
    void tray.stageAll(changes);
  };

  return (
    <div className={s.board}>
      {columns.map((column) => {
        // Only readiness and priority columns with a value take drops (the rest refuse, and a
        // refused drop never arrives), so a drop is always onto one of those.
        const data: DropAction = { drop: dropOn(grouping as Dimension, column.value ?? "") };
        return (
          <DropTarget
            key={column.id}
            id={`column:${grouping}:${column.id}`}
            label={visibleText(column.label)}
            data={data}
            accepts={(dragged) => columnRefusal(repos, grouping, column.value, rowsOf(dragged))}
            className={s.column}
            role="group"
            aria-label={visibleText(column.label)}
          >
            <div className={s.columnHead}>
              <span className={s.columnTitle}>
                <VisibleText text={column.label} />
              </span>
              <span className={s.muted}>{column.rows.length}</span>
            </div>
            <div className={s.columnBody}>
              {column.rows.map((row) => {
                const item = itemOf(row);
                return (
                  <Draggable key={item.id} id={item.id} label={item.label} data={row} group={group} className={s.card}>
                    {(handle) => (
                      <>
                        <div className={s.cardTop}>
                          <button type="button" {...handle} className={s.grip} aria-label={`Move ${item.label}`}>
                            <span aria-hidden="true">⠿</span>
                          </button>
                          <span className={s.ref}>{refOf(row)}</span>
                        </div>
                        <button type="button" className={s.cardTitle} onClick={() => onOpen(row)}>
                          <VisibleText text={row.title} />
                        </button>
                        <div className={s.cardLabels}>
                          {row.labels.slice(0, 4).map((label) => (
                            <Badge key={label} tone={toneFor(label)}>
                              <VisibleText text={label} />
                            </Badge>
                          ))}
                        </div>
                      </>
                    )}
                  </Draggable>
                );
              })}
            </div>
          </DropTarget>
        );
      })}
    </div>
  );
}
