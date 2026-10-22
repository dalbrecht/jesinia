// SPDX-License-Identifier: AGPL-3.0-or-later
// How long one feed keeps its posts: the global default, a number of days, or the newest N posts
// (unread posts are never dropped by a cap). Custom… takes any number; the server checks it.
import { Button, Input } from "@jesinia/design";
import { useState, type FormEvent } from "react";
import type { Retention } from "./api";
import s from "./rss.module.css";

const PRESETS: Retention[] = [
  { unit: "days", value: 7 },
  { unit: "days", value: 30 },
  { unit: "days", value: 90 },
  { unit: "days", value: 365 },
  { unit: "posts", value: 50 },
  { unit: "posts", value: 200 },
  { unit: "posts", value: 1000 },
];
const DEFAULT = "default";
const CUSTOM = "custom";

const keyOf = (r: Retention) => `${r.unit}:${r.value}`;
export const describeRetention = (r: Retention) => (r.unit === "days" ? `${r.value} days` : `Newest ${r.value} posts`);

export type RetentionPickerProps = {
  value: Retention | null;
  onChange(r: Retention | null): void;
  /** The select's accessible name, e.g. "Keep posts for Old Blog". */
  label?: string;
  /** Names the custom fields ("Custom amount for …"). */
  subject?: string;
};

export function RetentionPicker({
  value,
  onChange,
  label = "Keep posts",
  subject = "this feed",
}: RetentionPickerProps) {
  const [custom, setCustom] = useState(false);
  const [amount, setAmount] = useState("");
  const [unit, setUnit] = useState<Retention["unit"]>("days");
  // A value set elsewhere (or through Custom…) is offered too, so the select can show it.
  const options = value && !PRESETS.some((p) => keyOf(p) === keyOf(value)) ? [...PRESETS, value] : PRESETS;

  const choose = (key: string) => {
    if (key === CUSTOM) {
      setAmount("");
      setCustom(true);
      return;
    }
    onChange(key === DEFAULT ? null : (options.find((o) => keyOf(o) === key) as Retention));
  };
  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!amount.trim()) return;
    setCustom(false);
    onChange({ unit, value: Math.round(Number(amount)) });
  };

  return (
    <div className={s.retention}>
      <select
        className={s.select}
        aria-label={label}
        value={custom ? CUSTOM : value ? keyOf(value) : DEFAULT}
        onChange={(e) => choose(e.target.value)}
      >
        <option value={DEFAULT}>Default</option>
        {options.map((o) => (
          <option key={keyOf(o)} value={keyOf(o)}>
            {describeRetention(o)}
          </option>
        ))}
        <option value={CUSTOM}>Custom…</option>
      </select>
      {custom ? (
        <form
          className={s.inlineForm}
          onSubmit={save}
          onKeyDown={(e) => {
            if (e.key !== "Escape") return;
            e.preventDefault();
            setCustom(false);
          }}
        >
          <Input
            label={`Custom amount for ${subject}`}
            hideLabel
            autoFocus
            type="number"
            inputMode="numeric"
            min={1}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
          <select
            className={s.select}
            aria-label={`Unit for ${subject}`}
            value={unit}
            onChange={(e) => setUnit(e.target.value as Retention["unit"])}
          >
            <option value="days">days</option>
            <option value="posts">posts</option>
          </select>
          <Button type="submit" size="sm" variant="primary" disabled={!amount.trim()}>
            Set
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setCustom(false)}>
            Cancel
          </Button>
        </form>
      ) : null}
    </div>
  );
}
