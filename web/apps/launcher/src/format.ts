// SPDX-License-Identifier: AGPL-3.0-or-later
import type { GitMode } from "@jesinia/shell";

export const MODE_LABEL: Record<GitMode, string> = {
  off: "Off (not a git repository)",
  commit: "Commit locally",
  push: "Commit and push",
};

export function clock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}
