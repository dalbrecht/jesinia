// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import s from "./CommandPalette.module.css";
import { VisuallyHidden } from "./Spinner";

export type PaletteItem = {
  id: string;
  label: string;
  detail?: string;
  group: "app" | "kb" | "command";
  icon?: ReactNode;
  onRun: (opts: { newWindow: boolean }) => void;
};
type Search = (q: string, signal: AbortSignal) => Promise<PaletteItem[]>;

export type CommandPaletteProps = {
  appSearch?: Search;
  /** The "Knowledge base" group (the search service); supplied by the app shell. */
  kbSearch?: Search;
  commands?: PaletteItem[];
};

const GROUPS: { id: PaletteItem["group"]; label: string }[] = [
  { id: "app", label: "In this app" },
  { id: "kb", label: "Knowledge base" },
  { id: "command", label: "Commands" },
];
const PER_GROUP = 6;
const DEBOUNCE_MS = 100;

function useDebounced(value: string, ms: number): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function useSearch(fn: Search | undefined, q: string): PaletteItem[] {
  const [items, setItems] = useState<PaletteItem[]>([]);
  useEffect(() => {
    if (!fn || !q) {
      setItems([]);
      return;
    }
    const ctl = new AbortController();
    fn(q, ctl.signal).then(
      (r) => !ctl.signal.aborted && setItems(r),
      () => !ctl.signal.aborted && setItems([]),
    );
    return () => ctl.abort();
  }, [fn, q]);
  return items;
}

export function CommandPalette({ appSearch, kbSearch, commands = [] }: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const invoker = useRef<HTMLElement | null>(null);
  const field = useRef<HTMLInputElement>(null);
  const base = useId();
  const q = useDebounced(query.trim(), DEBOUNCE_MS);
  const appItems = useSearch(open ? appSearch : undefined, q);
  const kbItems = useSearch(open ? kbSearch : undefined, q);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        invoker.current = document.activeElement as HTMLElement | null;
        setOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) field.current?.focus();
  }, [open]);

  const groups = useMemo(() => {
    const needle = q.toLowerCase();
    const cmds = needle ? commands.filter((c) => c.label.toLowerCase().includes(needle)) : commands;
    const all = { app: appItems, kb: kbItems, command: cmds };
    return GROUPS.map((g) => {
      const items = all[g.id];
      const showAll = expanded.has(g.id) || items.length <= PER_GROUP;
      const shown: PaletteItem[] = showAll ? items : items.slice(0, PER_GROUP);
      if (!showAll) {
        shown.push({
          id: `${g.id}-more`,
          label: `Show all ${items.length}`,
          group: g.id,
          onRun: () => setExpanded((x) => new Set(x).add(g.id)),
        });
      }
      return { ...g, items: shown, total: items.length };
    }).filter((g) => g.items.length > 0);
  }, [q, commands, appItems, kbItems, expanded]);

  const flat = groups.flatMap((g) => g.items);
  const count = groups.reduce((n, g) => n + g.total, 0);
  const activeItem = flat[Math.min(active, flat.length - 1)];

  const close = () => {
    setOpen(false);
    setQuery("");
    setActive(0);
    setExpanded(new Set());
    invoker.current?.focus();
  };

  const run = (item: PaletteItem, newWindow: boolean) => {
    const keepOpen = item.id.endsWith("-more");
    item.onRun({ newWindow });
    if (!keepOpen) close();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter" && activeItem) {
      e.preventDefault();
      run(activeItem, e.metaKey || e.ctrlKey);
    } else if (e.key === "Tab") {
      e.preventDefault(); // focus stays in the dialog
    }
  };

  if (!open) return null;
  const optionId = (item: PaletteItem) => `${base}-opt-${item.id}`;
  return (
    <>
      <div className={s.scrim} onClick={close} data-scrim="" />
      <div role="dialog" aria-modal="true" aria-label="Command palette" className={s.dialog} onKeyDown={onKeyDown}>
        <input
          ref={field}
          className={s.query}
          role="combobox"
          aria-expanded={flat.length > 0}
          aria-controls={`${base}-list`}
          aria-activedescendant={activeItem ? optionId(activeItem) : undefined}
          aria-label="Search pages and commands"
          placeholder="Search pages and commands"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
        />
        <div id={`${base}-list`} role="listbox" aria-label="Results" className={s.results}>
          {groups.length === 0 ? <div className={s.none}>No matches</div> : null}
          {groups.map((g) => (
            <div key={g.id} role="group" aria-label={g.label}>
              <div className={s.groupLabel} aria-hidden="true">
                {g.label}
              </div>
              {g.items.map((item) => (
                <div
                  key={item.id}
                  id={optionId(item)}
                  role="option"
                  aria-selected={item === activeItem}
                  className={s.item}
                  onMouseMove={() => setActive(flat.indexOf(item))}
                  onClick={(e) => run(item, e.metaKey || e.ctrlKey)}
                >
                  {item.icon ? <span aria-hidden="true">{item.icon}</span> : null}
                  <span className={s.label}>{item.label}</span>
                  {item.detail ? <span className={s.detail}>{item.detail}</span> : null}
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className={s.footer} aria-hidden="true">
          <span>↑↓ move</span>
          <span>↵ open</span>
          <span>⌘↵ new window</span>
          <span>esc close</span>
        </div>
        <div aria-live="polite">
          <VisuallyHidden>{`${count} results`}</VisuallyHidden>
        </div>
      </div>
    </>
  );
}
