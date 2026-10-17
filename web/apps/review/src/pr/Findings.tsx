// SPDX-License-Identifier: AGPL-3.0-or-later
// Findings (spec §5.3): grouped as correctness, design and style, never merged across groups,
// each with its severity, location, explanation and rule. Agent output: safe Markdown only.
import { Badge, Button } from "@jesinia/design";
import { SafeMarkdown } from "@jesinia/shell";
import { GROUP_LABEL, GROUPS, lines, SEVERITY, visibleText, type Finding } from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";

const TONE = { critical: "danger", important: "danger", minor: "neutral" } as const;
const ICON = { critical: "!", important: "!", minor: "·" } as const;

type CardProps = { finding: Finding; showGroup?: boolean; onDraft?: (() => void) | undefined };

export function FindingCard({ finding, showGroup = false, onDraft }: CardProps) {
  const where = `${finding.path}:${lines(finding)}`;
  return (
    <div className={s.finding} data-severity={finding.severity}>
      <div className={s.findingHead}>
        <Badge tone={TONE[finding.severity]} icon={ICON[finding.severity]}>
          {SEVERITY[finding.severity] ?? finding.severity}
        </Badge>
        {showGroup ? <span className={s.muted}>{GROUP_LABEL[finding.group]}</span> : null}
        <span className={s.loc}>
          <Visible text={where} />
        </span>
      </div>
      <div className={s.prose}>
        <SafeMarkdown markdown={finding.explanation} />
      </div>
      {finding.rule ? <span className={s.muted}>{`${finding.rule.id} · ${finding.rule.layer}`}</span> : null}
      {onDraft ? (
        <span>
          <Button size="sm" variant="ghost" aria-label={`Draft a comment on ${visibleText(where)}`} onClick={onDraft}>
            Draft a comment
          </Button>
        </span>
      ) : null}
    </div>
  );
}

export function Findings({ findings }: { findings: Finding[] }) {
  return (
    <article className={s.page} aria-label="Findings">
      <h2>Findings</h2>
      {GROUPS.map((group) => {
        const mine = findings.filter((f) => f.group === group);
        return (
          <section key={group} className={s.group}>
            <h3>{`${GROUP_LABEL[group]} · ${mine.length}`}</h3>
            {mine.length === 0 ? <p className={s.muted}>No {group} findings.</p> : null}
            {mine.map((f, i) => (
              <FindingCard key={i} finding={f} />
            ))}
          </section>
        );
      })}
    </article>
  );
}
