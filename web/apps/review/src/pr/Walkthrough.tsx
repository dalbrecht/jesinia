// SPDX-License-Identifier: AGPL-3.0-or-later
// The walkthrough (spec §3.1): each step's narrative (about 40%) beside its diff (about 60%),
// the step's hunks highlighted; and "Not walked through", with Show and Walk everything.
// The diff is PR content: every line is a monospace <pre> of text, never HTML.
import { Button } from "@jesinia/design";
import { SafeMarkdown } from "@jesinia/shell";
import { useEffect, useRef, useState } from "react";
import type { PrFile } from "../api";
import { FindingCard } from "./Findings";
import {
  coverageLine,
  fileOf,
  parsePatch,
  REASONS,
  type DiffLine,
  type Finding,
  type Hunk,
  type Step,
  type WalkCoverage,
} from "./model";
import s from "./pr.module.css";

export type DiffLayout = "unified" | "split";

const sign = (line: DiffLine) => (line.kind === " " ? " " : line.kind);

function UnifiedHunk({ hunk }: { hunk: Hunk }) {
  return (
    <>
      <pre className={s.hunkHead}>{hunk.header}</pre>
      {hunk.lines.map((line, i) => (
        <div key={i} className={s.line} data-kind={line.kind}>
          <span className={s.ln} aria-hidden="true">
            {line.old ?? ""}
          </span>
          <span className={s.ln} aria-hidden="true">
            {line.new ?? ""}
          </span>
          <pre>{`${sign(line)}${line.text}`}</pre>
        </div>
      ))}
    </>
  );
}

type Pair = { left: DiffLine | null; right: DiffLine | null };

/** Side by side: context on both sides; a run of removals beside the additions that follow it. */
export function pairLines(lines: DiffLine[]): Pair[] {
  const out: Pair[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as DiffLine;
    if (line.kind !== "-" && line.kind !== "+") {
      out.push({ left: line, right: line });
      i += 1;
      continue;
    }
    const removed: DiffLine[] = [];
    const added: DiffLine[] = [];
    while (lines[i]?.kind === "-") removed.push(lines[i++] as DiffLine);
    while (lines[i]?.kind === "+") added.push(lines[i++] as DiffLine);
    for (let k = 0; k < Math.max(removed.length, added.length); k += 1) {
      out.push({ left: removed[k] ?? null, right: added[k] ?? null });
    }
  }
  return out;
}

function Cell({ line }: { line: DiffLine | null }) {
  return <td data-kind={line?.kind}>{line ? <pre>{`${sign(line)}${line.text}`}</pre> : null}</td>;
}

type FileDiffProps = {
  file: PrFile;
  /** The hunks to show (all when omitted), and which of them the step walks through. */
  only?: number[] | undefined;
  current?: number[] | undefined;
  layout: DiffLayout;
};

