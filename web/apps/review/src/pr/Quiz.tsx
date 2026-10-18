// SPDX-License-Identifier: AGPL-3.0-or-later
// The quiz (spec §3.1): three to five questions, one at a time, with feedback after each. It
// never blocks posting and can be skipped. Questions are agent output: shown as text.
import { Button } from "@jesinia/design";
import { useState } from "react";
import type { Question } from "./model";
import s from "./pr.module.css";

type Outcome = "right" | "wrong" | "skipped";

/**
 * `onFinish` hears how many of the answered questions were right once the last question is done
 * (skipped ones aren't answered); a quiz with nothing answered never calls it.
 */
export function Quiz({
  questions,
  onFinish,
}: {
  questions: Question[];
  onFinish?: (right: number, answered: number) => void;
}) {
  const [at, setAt] = useState(0);
  const [picked, setPicked] = useState<number | null>(null);
  const [checked, setChecked] = useState(false);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const question = questions[at];

  const advance = (outcome: Outcome) => {
    const done = [...outcomes, outcome];
    setOutcomes(done);
    setAt((n) => n + 1);
    const answered = done.filter((o) => o !== "skipped").length;
    if (done.length === questions.length && answered > 0) {
      onFinish?.(done.filter((o) => o === "right").length, answered);
    }
    setPicked(null);
    setChecked(false);
  };
  const restart = () => {
    setAt(0);
    setPicked(null);
    setChecked(false);
    setOutcomes([]);
  };

  if (!question) {
    const right = outcomes.filter((o) => o === "right").length;
    const skipped = outcomes.filter((o) => o === "skipped").length;
    return (
      <article className={s.page} aria-label="Quiz">
        <h2>Quiz</h2>
        <p>{`${right} of ${questions.length} right · ${skipped} skipped. The quiz never blocks posting.`}</p>
        <div className={s.row}>
          <Button size="sm" variant="secondary" onClick={restart}>
            Start again
          </Button>
        </div>
      </article>
    );
  }

  const isRight = picked === question.answer;
  const last = at === questions.length - 1;
  return (
    <article className={s.page} aria-label="Quiz">
      <h2>Quiz</h2>
      <p className={s.muted}>{`Question ${at + 1} of ${questions.length}`}</p>
      <fieldset className={s.quiz}>
        <legend>{question.question}</legend>
        {question.choices.map((choice, i) => (
          <label key={i} className={s.option}>
            <input
              type="radio"
              name={`quiz-${at}`}
              checked={picked === i}
              disabled={checked}
              onChange={() => setPicked(i)}
            />
            <span>{choice}</span>
          </label>
        ))}
      </fieldset>
      {checked ? (
        <p className={s.feedback} data-right={isRight || undefined} role="status">
          {isRight
            ? `Right. ${question.explanation}`
            : `Not quite: the answer is “${question.choices[question.answer] ?? "?"}”. ${question.explanation}`}
        </p>
      ) : null}
      <div className={s.row}>
        {checked ? (
          <Button size="sm" variant="primary" onClick={() => advance(isRight ? "right" : "wrong")}>
            {last ? "Finish" : "Next question"}
          </Button>
        ) : (
          <>
            <Button size="sm" variant="primary" disabled={picked === null} onClick={() => setChecked(true)}>
              Check
            </Button>
            <Button size="sm" variant="ghost" onClick={() => advance("skipped")}>
              Skip
            </Button>
          </>
        )}
      </div>
    </article>
  );
}
