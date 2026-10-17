// SPDX-License-Identifier: AGPL-3.0-or-later
// §9 copy for a request that got no answer (the server is restarting or stopped): the cause,
// then the fix.

/** The next step when Jesinia never answered. */
export const UNREACHABLE = "Check that Jesinia is running, then try again.";

/** `text` as a sentence, so a next step can follow it. */
export function sentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/** Why a request got no answer, with what to do: "Failed to fetch. Check that Jesinia…". */
export function unreachable(e: unknown): string {
  return `${sentence(e instanceof Error ? e.message : String(e))} ${UNREACHABLE}`;
}
