// SPDX-License-Identifier: AGPL-3.0-or-later
// PR text with its hidden and direction-changing characters made visible (Trojan Source, I6):
// a bidi control or a zero-width character shows as a styled token such as ⟪U+202E⟫, never
// invisibly, so what the author reads is what the code says.
import { Badge } from "@jesinia/design";
import { revealHidden } from "./model";
import s from "./pr.module.css";

const WHAT = {
  bidi: "a direction-changing (bidi) character",
  invisible: "an invisible character",
  control: "a control character",
} as const;

export function Visible({ text }: { text: string }) {
  const runs = revealHidden(text);
  if (!runs.some((r) => "code" in r)) return <>{text}</>;
  return (
    <>
      {runs.map((run, i) =>
        "code" in run ? (
          <span key={i} className={s.hiddenChar} data-kind={run.kind} title={`${run.code}: ${WHAT[run.kind]}`}>
            {`⟪${run.code}⟫`}
          </span>
        ) : (
          <span key={i}>{run.text}</span>
        ),
      )}
    </>
  );
}

/** The warning on a file or hunk that has any such character. */
export function HiddenBadge() {
  return (
    <Badge tone="warn" icon="⚠">
      hidden or direction-changing characters
    </Badge>
  );
}
