// SPDX-License-Identifier: AGPL-3.0-or-later
// The reader's single-key commands (spec §2.2). Keys are named as `useKeys` matches them:
// a plain key ("j", "Enter", "/"), "Shift+<LETTER>", or a "g <key>" chord.
import { useEffect, useRef } from "react";

export type KeyMap = Record<string, () => void>;

export const CHORD_TIMEOUT_MS = 1000;
const CHORD_LEADER = "g";

/** What `?` lists (design pass: no on-screen key guide). */
export const KEY_HELP: { keys: string[]; label: string }[] = [
  { keys: ["j", "k"], label: "Next post / previous post" },
  { keys: ["Enter", "o"], label: "Open the original in a new tab" },
  { keys: ["n"], label: "Keep" },
  { keys: ["e"], label: "Engage" },
  { keys: ["m"], label: "Toggle read" },
  { keys: ["Shift+A"], label: "Mark everything above read" },
  { keys: ["g a"], label: "Go to All unread" },
  { keys: ["g i"], label: "Go to In KB" },
  { keys: ["/"], label: "Search headlines" },
  { keys: ["?"], label: "Show these keys" },
];

const EDITABLE = '[contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]';

function isTextField(el: Element | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  // `closest` as well: jsdom (and very old engines) lack isContentEditable.
  return el.isContentEditable || el.closest(EDITABLE) !== null || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
}

/** Enter on a focused control belongs to that control. */
function isControl(el: Element | null): boolean {
  return el instanceof HTMLElement && /^(BUTTON|A|SUMMARY)$/.test(el.tagName);
}

function keyName(e: KeyboardEvent): string {
  return e.shiftKey && /^[A-Z]$/.test(e.key) ? `Shift+${e.key}` : e.key;
}

/**
 * Run `map[key]` for keys pressed anywhere in the page, except while a text field has focus.
 * After `g`, the next key within one second completes a "g <key>" chord or is dropped.
 */
export function useKeys(map: KeyMap): void {
  const latest = useRef(map);
  latest.current = map;

  useEffect(() => {
    let chordUntil = 0;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const active = document.activeElement;
      if (isTextField(active)) return;
      const key = keyName(e);
      if (key === "Enter" && isControl(active)) return;
      const chording = chordUntil > Date.now();
      chordUntil = 0;
      let run: (() => void) | undefined;
      if (chording) run = latest.current[`${CHORD_LEADER} ${key}`];
      else if (key === CHORD_LEADER) {
        chordUntil = Date.now() + CHORD_TIMEOUT_MS;
        e.preventDefault();
        return;
      } else run = latest.current[key];
      if (!run) return;
      e.preventDefault();
      run();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
