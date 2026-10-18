// SPDX-License-Identifier: AGPL-3.0-or-later
// PR text with its hidden and direction-changing characters made visible (Trojan Source, I6):
// a bidi control or a zero-width character shows as a styled token such as ⟪U+202E⟫, never
// invisibly, so what the author reads is what the code says. The rendering is the shell's
// (`VisibleText`), shared with every app that shows untrusted names (R62).
import { Badge } from "@jesinia/design";
import { VisibleText } from "@jesinia/shell";

export function Visible({ text }: { text: string }) {
  return <VisibleText text={text} />;
}

/** The warning on a file or hunk that has any such character. */
export function HiddenBadge() {
  return (
    <Badge tone="warn" icon="⚠">
      hidden or direction-changing characters
    </Badge>
  );
}
