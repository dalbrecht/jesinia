// SPDX-License-Identifier: AGPL-3.0-or-later
// The menu equivalent of a drop target (spec §6.1: "Move to epic…", "Add to queue…", "Add
// theme…"). A button opens a small menu of choices; arrows move between them, Escape closes and
// returns focus to the button. A choice a drop would refuse is listed, disabled, with the same
// reason, so the menu and the drag never disagree. Choices may load when the menu opens.
import { Button, Input } from "@jesinia/design";
import { VisibleText } from "@jesinia/shell";
import { useEffect, useId, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import s from "./issues.module.css";

export type MenuOption = {
  id: string;
  label: string;
  /** Why this choice is refused (shown, and the choice disabled); null or absent to allow it. */
  refusal?: string | null;
  /** A note beside the choice, such as "won't fit one PR". */
  hint?: string | null;
};

type Options = readonly MenuOption[] | (() => Promise<readonly MenuOption[]>);

export type ActionMenuProps = {
  /** The button's visible text, such as "Move to epic…". */
  label: string;
  /** The button's accessible name, naming what it acts on ("Move acme/app#1 to an epic"). */
  name: string;
  options: Options;
  onPick: (id: string) => void;
  /** A free-text choice (a new theme): its field label and what it does. */
  create?: { label: string; onCreate: (text: string) => void; submitLabel?: string } | undefined;
  /** Shown when there is nothing to pick. */
  emptyText?: string;
  disabled?: boolean;
};

export function ActionMenu({ label, name, options, onPick, create, emptyText, disabled }: ActionMenuProps) {
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<readonly MenuOption[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const trigger = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const wrap = useRef<HTMLDivElement>(null);

  // Callers pass a fresh `options` on every render (a poll, an ask's progress, a tray change):
  // read it through a ref so an open menu loads once, when it opens, and keeps its place.
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const justOpened = useRef(false);

  useEffect(() => {
    if (!open) return;
    justOpened.current = true;
    const current = optionsRef.current;
    if (typeof current !== "function") {
      setLoaded(current);
      return;
    }
    let isCurrent = true;
    setLoaded(null);
    setProblem(null);
    current().then(
      (found) => isCurrent && setLoaded(found),
      (e: unknown) => isCurrent && setProblem(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      isCurrent = false;
    };
  }, [open]);

  // A static list follows the caller's latest choices while open (a refusal may change).
  const shown = open && typeof options !== "function" ? options : loaded;

  // Focus the first choice once, when the opened menu first shows its choices.
  useEffect(() => {
    if (!open || shown === null || !justOpened.current) return;
    justOpened.current = false;
    menu.current?.querySelector<HTMLElement>("[role=menuitem], input")?.focus();
  }, [open, shown]);

  // A pointer-down outside closes it. Blur alone can't decide: Safari and macOS Firefox don't
  // focus a clicked button, so a mouse pick blurs with no next focus before its click lands.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node | null)) close(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const close = (refocus: boolean) => {
    setOpen(false);
    setDraft("");
    if (refocus) trigger.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = [...(menu.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    if (items.length === 0) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next = e.key === "ArrowDown" ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
    items[next]?.focus();
  };

  // Tabbing away closes it: focus moved somewhere known, outside the menu.
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next !== null && !e.currentTarget.contains(next)) close(false);
  };

  const pick = (option: MenuOption) => {
    if (option.refusal) return;
    close(true);
    onPick(option.id);
  };

  const submit = () => {
    const text = draft.trim();
    if (!text || !create) return;
    close(true);
    create.onCreate(text);
  };

  return (
    <div ref={wrap} className={s.menuWrap} onBlur={onBlur}>
      <Button
        ref={trigger}
        size="sm"
        variant="ghost"
        aria-label={name}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        {label}
      </Button>
      {open ? (
        <div ref={menu} id={menuId} className={s.menu} onKeyDown={onKeyDown}>
          <ul role="menu" aria-label={name} className={s.menuList}>
            {problem !== null ? (
              <li role="none" className={s.muted}>{`Couldn't load the choices: ${problem}`}</li>
            ) : shown === null ? (
              <li role="none" className={s.muted}>
                Loading…
              </li>
            ) : shown.length === 0 ? (
              <li role="none" className={s.muted}>
                {emptyText ?? "Nothing to pick."}
              </li>
            ) : (
              shown.map((option) => (
                <li key={option.id} role="none">
                  <button
                    type="button"
                    role="menuitem"
                    className={s.menuItem}
                    aria-disabled={option.refusal ? true : undefined}
                    onClick={() => pick(option)}
                  >
                    <VisibleText text={option.label} />
                    {option.refusal ? <span className={s.menuWhy}>{`Can't: ${option.refusal}`}</span> : null}
                    {!option.refusal && option.hint ? <span className={s.menuWhy}>{option.hint}</span> : null}
                  </button>
                </li>
              ))
            )}
          </ul>
          {create ? (
            <form
              className={s.menuCreate}
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
            >
              <Input label={create.label} hideLabel value={draft} onChange={(e) => setDraft(e.target.value)} />
              <Button size="sm" type="submit" disabled={draft.trim() === ""}>
                {create.submitLabel ?? "Add"}
              </Button>
            </form>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
