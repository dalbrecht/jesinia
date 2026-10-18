// SPDX-License-Identifier: AGPL-3.0-or-later
// The book's outline (design pass pattern 3): each part ("Before you read", the walkthrough,
// "After you read") labels its own list (M11). A part with hundreds of steps is windowed
// (I4): only the rows in view render, so a 1000-step walkthrough stays fast.
import { VisuallyHidden } from "@jesinia/design";
import { useEffect, useId, useRef, useState } from "react";
import type { Entry } from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";

/** Parts longer than this are windowed. */
export const WINDOW_FROM = 150;
const ROW_PX = 30;
const OVERSCAN = 10;

type Props = {
  entries: Entry[];
  current: string;
  read: ReadonlySet<string>;
  onGo: (id: string) => void;
};

function Row({
  e,
  isCurrent,
  isRead,
  onGo,
}: {
  e: Entry;
  isCurrent: boolean;
  isRead: boolean;
  onGo: (id: string) => void;
}) {
  return (
    <li>
      <a
        href={`#${e.id}`}
        className={s.entry}
        data-sub={e.kind === "step" || e.id === "notwalked" || undefined}
        aria-current={isCurrent ? "step" : undefined}
        onClick={(ev) => {
          ev.preventDefault();
          onGo(e.id);
        }}
      >
        <span className={s.tick} data-read={isRead || undefined} aria-hidden="true">
          {isRead ? "✓" : isCurrent ? "●" : "○"}
        </span>
        <span className={s.entryLabel}>
          <Visible text={e.label} />
        </span>
        <span className={s.note} data-bad={e.bad || undefined}>
          {e.note ? ` ${e.note}` : ""}
        </span>
        {isRead ? <VisuallyHidden> (read)</VisuallyHidden> : null}
      </a>
    </li>
  );
}

function Part({ name, entries, current, read, onGo }: Props & { name: string }) {
  const id = useId();
  const box = useRef<HTMLOListElement>(null);
  const [top, setTop] = useState(0);
  const windowed = entries.length > WINDOW_FROM;
  const at = entries.findIndex((e) => e.id === current);

  // The current entry stays in view as the author pages through a long walkthrough.
  useEffect(() => {
    const el = box.current;
    if (!windowed || !el || at < 0) return;
    const view = el.clientHeight || 600;
    if (at * ROW_PX < el.scrollTop || (at + 1) * ROW_PX > el.scrollTop + view) {
      el.scrollTop = Math.max(0, at * ROW_PX - view / 2);
      setTop(el.scrollTop);
    }
  }, [windowed, at]);

  const view = box.current?.clientHeight || 600;
  const first = windowed ? Math.max(0, Math.floor(top / ROW_PX) - OVERSCAN) : 0;
  const last = windowed ? Math.min(entries.length, Math.ceil((top + view) / ROW_PX) + OVERSCAN) : entries.length;
  return (
    <>
      <p id={id} className={s.part}>
        <Visible text={name} />
      </p>
      <ol
        ref={box}
        aria-labelledby={id}
        className={windowed ? s.windowed : s.partList}
        onScroll={windowed ? (e) => setTop(e.currentTarget.scrollTop) : undefined}
        style={windowed ? { paddingTop: first * ROW_PX, paddingBottom: (entries.length - last) * ROW_PX } : undefined}
      >
        {entries.slice(first, last).map((e) => (
          <Row key={e.id} e={e} isCurrent={e.id === current} isRead={read.has(e.id)} onGo={onGo} />
        ))}
      </ol>
    </>
  );
}

export function Outline(props: Props) {
  const parts: { name: string; entries: Entry[] }[] = [];
  for (const e of props.entries) {
    const last = parts[parts.length - 1];
    if (last && last.name === e.part) last.entries.push(e);
    else parts.push({ name: e.part, entries: [e] });
  }
  return (
    <div className={s.entries}>
      {parts.map((p) => (
        <Part key={p.name} name={p.name} {...props} entries={p.entries} />
      ))}
    </div>
  );
}
