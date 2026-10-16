// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, type components } from "@jesinia/client";
import { Badge, Button, Input, toneFor } from "@jesinia/design";
import { useEffect, useState, type KeyboardEvent } from "react";
import { useClient } from "./context";
import s from "./TagPicker.module.css";

type Suggestion = components["schemas"]["SuggestionOut"];

export type TagPickerProps = {
  /** The text to suggest tags for (a note, a title, an excerpt). */
  text: string;
  /** The chosen tags, normalised and without `#`. */
  value: string[];
  onChange: (tags: string[]) => void;
};

/** A near-match the user can take instead of the tag they typed. */
type Offer = { typed: string; tag: string };

const SUGGESTIONS = 5;

/**
 * Tag chips for something about to be saved: the catalog's suggestions for `text`, plus a box
 * to add a tag by name. Tags outside the catalog carry a *new* badge (saving adds them); a
 * typed tag close to an existing one offers the existing one instead, so near-duplicates are
 * never created by accident.
 */
export function TagPicker({ text, value, onChange }: TagPickerProps) {
  const client = useClient();
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    const wanted = text.trim();
    setSuggestions([]);
    if (!wanted) return;
    let live = true;
    const load = async () => {
      const r = await client.POST("/api/kb/tags/suggest", { body: { text: wanted, limit: SUGGESTIONS } });
      // Suggestions are a convenience: when they fail, the box still adds tags by name.
      if (live) setSuggestions(r.data ?? []);
    };
    void load();
    return () => {
      live = false;
    };
  }, [client, text]);

  const add = (tag: string, isNew: boolean) => {
    if (!value.includes(tag)) onChange([...value, tag]);
    setFresh((previous) => {
      const next = new Set(previous);
      if (isNew) next.add(tag);
      else next.delete(tag);
      return next;
    });
  };

  const choose = async (raw: string) => {
    const typed = raw.trim().replace(/^#/, "");
    if (!typed) return;
    setError(null);
    setOffer(null);
    setChecking(true);
    const r = await client.GET("/api/kb/tags/check", { params: { query: { tag: typed } } });
    setChecking(false);
    if (!r.data) {
      setError(errorText(r.error, "Couldn't check that tag."));
      return;
    }
    setDraft("");
    const { normalised, canonical, near, same_meaning: sameMeaning } = r.data;
    const closest = sameMeaning ?? near[0];
    if (canonical) add(canonical, false);
    else if (closest) setOffer({ typed: normalised, tag: closest });
    else add(normalised, true);
  };

  const pick = (suggestion: Suggestion) => {
    // A new suggestion is checked like a typed tag: it may be a near-duplicate.
    if (suggestion.new) void choose(suggestion.tag);
    else add(suggestion.tag, false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
    e.preventDefault();
    void choose(draft);
  };

  const offered = suggestions.filter((suggestion) => !value.includes(suggestion.tag));
  return (
    <div className={s.picker}>
      {value.length > 0 ? (
        <ul className={s.chips} aria-label="Tags">
          {value.map((tag) => (
            <li key={tag} className={s.chip}>
              <Badge tone={toneFor(tag)}>#{tag}</Badge>
              {fresh.has(tag) ? <Badge>new</Badge> : null}
              <Button
                size="sm"
                variant="ghost"
                icon="×"
                aria-label={`Remove #${tag}`}
                onClick={() => onChange(value.filter((other) => other !== tag))}
              />
            </li>
          ))}
        </ul>
      ) : null}
      {offered.length > 0 ? (
        <div className={s.suggestions} role="group" aria-label="Suggested tags">
          {offered.map((suggestion) => (
            <Button key={suggestion.tag} size="sm" onClick={() => pick(suggestion)}>
              #{suggestion.tag}
              {suggestion.new ? (
                <>
                  {" "}
                  <Badge>new</Badge>
                </>
              ) : null}
            </Button>
          ))}
        </div>
      ) : null}
      {offer ? (
        <div className={s.offer}>
          <p role="status">
            #{offer.typed} looks like an existing tag. Use #{offer.tag}?
          </p>
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              add(offer.tag, false);
              setOffer(null);
            }}
          >
            Use #{offer.tag}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setOffer(null)}>
            Cancel
          </Button>
        </div>
      ) : null}
      <div className={s.add}>
        <Input
          label="Add a tag"
          hideLabel
          placeholder="Add a tag"
          value={draft}
          loading={checking}
          error={error ?? undefined}
          onChange={(e) => {
            setDraft(e.target.value);
            setError(null);
          }}
          onKeyDown={onKeyDown}
        />
        <Button size="sm" disabled={!draft.trim()} onClick={() => void choose(draft)}>
          Add
        </Button>
      </div>
    </div>
  );
}
