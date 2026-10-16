// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useId, useRef, useState } from "react";
import { Panel } from "./Panel";
import s from "./StatusIndicator.module.css";
import { cx } from "./util";

export type StatusItem = { id: string; severity: "minor" | "blocking"; title: string; detail: string; href?: string };

export function StatusIndicator({ items }: { items: StatusItem[] }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const popId = useId();
  const level = items.some((i) => i.severity === "blocking") ? "blocking" : items.length ? "minor" : "clear";
  const label =
    items.length === 0 ? "Status: all clear" : `Status: ${items.length} ${items.length === 1 ? "issue" : "issues"}`;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div
      ref={wrap}
      className={s.wrap}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.preventDefault();
          setOpen(false);
          button.current?.focus();
        }
      }}
    >
      <button
        ref={button}
        type="button"
        className={s.button}
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen(!open)}
      >
        <span className={cx(s.dot, s[level])} aria-hidden="true" />
      </button>
      {open ? (
        <div id={popId} role="dialog" aria-label="System status details" className={s.popover}>
          <Panel variant="raised" title="Status" headingLevel={3}>
            {items.length === 0 ? (
              <p className={s.none}>Everything is working.</p>
            ) : (
              <ul className={s.items}>
                {items.map((i) => (
                  <li key={i.id} className={s.item}>
                    <span className={i.severity === "blocking" ? s.blockingIcon : s.minorIcon} aria-hidden="true">
                      {i.severity === "blocking" ? "×" : "!"}
                    </span>
                    <strong>{i.title}</strong>
                    <span className={s.fix}>{i.detail}</span>
                    {i.href ? (
                      <a className={s.link} href={i.href}>
                        Open settings
                      </a>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      ) : null}
    </div>
  );
}