export function FileDiff({ file, only, current = [], layout }: FileDiffProps) {
  const hunks = file.patch ? parsePatch(file.patch) : [];
  const shown = hunks.map((hunk, index) => ({ hunk, index })).filter(({ index }) => !only || only.includes(index));
  const head = (
    <div className={s.fileHead}>
      <span>{file.path}</span>
      <span>
        <span className={s.plus}>{`+${file.additions}`}</span> <span className={s.minus}>{`−${file.deletions}`}</span>
      </span>
    </div>
  );
  if (file.patch === null) {
    return (
      <div>
        {head}
        <p className={s.empty}>
          GitHub doesn't show this file's diff (binary or too large). Open the PR on GitHub to see it.
        </p>
      </div>
    );
  }
  const label = (index: number) => `${file.path} hunk ${index + 1}${current.includes(index) ? " (this step)" : ""}`;
  if (layout === "split") {
    return (
      <div>
        {head}
        <table className={s.split}>
          <thead>
            <tr>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          {shown.map(({ hunk, index }) => (
            <tbody
              key={index}
              role="group"
              aria-label={label(index)}
              className={s.hunk}
              data-current={current.includes(index) || undefined}
            >
              <tr>
                <td colSpan={2}>
                  <pre className={s.hunkHead}>{hunk.header}</pre>
                </td>
              </tr>
              {pairLines(hunk.lines).map((pair, i) => (
                <tr key={i}>
                  <Cell line={pair.left} />
                  <Cell line={pair.right} />
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    );
  }
  return (
    <div>
      {head}
      {shown.map(({ hunk, index }) => (
        <div
          key={index}
          role="group"
          aria-label={label(index)}
          className={s.hunk}
          data-current={current.includes(index) || undefined}
        >
          <UnifiedHunk hunk={hunk} />
        </div>
      ))}
    </div>
  );
}

const NO_DIFF =
  "The diff isn't cached yet: GitHub's file list for this head hasn't been read. It shows here once the review has read it.";

type StepPageProps = {
  step: Step;
  index: number;
  total: number;
  findings: Finding[];
  files: PrFile[] | null;
  layout: DiffLayout;
};

export function StepPage({ step, index, total, findings, files, layout }: StepPageProps) {
  const diff = useRef<HTMLElement>(null);
  const paths = [...new Set(step.hunks.map((h) => h.path))];
  // The step's first hunk comes into view within the diff (highlighted among its file's other
  // hunks). Only the diff scrolls: stacked on a narrow window, the narrative stays in view.
  useEffect(() => {
    const pane = diff.current;
    const hunk = pane?.querySelector<HTMLElement>("[data-current]");
    if (pane && hunk && pane.scrollHeight > pane.clientHeight) pane.scrollTop = Math.max(0, hunk.offsetTop - 40);
  }, [index]);
  return (
    <div className={s.step}>
      <article className={s.narrative} aria-label={step.title}>
        <p className={s.context}>{`Step ${index + 1} of ${total}`}</p>
        <h2>{step.title}</h2>
        <div className={s.prose}>
          <SafeMarkdown markdown={step.body} />
        </div>
        {findings.map((f, i) => (
          <FindingCard key={i} finding={f} showGroup />
        ))}
      </article>
      <section ref={diff} className={s.diff} role="region" aria-label="Diff for this step">
        {files === null ? <p className={s.empty}>{NO_DIFF}</p> : null}
        {files !== null
          ? paths.map((path) => {
              const file = fileOf(files, path);
              const current = step.hunks.filter((h) => h.path === path).map((h) => h.hunk);
              return file ? (
                <FileDiff key={path} file={file} current={current} layout={layout} />
              ) : (
                <p key={path} className={s.empty}>{`GitHub's file list has no ${path}.`}</p>
              );
            })
          : null}
      </section>
    </div>
  );
}

type NotWalkedProps = {
  coverage: WalkCoverage | null;
  nothingToWalk: boolean;
  files: PrFile[] | null;
  layout: DiffLayout;
  /** "Walk everything": asks for steps for the rest; busy while the request is out. */
  onWalkEverything: () => void;
  walking: boolean;
  walkNote: string | null;
};

export function NotWalked({
  coverage,
  nothingToWalk,
  files,
  layout,
  onWalkEverything,
  walking,
  walkNote,
}: NotWalkedProps) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  const items = coverage?.not_walked ?? [];
  const more = (coverage?.not_walked_total ?? 0) - items.length;
  return (
    <article className={s.page} aria-label="Not walked through">
      <h2>Not walked through</h2>
      <p>{coverageLine(coverage, nothingToWalk, true) ?? "Coverage isn't known yet."}</p>
      <p className={s.muted}>
        Leaving parts out is often right (lockfiles, generated code, renames, mechanical edits), but never silently:
        each is listed with its reason.
      </p>
      {items.length === 0 ? (
        <p className={s.muted}>Nothing was left out.</p>
      ) : (
        <ul className={s.skipped}>
          {items.map((skip, i) => {
            const file = fileOf(files, skip.path);
            const shown = open.has(i);
            return (
              <li key={i}>
                <div className={s.skipRow}>
                  <code>{skip.path}</code>
                  {skip.hunk !== null ? <span className={s.muted}>{`hunk ${skip.hunk + 1}`}</span> : null}
                  <span className={s.muted}>{REASONS[skip.reason] ?? skip.reason}</span>
                  <Button size="sm" variant="ghost" aria-expanded={shown} onClick={() => toggle(i)}>
                    {shown ? "Hide" : "Show"}
                  </Button>
                </div>
                {shown ? (
                  <div className={`${s.diff} ${s.skipDiff}`}>
                    {file ? (
                      <FileDiff file={file} only={skip.hunk === null ? undefined : [skip.hunk]} layout={layout} />
                    ) : (
                      <p className={s.empty}>{files === null ? NO_DIFF : `GitHub's file list has no ${skip.path}.`}</p>
                    )}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {more > 0 ? <p className={s.muted}>{`…and ${more} more not listed here.`}</p> : null}
      {items.length > 0 ? (
        <div className={s.row}>
          <Button size="sm" variant="secondary" onClick={onWalkEverything} loading={walking}>
            Walk everything
          </Button>
          <span className={s.muted}>
            Adds steps for the rest; Jesinia's own skips (lockfiles, vendored code) stay out.
          </span>
        </div>
      ) : null}
      {walkNote ? (
        <p className={s.muted} role="status">
          {walkNote}
        </p>
      ) : null}
    </article>
  );
}
