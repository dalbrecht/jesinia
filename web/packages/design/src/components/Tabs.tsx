// SPDX-License-Identifier: AGPL-3.0-or-later
import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Badge } from "./Badge";
import s from "./Tabs.module.css";

export type TabsProps = {
  /** `title`: the full text on hover, for a label shortened to fit. */
  tabs: { id: string; label: string; count?: number; disabled?: boolean; title?: string }[];
  selected: string;
  onSelect: (id: string) => void;
  "aria-label": string;
  children: (selectedId: string) => ReactNode;
};

export function Tabs({ tabs, selected, onSelect, children, ...aria }: TabsProps) {
  const base = useId();
  const refs = useRef(new Map<string, HTMLButtonElement>());
  const enabled = tabs.filter((t) => !t.disabled);

  const move = (e: KeyboardEvent<HTMLDivElement>) => {
    const at = enabled.findIndex((t) => t.id === selected);
    let next: number | null = null;
    if (e.key === "ArrowRight") next = (at + 1) % enabled.length;
    else if (e.key === "ArrowLeft") next = (at - 1 + enabled.length) % enabled.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = enabled.length - 1;
    const target = next === null ? undefined : enabled[next];
    if (!target) return;
    e.preventDefault();
    onSelect(target.id);
    refs.current.get(target.id)?.focus();
  };

  return (
    <div>
      <div role="tablist" aria-label={aria["aria-label"]} className={s.list} onKeyDown={move}>
        {tabs.map((t) => {
          const isSelected = t.id === selected;
          return (
            <button
              key={t.id}
              ref={(el) => {
                if (el) refs.current.set(t.id, el);
                else refs.current.delete(t.id);
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${t.id}`}
              className={s.tab}
              aria-selected={isSelected}
              aria-controls={`${base}-panel`}
              aria-disabled={t.disabled || undefined}
              title={t.title}
              tabIndex={isSelected ? 0 : -1}
              onClick={() => !t.disabled && onSelect(t.id)}
            >
              {t.label}
              {t.count !== undefined ? <Badge>{t.count}</Badge> : null}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`${base}-panel`}
        aria-labelledby={`${base}-tab-${selected}`}
        tabIndex={0}
        className={s.panel}
      >
        {children(selected)}
      </div>
    </div>
  );
}
