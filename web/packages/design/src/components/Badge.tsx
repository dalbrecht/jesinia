// SPDX-License-Identifier: AGPL-3.0-or-later
import type { HTMLAttributes, ReactNode } from "react";
import s from "./Badge.module.css";
import { cx } from "./util";

export type LabelTone = "marigold" | "plum" | "cornflower" | "clay" | "fern";
export type BadgeProps = (
  | { tone?: "neutral" | LabelTone; icon?: ReactNode; children: ReactNode }
  | { tone: "success" | "warn" | "danger"; icon: ReactNode; children: ReactNode }
) &
  HTMLAttributes<HTMLSpanElement>;

export function Badge({ tone = "neutral", icon, children, className, ...rest }: BadgeProps) {
  return (
    <span {...rest} className={cx(s.badge, s[tone], className)}>
      {icon ? <span aria-hidden="true">{icon}</span> : null}
      {children}
    </span>
  );
}

const LABEL_TONES: readonly LabelTone[] = ["marigold", "plum", "cornflower", "clay", "fern"];

/** A stable tone for a tag or label name (FNV-1a over the casefolded name). */
export function toneFor(name: string): LabelTone {
  let h = 0x811c9dc5;
  for (const ch of name.toLowerCase()) {
    h ^= ch.codePointAt(0) ?? 0;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return LABEL_TONES[h % LABEL_TONES.length] ?? "marigold";
}
