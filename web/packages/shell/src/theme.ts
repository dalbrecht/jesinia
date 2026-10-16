// SPDX-License-Identifier: AGPL-3.0-or-later
export type ThemeChoice = "system" | "light" | "dark";
const KEY = "jesinia.theme";

export function readTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

/** system → light → dark → system. */
export function cycleTheme(): ThemeChoice {
  const next: ThemeChoice = { system: "light", light: "dark", dark: "system" }[readTheme()] as ThemeChoice;
  applyTheme(next);
  return next;
}
