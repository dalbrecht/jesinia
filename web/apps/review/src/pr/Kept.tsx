// SPDX-License-Identifier: AGPL-3.0-or-later
// The kept review page and How it works (spec §6.2, §8), under a finished deep review: where its
// KB page went (or why it couldn't be written, with Retry), and the drafted How it works for the
// author to accept. Nothing is written to How it works until they do, and a page someone edited
// since the draft is never overwritten (the server refuses it). The page path carries the PR's
// title and the draft carries the page as it is in the KB, which anyone may have edited: both are
// shown with their hidden characters made visible (R62).
import { Button } from "@jesinia/design";
import { useState } from "react";
import type { KeptPage, SystemDraft } from "./model";
import s from "./pr.module.css";
import { Visible } from "./Visible";

type Props = {
  kept: KeptPage | null;
  draft: SystemDraft | null;
  /** Keep the page (again); resolves once the round is read again. */
  onKeep: () => Promise<void>;
  /** Accept the draft; rejects with the reason it was refused. */
  onAccept: (draft: SystemDraft) => Promise<void>;
};

export function KeptReview({ kept, draft, onKeep, onAccept }: Props) {
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [later, setLater] = useState(false);
  const run = (work: () => Promise<void>) => {
    setBusy(true);
    setRefused(null);
    work().then(
      () => setBusy(false),
      (e: unknown) => {
        setBusy(false);
        setRefused(e instanceof Error ? e.message : String(e));
      },
    );
  };
  const notes = draft?.kind === "seed" ? "Start How it works" : "Update How it works";
  return (
    <section className={s.group} aria-label="Kept in your KB">
      <h3>Kept in your KB</h3>
      {kept?.state === "kept" && kept.page_id && kept.path ? (
        <p>
          {"This review is kept as "}
          <a href={`/notes/page/${encodeURIComponent(kept.page_id)}`}>
            <Visible text={kept.path} />
          </a>
          .
        </p>
      ) : (
        <div className={s.row}>
          <p className={s.muted}>
            {kept?.state === "failed"
              ? `Couldn't keep the review page (${kept.error ?? "no cause given"}).`
              : "This review isn't kept in your KB yet."}
          </p>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => run(onKeep)}>
            {kept?.state === "failed" ? "Retry" : "Keep it"}
          </Button>
        </div>
      )}
      {draft?.state === "failed" ? (
        <p className={s.muted}>{`Couldn't draft How it works (${draft.error ?? "no cause given"}).`}</p>
      ) : null}
      {draft?.state === "accepted" ? <p>How it works is updated with this review.</p> : null}
      {draft?.state === "proposed" && !later ? (
        <section className={s.group} aria-label={notes}>
          <h3>{notes}</h3>
          <p className={s.muted}>
            {draft.kind === "seed"
              ? "Your notes on how this repo works, seeded from this review. Nothing is written until you accept."
              : "What this approved PR changed, added to your notes on how this repo works. Nothing is written until you accept."}
          </p>
          <pre className={s.exact} aria-label="Drafted How it works">
            <Visible text={draft.text} />
          </pre>
          <div className={s.row}>
            <Button size="sm" disabled={busy} onClick={() => run(() => onAccept(draft))}>
              Accept
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setLater(true)}>
              Not now
            </Button>
          </div>
        </section>
      ) : null}
      {refused ? (
        <p className={s.muted} role="alert">
          {refused}
        </p>
      ) : null}
    </section>
  );
}
