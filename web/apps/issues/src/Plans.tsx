// SPDX-License-Identifier: AGPL-3.0-or-later
// Drafts shown as a diff to approve (spec §4, §5.2): a checklist conversion or a theme's epic
// (one batch: the epic, then each child linked), and the agent's proposals (a new epic for a
// group of issues, additions to an epic, a split). Every title and reason here came from an
// issue's author or the agent: plain text, hidden characters shown as tokens (R62). Nothing is
// written until Approve, and approving a group, split or plan applies one batch through the
// engine, followed in the tray.
import { Badge, Button } from "@jesinia/design";
import { VisibleText } from "@jesinia/shell";
import type { ReactNode } from "react";
import { describeSuggestion, type PlanChild, type Suggestion } from "./api";
import s from "./issues.module.css";

type PlannedEpic = { kind: "create"; title: string } | { kind: "existing"; repo: string; n: number };

export type PlanDiffProps = {
  /** The plan's name, such as "Convert the checklist of acme/app#10". */
  title: string;
  epic: PlannedEpic | null;
  children: readonly PlanChild[];
  skipped: readonly { item: string; reason: string }[];
  onApprove: () => void;
  onCancel: () => void;
  busy: boolean;
  /** Why the last approval was refused, with what to do (Retry in the tray, a new epic). */
  problem?: string | null;
  actions?: ReactNode;
};

export function PlanDiff({
  title,
  epic,
  children,
  skipped,
  onApprove,
  onCancel,
  busy,
  problem,
  actions,
}: PlanDiffProps) {
  return (
    <section className={s.plan} aria-label={title}>
      <h3 className={s.planTitle}>
        <VisibleText text={title} />
      </h3>
      <ul className={s.diff}>
        {epic?.kind === "create" ? (
          <li className={s.diffAdd}>
            <span aria-hidden="true">+</span> Create the epic <VisibleText text={epic.title} />
          </li>
        ) : null}
        {epic?.kind === "existing" ? (
          <li className={s.diffKeep}>{`Link under the theme's epic ${epic.repo}#${epic.n}`}</li>
        ) : null}
        {children.map((child, i) => (
          <li key={child.temp_id ?? `${child.repo ?? ""}#${child.n ?? i}`} className={s.diffAdd}>
            <span aria-hidden="true">+</span>{" "}
            {child.is_new ? "New issue " : `Link ${child.repo ?? ""}#${child.n ?? ""} `}
            <VisibleText text={child.title} />
            {child.warning ? (
              <Badge tone="warn" icon="⚠">
                {child.warning}
              </Badge>
            ) : null}
          </li>
        ))}
        {skipped.map((skip) => (
          <li key={skip.item} className={s.diffSkip}>
            <span aria-hidden="true">–</span> Skipped <VisibleText text={skip.item} />:{" "}
            <VisibleText text={skip.reason} />
          </li>
        ))}
      </ul>
      {children.length === 0 ? <p className={s.muted}>Nothing to link: there is nothing to approve.</p> : null}
      {problem ? (
        <p className={s.planProblem} role="alert">
          {problem}
        </p>
      ) : null}
      <div className={s.planActions}>
        <Button size="sm" variant="primary" loading={busy} disabled={children.length === 0} onClick={onApprove}>
          Approve
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        {actions}
      </div>
    </section>
  );
}

type Ref = { repo?: unknown; n?: unknown };
const refText = (r: Ref) => `${String(r.repo)}#${String(r.n)}`;

/** A group or split draft as a diff: what approving creates and links. */
export function ProposalDiff({ suggestion }: { suggestion: Suggestion }) {
  const p = suggestion.payload;
  const issues = (Array.isArray(p.issues) ? p.issues : []) as Ref[];
  const parts = (Array.isArray(p.parts) ? p.parts : []) as { title?: unknown }[];
  const epic = p.epic as Ref | undefined;
  return (
    <ul className={s.diff}>
      {suggestion.kind === "group" && epic === undefined ? (
        <li className={s.diffAdd}>
          <span aria-hidden="true">+</span> Create the epic <VisibleText text={String(p.title ?? "")} />
          {p.cross_repo === true ? (
            <Badge tone="warn" icon="⚠">
              won&apos;t fit one PR
            </Badge>
          ) : null}
        </li>
      ) : null}
      {suggestion.kind === "group" && typeof p.scope === "string" && p.scope ? (
        <li className={s.diffKeep}>
          <VisibleText text={p.scope} />
        </li>
      ) : null}
      {suggestion.kind === "group"
        ? issues.map((issue) => (
            <li key={refText(issue)} className={s.diffAdd}>
              <span aria-hidden="true">+</span> {`Link ${refText(issue)} under ${epic ? refText(epic) : "it"}`}
            </li>
          ))
        : null}
      {suggestion.kind === "split"
        ? parts.map((part, i) => (
            <li key={i} className={s.diffAdd}>
              <span aria-hidden="true">+</span> New part <VisibleText text={String(part.title ?? "")} />
            </li>
          ))
        : null}
    </ul>
  );
}

/** A proposal with its diff, the agent's reason, and Approve and Dismiss. */
export function ProposalCard({
  suggestion,
  busy,
  onApprove,
  onDismiss,
  problem,
  actions,
}: {
  suggestion: Suggestion;
  busy: boolean;
  onApprove: () => void;
  onDismiss: () => void;
  problem?: string | null;
  actions?: ReactNode;
}) {
  const what = describeSuggestion(suggestion);
  return (
    <li className={s.proposal} aria-label={what}>
      <div className={s.sugWhat}>{what}</div>
      <ProposalDiff suggestion={suggestion} />
      <span className={s.why}>
        <VisibleText text={suggestion.reason} />
      </span>
      {suggestion.apply_error ? (
        <span className={s.sugError}>
          <VisibleText text={`Last approval didn't apply: ${suggestion.apply_error}`} />
        </span>
      ) : null}
      {problem ? (
        <p className={s.planProblem} role="alert">
          {problem}
        </p>
      ) : null}
      <div className={s.planActions}>
        <Button size="sm" variant="primary" loading={busy} onClick={onApprove} aria-label={`Approve: ${what}`}>
          Approve
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss} aria-label={`Dismiss: ${what}`}>
          Dismiss
        </Button>
        {actions}
      </div>
    </li>
  );
}
