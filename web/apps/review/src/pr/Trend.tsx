// SPDX-License-Identifier: AGPL-3.0-or-later
// The metrics trend (spec §5.2, anti-goal: deterioration): the repo's scored reviews over time,
// one point per round at its mean score from 0 to 10, so a drift in quality shows. Plain SVG in
// the design tokens (no chart library): the chart is an image with a spoken summary, and the
// numbers are in a table beside it. Criteria are agent text: shown as text, hidden characters
// as visible tokens.
import { VisibleText, useClient } from "@jesinia/shell";
import { useEffect, useState } from "react";
import { errorCause, getMetricsHistory, type TrendPoint } from "../api";
import s from "./pr.module.css";

const WIDTH = 560;
const HEIGHT = 160;
const PAD = { top: 12, right: 16, bottom: 20, left: 28 };
const MAX = 10;
const GRID = [0, 5, 10];

const day = (iso: string) => new Date(iso).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
const num = (n: number) => String(Math.round(n * 100) / 100);

function summary(repo: string, points: TrendPoint[]): string {
  const first = points[0] as TrendPoint;
  const last = points[points.length - 1] as TrendPoint;
  const means = points.map((p) => p.mean);
  return (
    `Mean score per review of ${repo}, from 0 to 10: ${points.length} reviews, from ${num(first.mean)} ` +
    `(#${first.number}) to ${num(last.mean)} (#${last.number}); lowest ${num(Math.min(...means))}, ` +
    `highest ${num(Math.max(...means))}.`
  );
}

/** The chart and its table for `points` (oldest first); empty and single-point series say so. */
export function TrendChart({ repo, points }: { repo: string; points: TrendPoint[] }) {
  return (
    <figure className={s.trend} aria-label={`Quality trend for ${repo}`}>
      <figcaption className={s.trendTitle}>Quality trend</figcaption>
      {points.length === 0 ? (
        <p className={s.muted}>{`No scored reviews of ${repo} yet.`}</p>
      ) : (
        <>
          {points.length === 1 ? (
            <p className={s.muted}>{`One scored review so far: ${num((points[0] as TrendPoint).mean)} of 10.`}</p>
          ) : null}
          <Chart repo={repo} points={points} />
          <table className={s.table}>
            <thead>
              <tr>
                <th scope="col">Review</th>
                <th scope="col">When</th>
                <th scope="col">Mean</th>
                <th scope="col">Scores</th>
              </tr>
            </thead>
            <tbody>
              {[...points].reverse().map((p) => (
                <tr key={p.round_id}>
                  <td className={s.tabular}>{`#${p.number}`}</td>
                  <td>{day(p.at)}</td>
                  <td className={s.tabular}>{num(p.mean)}</td>
                  <td>
                    <ul className={s.trendScores}>
                      {p.scores.map((score, i) => (
                        <li key={i}>
                          <VisibleText text={score.criterion} />
                          {`: ${num(score.score)}`}
                        </li>
                      ))}
                    </ul>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </figure>
  );
}

function Chart({ repo, points }: { repo: string; points: TrendPoint[] }) {
  const innerWidth = WIDTH - PAD.left - PAD.right;
  const innerHeight = HEIGHT - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (points.length === 1 ? innerWidth / 2 : (i * innerWidth) / (points.length - 1));
  const y = (score: number) => PAD.top + innerHeight - (Math.max(0, Math.min(MAX, score)) / MAX) * innerHeight;
  const label =
    points.length === 1
      ? `Mean score of the one review of ${repo}: ${num((points[0] as TrendPoint).mean)} of 10.`
      : summary(repo, points);
  return (
    <svg className={s.trendChart} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={label}>
      {GRID.map((g) => (
        <g key={g} aria-hidden="true">
          <line className={s.trendGrid} x1={PAD.left} x2={WIDTH - PAD.right} y1={y(g)} y2={y(g)} />
          <text className={s.trendAxis} x={PAD.left - 6} y={y(g)} textAnchor="end" dominantBaseline="middle">
            {g}
          </text>
        </g>
      ))}
      {points.length > 1 ? (
        <polyline className={s.trendLine} points={points.map((p, i) => `${x(i)},${y(p.mean)}`).join(" ")} />
      ) : null}
      {points.map((p, i) => (
        <circle key={p.round_id} className={s.trendPoint} cx={x(i)} cy={y(p.mean)} r={3.5}>
          <title>{`#${p.number} · ${num(p.mean)} of 10 · ${day(p.at)}`}</title>
        </circle>
      ))}
    </svg>
  );
}

/** The trend of `repo`, read from the server's cache; a failure says why, nothing else breaks. */
export function Trend({ repo }: { repo: string }) {
  const client = useClient();
  const [points, setPoints] = useState<TrendPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    setPoints(null);
    setError(null);
    getMetricsHistory(client, repo).then(
      (history) => {
        if (current) setPoints(history.points);
      },
      (e: unknown) => {
        if (current) setError(errorCause(e));
      },
    );
    return () => {
      current = false;
    };
  }, [client, repo]);
  if (points !== null) return <TrendChart repo={repo} points={points} />;
  return (
    <figure className={s.trend} aria-label={`Quality trend for ${repo}`}>
      <figcaption className={s.trendTitle}>Quality trend</figcaption>
      <p className={s.muted}>{error === null ? "Loading the trend…" : `Couldn't load the trend: ${error}`}</p>
    </figure>
  );
}
