// SPDX-License-Identifier: AGPL-3.0-or-later
// An opened review's tab, from the cache (spec §2, "Always cached"): the PR's row and its
// latest round, marked stale while the PR has moved past the head the review covers.
import { useClient } from "@jesinia/shell";
import { Badge, Button } from "@jesinia/design";
import { useEffect, useState } from "react";
import { errorCause, getPr, safeHttpUrl, type PrView } from "./api";
import { RowCard, refOf } from "./Queue";
import s from "./review.module.css";

export type OpenedPrProps = {
  repo: string;
  number: number;
  /** Changes whenever the queue was read again: the cached view is read again with it. */
  refresh: number;
};

export function OpenedPr({ repo, number, refresh }: OpenedPrProps) {
  const client = useClient();
  const [view, setView] = useState<PrView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let current = true;
    getPr(client, repo, number).then(
      (v) => {
        if (!current) return;
        setView(v);
        setError(null);
      },
      (e: unknown) => {
        if (current) setError(errorCause(e));
      },
    );
    return () => {
      current = false;
    };
  }, [client, repo, number, refresh, retry]);

  // A PR switched in place (another tab) starts from its own cache, not the last one's.
  const shown = view && view.pr.repo === repo && view.pr.number === number ? view : null;

  if (!shown) {
    return (
      <section className={s.opened} aria-label={`${repo}#${number}`}>
        {error ? (
          <div className={s.problem} role="alert">
            <span>{`Couldn't read this review: ${error}`}</span>
            <Button size="sm" variant="secondary" onClick={() => setRetry((n) => n + 1)}>
              Try again
            </Button>
          </div>
        ) : null}
      </section>
    );
  }

  const { pr } = shown;
  const github = safeHttpUrl(pr.url);
  return (
    <section className={s.opened} aria-label={refOf(pr)}>
      <header className={s.openedHead}>
        <p className={s.muted}>{refOf(pr)}</p>
        <h1 className={s.openedTitle}>{pr.title}</h1>
        <div className={s.openedTools}>
          {shown.stale ? (
            <Badge tone="warn" icon="!">
              Stale: this PR has newer commits than this review; it refreshes in the background.
            </Badge>
          ) : null}
          {github ? (
            <Button size="sm" variant="ghost" href={github} target="_blank" rel="noopener noreferrer">
              Open on GitHub
            </Button>
          ) : null}
        </div>
      </header>
      <RowCard row={pr} />
      {shown.round?.error ? <p className={s.reason}>{`The last review failed: ${shown.round.error}`}</p> : null}
    </section>
  );
}
