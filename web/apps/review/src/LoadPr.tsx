// SPDX-License-Identifier: AGPL-3.0-or-later
// The Load PR field at the top of the queue (spec §2, "Load a PR"): a URL, owner/repo#123 or a
// review link. The server parses it; a failure is shown under the field with its cause.
import { Button, Input } from "@jesinia/design";
import { useState, type FormEvent } from "react";
import { errorCause } from "./api";
import s from "./review.module.css";

export type LoadPrProps = {
  /** Loads `ref`; resolves once the PR's tab is open, rejects with the cause. */
  onLoad: (ref: string) => Promise<void>;
};

export function LoadPr({ onLoad }: LoadPrProps) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ref = value.trim();
    if (!ref || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onLoad(ref);
      setValue("");
    } catch (err) {
      setError(errorCause(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={s.loadPr} onSubmit={(e) => void submit(e)}>
      <div className={s.loadField}>
        <Input
          label="Load a PR"
          hideLabel
          placeholder="Load a PR: paste a URL or owner/repo#123"
          value={value}
          error={error}
          onChange={(e) => {
            setValue(e.target.value);
            setError(null);
          }}
        />
      </div>
      <Button type="submit" variant="secondary" size="sm" loading={busy}>
        Load
      </Button>
    </form>
  );
}
