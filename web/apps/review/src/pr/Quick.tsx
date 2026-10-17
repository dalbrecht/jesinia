// SPDX-License-Identifier: AGPL-3.0-or-later
// A quick review's explanation (spec §3.1): two to four sentences on the change, before its
// one guided walk. Agent output: safe Markdown only, images off.
import { SafeMarkdown } from "@jesinia/shell";
import type { Overview } from "./model";
import s from "./pr.module.css";

export function Quick({ overview }: { overview: Overview }) {
  return (
    <article className={s.page} aria-label="Explanation">
      <p className={s.context}>
        Quick review: the explanation, one walk through the diff, findings and the recommendation.
      </p>
      <h2>Explanation</h2>
      <div className={s.prose}>
        <SafeMarkdown markdown={overview.summary} />
      </div>
    </article>
  );
}
