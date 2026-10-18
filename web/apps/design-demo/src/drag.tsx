// SPDX-License-Identifier: AGPL-3.0-or-later
// The drag-and-drop primitives in a real browser: a long queue in a scrolling pane, a few
// targets, and an item to drag in. The e2e suite drives it (tests/e2e/dnd.spec.ts), since jsdom
// lays nothing out and fires no real scroll events.
import "@jesinia/design/fonts.css";
import "@jesinia/design/reset.css";
import "@jesinia/design/tokens.css";
import { DndProvider, Draggable, DropTarget, SortableList, type DragItem } from "@jesinia/design";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import "./demo.css";

const ROWS: DragItem[] = Array.from({ length: 40 }, (_, index) => ({ id: `r${index}`, label: `Row ${index}` }));

function DragDemo() {
  const [rows, setRows] = useState(ROWS);
  const [log, setLog] = useState("Nothing dropped yet.");
  return (
    <DndProvider
      noun={{ one: "row", other: "rows" }}
      onDrop={(items, target, position) => {
        setLog(`Dropped ${items.map((item) => item.label).join(", ")} on ${target.label} at ${String(position)}.`);
      }}
    >
      <main className="page">
        <h1>Drag and drop</h1>
        <div className="drag-layout">
          <div className="drag-pane" data-testid="pane">
            <SortableList
              id="queue"
              label="Queue"
              items={rows}
              onMove={(order) => setRows(order.map((id) => rows.find((row) => row.id === id) as DragItem))}
              renderItem={(row) => <span className="drag-row">{row.label}</span>}
            />
          </div>
          <div className="drag-side">
            <Draggable id="new" label="New issue">
              <span className="drag-row">New issue</span>
            </Draggable>
            <DropTarget id="done" label="Done">
              <span className="drag-row">Done</span>
            </DropTarget>
            <DropTarget
              id="locked"
              label="Locked epic"
              accepts={() => ({ code: "locked", reason: "This epic is closed" })}
            >
              <span className="drag-row">Locked epic</span>
            </DropTarget>
            <p data-testid="log">{log}</p>
          </div>
        </div>
      </main>
    </DndProvider>
  );
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <DragDemo />
  </StrictMode>,
);
