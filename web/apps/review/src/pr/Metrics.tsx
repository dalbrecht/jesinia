// SPDX-License-Identifier: AGPL-3.0-or-later
// Metrics (spec §5.2): scores from 0 to 10 against the applied standards, each naming its rule.
import { SafeMarkdown } from "@jesinia/shell";
import type { Score } from "./model";
import s from "./pr.module.css";

export function Metrics({ scores }: { scores: Score[] }) {
  return (
    <article className={s.page} aria-label="Metrics">
      <h2>Metrics</h2>
      <table className={s.table}>
        <thead>
          <tr>
            <th scope="col">Criterion</th>
            <th scope="col">Score</th>
            <th scope="col">Why</th>
            <th scope="col">Rule</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((m, i) => (
            <tr key={i}>
              <td>{m.criterion}</td>
              <td>
                <span className={s.score}>
                  <span
                    className={s.bar}
                    role="meter"
                    aria-valuemin={0}
                    aria-valuemax={10}
                    aria-valuenow={m.score}
                    aria-label={`${m.criterion}: ${m.score} of 10`}
                  >
                    <span style={{ width: `${Math.max(0, Math.min(10, m.score)) * 10}%` }} />
                  </span>
                  <span className={s.tabular}>{m.score}</span>
                </span>
              </td>
              <td className={s.prose}>
                <SafeMarkdown markdown={m.note} />
              </td>
              <td className={s.muted}>{`${m.rule.id} · ${m.rule.layer}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}
