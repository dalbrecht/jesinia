// SPDX-License-Identifier: AGPL-3.0-or-later
// Untrusted text with its hidden and direction-changing characters made visible (Trojan Source,
// R62): a bidi control, a zero-width or invisible character, or a control character shows as a
// styled token such as ⟪U+202E⟫, never invisibly, so what the author reads is what is there.
// Shared by every app that shows a name anyone could write (PR files, folder names).
import s from "./visible.module.css";

export type HiddenKind = "bidi" | "invisible" | "control";
export type HiddenRun = { text: string } | { code: string; kind: HiddenKind };

export type RevealOptions = {
  /**
   * The text is a multi-line body (a review comment, a brief): LF and the CR of a CRLF are
   * ordinary line breaks. Off by default, because in a name (a path, a folder) a newline is
   * itself a disguise worth showing.
   */
  multiline?: boolean;
};

function hiddenKind(cp: number): HiddenKind | null {
  if (
    (cp >= 0x202a && cp <= 0x202e) ||
    (cp >= 0x2066 && cp <= 0x2069) ||
    cp === 0x200e ||
    cp === 0x200f ||
    cp === 0x061c
  )
    return "bidi";
  if (
    (cp >= 0x200b && cp <= 0x200d) ||
    (cp >= 0x2060 && cp <= 0x2064) ||
    // U+2028 and U+2029, the line and paragraph separators, break lines unseen.
    [0xfeff, 0x00ad, 0x180e, 0x034f, 0x115f, 0x1160, 0x3164, 0xffa0, 0x2028, 0x2029].includes(cp)
  )
    return "invisible";
  if ((cp < 0x20 && cp !== 0x09) || (cp >= 0x7f && cp <= 0x9f)) return "control";
  return null;
}

const code = (cp: number) => `U+${cp.toString(16).toUpperCase().padStart(4, "0")}`;

/**
 * Text split into plain runs and the characters that hide or reorder what is read: bidi
 * controls, zero-width and invisible characters, and control characters (a tab, and the CR of a
 * CRLF line ending, are ordinary). Each becomes a token the UI shows visibly. With
 * `multiline`, line feeds and the CR of a CRLF are ordinary too; a lone CR, U+2028 and every
 * other control character still show.
 */
export function revealHidden(text: string, options: RevealOptions = {}): HiddenRun[] {
  const out: HiddenRun[] = [];
  let plain = "";
  const chars = [...text];
  chars.forEach((ch, i) => {
    const cp = ch.codePointAt(0) as number;
    const isLineBreak = options.multiline === true && (cp === 0x0a || (cp === 0x0d && chars[i + 1] === "\n"));
    const kind = isLineBreak || (cp === 0x0d && i === chars.length - 1) ? null : hiddenKind(cp);
    if (kind === null) {
      plain += ch;
      return;
    }
    if (plain) out.push({ text: plain });
    plain = "";
    out.push({ code: code(cp), kind });
  });
  if (plain || out.length === 0) out.push({ text: plain });
  return out;
}

/** Text with each hidden character as its token, for accessible names and plain strings. */
export function visibleText(text: string, options: RevealOptions = {}): string {
  return revealHidden(text, options)
    .map((r) => ("code" in r ? `⟪${r.code}⟫` : r.text))
    .join("");
}

export function hasHidden(text: string, options: RevealOptions = {}): boolean {
  return revealHidden(text, options).some((r) => "code" in r);
}

const WHAT: Record<HiddenKind, string> = {
  bidi: "a direction-changing (bidi) character",
  invisible: "an invisible character",
  control: "a control character",
};

/**
 * `text` rendered as text, each hidden character a visible, explained token. A `multiline` body
 * keeps its line breaks in the text, so the parent needs `white-space: pre-wrap` to show them.
 */
export function VisibleText({ text, multiline = false }: { text: string; multiline?: boolean }) {
  const runs = revealHidden(text, { multiline });
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
