// SPDX-License-Identifier: AGPL-3.0-or-later
// The recommendation (spec §3.2): Jesinia's verdict (its evidence rule decides, never the
// agent), the reasons, the evidence list, the agent's advisory suggestion, and the drafts.
// Nothing goes to GitHub until the author chooses (spec §7).
import { SafeMarkdown } from "@jesinia/shell";
import type { ReactNode } from "react";
import { DraftsEditor, type DraftsState } from "./Drafts";
import { coverageLine, type Artifacts } from "./model";
import s from "./pr.module.css";

export type VerdictWords = { text: string; tone: "success" | "danger" | "warn" };

const VERDICTS: Record<string, VerdictWords> = {
  approve: { text: "✓ Approve", tone: "success" },
  request_changes: { text: "✕ Request changes", tone: "danger" },
  needs_closer_look: { text: "? Needs a closer look", tone: "warn" },
};
// Your own PR (spec §3.3): the verdict reads as ready (or not) to request review.
const OWN: Record<string, VerdictWords> = {
  approve: { text: "✓ Ready to request review", tone: "success" },
  request_changes: { text: "✕ Not ready to request review", tone: "danger" },
  needs_closer_look: { text: "? Needs a closer look before you request review", tone: "warn" },
};

export function verdictWords(verdict: string | null | undefined, own: boolean): VerdictWords | null {
  if (!verdict) return null;
  return (own ? OWN : VERDICTS)[verdict] ?? { text: verdict, tone: "warn" };
}

type Props = {
  artifacts: Artifacts;
  verdict: string | null;
  own: boolean;
  drafts: DraftsState | null;
  /** The review actions under the drafts (Done without posting). */
  children?: ReactNode;
};

export function Recommendation({ artifacts, verdict, own, drafts, children }: Props) {
  const words = verdictWords(verdict, own);
  const reasons = artifacts.verdict?.reasons ?? [];
  const ev = artifacts.evidence;
  const read = ev?.correctness_coverage?.pct;
  const findings = Object.entries(ev?.findings ?? {})
    .filter(([, n]) => n > 0)
    .map(([sev, n]) => `${n} ${sev}`);
  const walk = coverageLine(artifacts.walk, artifacts.nothingToWalk);
  return (
    <article className={s.page} aria-label="Recommendation">
      <h2>Recommendation</h2>
      {words ? (
        <p className={s.bigVerdict} data-tone={words.tone}>
          {words.text}
        </p>
      ) : (
        <p className={s.muted}>No verdict yet: the pre-review hasn't finished.</p>
      )}
      {reasons.length ? (
        <ul className={s.reasons}>
          {reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      ) : null}
      {ev ? (
        <section className={s.group}>
          <h3>Evidence</h3>
          <ul className={s.refs}>
            <li>{`CI: ${ev.ci}`}</li>
            {ev.checks.map((c, i) => (
              <li key={i} className={s.muted}>{`${c.name}: ${c.status}`}</li>
            ))}
            <li>{`Tests: ${ev.tests ?? "none found by the agent"}`}</li>
            <li>
              {read === null || read === undefined
                ? `Correctness pass: ${ev.correctness ?? "not run"}`
                : `Correctness pass read ${Math.round(read)}% of changed lines (${ev.correctness ?? "unknown"})`}
            </li>
            <li>{`Findings: ${findings.length ? findings.join(" · ") : "none"}`}</li>
            {walk ? <li>{walk}</li> : null}
          </ul>
        </section>
      ) : null}
      {artifacts.suggestion ? (
        <section className={s.group}>
          <h3>The agent's suggestion</h3>
          <div className={s.prose}>
            <SafeMarkdown markdown={artifacts.suggestion.reasoning} />
          </div>
        </section>
      ) : null}
      <p className={s.advisory}>Advisory only: nothing goes to GitHub until you choose an action below.</p>
      {drafts ? (
        <section className={s.group}>
          <h3>Your comments</h3>
          <DraftsEditor drafts={drafts} />
        </section>
      ) : null}
      {children}
    </article>
  );
}
