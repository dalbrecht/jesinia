// SPDX-License-Identifier: AGPL-3.0-or-later
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import s from "./List.module.css";
import { cx } from "./util";

export type ListProps<T> = {
  items: T[];
  getKey: (item: T) => string;
  renderRow: (item: T, state: { selected: boolean; focused: boolean }) => ReactNode;
  selection?: "none" | "single" | "multiple";
  selected?: string[];
  onSelectionChange?: (keys: string[]) => void;
  onActivate?: (key: string) => void;
  /** Rows the arrow keys skip (their Row also sets `disabled`). */
  isDisabled?: (item: T) => boolean;
  density?: "desk" | "compact";
  loading?: boolean;
  empty?: ReactNode;
  "aria-label"?: string;
  "aria-labelledby"?: string;
};

function isTextField(el: EventTarget | null): boolean {
  return el instanceof HTMLElement && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}

export function List<T>(props: ListProps<T>) {
  const { items, getKey, renderRow, selection = "none", selected = [], density, loading, empty } = props;
  const keys = items.map(getKey);
  const disabled = new Set(items.filter((i) => props.isDisabled?.(i)).map(getKey));
  const firstSelected = keys.findIndex((k) => selected.includes(k));
  const [focusIndex, setFocusIndex] = useState(firstSelected >= 0 ? firstSelected : 0);
  const anchor = useRef<number | null>(null);
  const nodes = useRef(new Map<string, HTMLElement>());
  const aria = { "aria-label": props["aria-label"], "aria-labelledby": props["aria-labelledby"] };
  const densityAttr = density ? { "data-density": density } : {};

  if (loading) {
    return (
      <div {...aria} {...densityAttr} aria-busy="true" className={s.list}>
        {[0, 1, 2].map((i) => (
          <div key={i} className={s.skeleton} data-skeleton="">
            <div className={s.bar} style={{ width: "40%" }} />
            <div className={s.bar} style={{ width: "75%" }} />
            <div className={s.bar} style={{ width: "25%" }} />
          </div>
        ))}
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div {...aria} {...densityAttr} className={cx(s.list, s.empty)}>
        {empty ?? "Nothing here yet."}
      </div>
    );
  }

  if (selection === "none") {
    return (
      <ul {...aria} {...densityAttr} className={s.list}>
        {items.map((item, i) => (
          <li key={keys[i]} className={s.item}>
            {renderRow(item, { selected: false, focused: false })}
          </li>
        ))}
      </ul>
    );
  }

  const focusAt = (i: number) => {
    setFocusIndex(i);
    const key = keys[i];
    if (key !== undefined) nodes.current.get(key)?.focus();
    return key;
  };
  const step = (from: number, dir: 1 | -1) => {
    for (let i = from + dir; i >= 0 && i < keys.length; i += dir) {
      if (!disabled.has(keys[i] ?? "")) return i;
    }
    return from;
  };
  const edge = (dir: 1 | -1) => step(dir === 1 ? -1 : keys.length, dir);
  const select = (next: string[]) => props.onSelectionChange?.(next);
  const range = (a: number, b: number) =>
    keys.slice(Math.min(a, b), Math.max(a, b) + 1).filter((k) => !disabled.has(k));

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const down = e.key === "ArrowDown" || (e.key === "j" && !isTextField(e.target));
    const up = e.key === "ArrowUp" || (e.key === "k" && !isTextField(e.target));
    let next: number | null = null;
    if (down) next = step(focusIndex, 1);
    else if (up) next = step(focusIndex, -1);
    else if (e.key === "Home") next = edge(1);
    else if (e.key === "End") next = edge(-1);
    if (next !== null) {
      e.preventDefault();
      const key = focusAt(next);
      if (key === undefined) return;
      if (selection === "single") select([key]);
      else if (e.shiftKey) {
        anchor.current ??= focusIndex;
        select(range(anchor.current, next));
      } else anchor.current = null;
      return;
    }
    const key = keys[focusIndex];
    if (key === undefined || disabled.has(key)) return;
    if (e.key === "Enter") {
      e.preventDefault();
      props.onActivate?.(key);
    } else if (e.key === " ") {
      e.preventDefault();
      if (selection === "multiple") {
        select(selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key]);
      } else props.onActivate?.(key);
    } else if (e.key === "a" && (e.metaKey || e.ctrlKey) && selection === "multiple") {
      e.preventDefault();
      select(keys.filter((k) => !disabled.has(k)));
    }
  };

  return (
    <div
      {...aria}
      {...densityAttr}
      role="listbox"
      aria-multiselectable={selection === "multiple" || undefined}
      className={s.list}
      onKeyDown={onKeyDown}
    >
      {items.map((item, i) => {
        const key = keys[i] ?? "";
        const isSelected = selected.includes(key);
        const isDisabled = disabled.has(key);
        return (
          <div
            key={key}
            ref={(el) => {
              if (el) nodes.current.set(key, el);
              else nodes.current.delete(key);
            }}
            role="option"
            aria-selected={isSelected}
            aria-disabled={isDisabled || undefined}
            tabIndex={i === focusIndex ? 0 : -1}
            className={cx(s.item, s.interactive)}
            onClick={(e) => {
              if (isDisabled) return;
              setFocusIndex(i);
              if (selection === "single") select([key]);
              else if (e.metaKey || e.ctrlKey) {
                select(isSelected ? selected.filter((k) => k !== key) : [...selected, key]);
              } else if (e.shiftKey) select(range(anchor.current ?? focusIndex, i));
              else {
                anchor.current = i;
                select([key]);
              }
            }}
            onDoubleClick={() => !isDisabled && props.onActivate?.(key)}
          >
            {renderRow(item, { selected: isSelected, focused: i === focusIndex })}
          </div>
        );
      })}
    </div>
  );
}

export type RowProps = {
  title: ReactNode;
  excerpt?: ReactNode;
  meta?: ReactNode;
  leading?: ReactNode;
  trailing?: ReactNode;
  unread?: boolean;
  disabled?: boolean;
};

export function Row({ title, excerpt, meta, leading, trailing, unread, disabled }: RowProps) {
  return (
    <div className={cx(s.row, unread && s.unread)} aria-disabled={disabled || undefined}>
      <span className={s.leading}>
        {leading ?? (unread ? <span className={s.dot} aria-label="Unread" role="img" /> : null)}
      </span>
      <div className={s.main}>
        <div className={s.title} data-row-title="">
          {title}
        </div>
        {excerpt !== undefined ? <div className={s.excerpt}>{excerpt}</div> : null}
        {meta !== undefined ? <div className={s.meta}>{meta}</div> : null}
      </div>
      {trailing !== undefined ? <span className={s.trailing}>{trailing}</span> : <span />}
    </div>
  );
}
