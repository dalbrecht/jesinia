// SPDX-License-Identifier: AGPL-3.0-or-later
// Overview (spec §3.1): why the PR exists and what it is for, in business terms. Agent output:
// rendered only through the safe Markdown renderer, images off.
import { SafeMarkdown } from "@jesinia/shell";
import type { Overview as OverviewData } from "./model";
import s from "./pr.module.css";

export function Overview({ overview, context }: { overview: OverviewData; context?: string | undefined }) {
  return (
    <article className={s.page} aria-label="Overview">
      {context ? <p className={s.context}>{context}</p> : null}
      <h2>Overview</h2>
      <div className={s.prose}>
        <SafeMarkdown markdown={overview.summary} />
      </div>
      <div className={s.prose}>
        <SafeMarkdown markdown={overview.purpose} />
      </div>
      {overview.sources.length ? (
        <div>
          <h3>Drawn from</h3>
          <ul className={s.refs}>
            {overview.sources.map((src, i) => (
              <li key={i}>{src}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  );
}
