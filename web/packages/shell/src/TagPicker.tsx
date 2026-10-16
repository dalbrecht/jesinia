// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, type components } from "@jesinia/client";
import { Badge, Button, Input, toneFor } from "@jesinia/design";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useClient } from "./context";
import s from "./TagPicker.module.css";

type Suggestion = components["schemas"]["SuggestionOut"];

export type TagPickerProps = {
  /** The text to suggest tags for (a note, a title, an excerpt). */
  text: string;
  /**
   * What the tags are for (a page id, a post id). When it changes, answers still in flight
   * for the old one are dropped, so a slow check never tags the wrong thing.
   */
  context?: string;
  /** The chosen tags, normalised and without `#`. */
  value: string[];
  onChange: (tags: string[]) => void;
};

/** A near-match the user can take instead of the tag they typed. */
type Offer = { typed: string; tag: string };

const SUGGESTIONS = 5;

/** The reason a request failed to even get a response (offline, refused, aborted). */
function causeText(cause: unknown): string {
  return cause instanceof Error && cause.message ? cause.message : "the request failed.";
}

/**
 * Tag chips for something about to be saved, plus a box to add a tag by name. Suggestions
 * (which may run the user's agent) are fetched only when the user asks, about to save, never
 * on every change of `text`. Tags outside the catalog carry a *new* badge (saving adds them);
 * a typed tag close to an existing one offers the existing one instead, so near-duplicates are
 * never created by accident.
 */
export function TagPicker({ text, context, value, onChange }: TagPickerProps) {
  const client = useClient();
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [checking, setChecking] = useState(false);
  // Bumped when the context changes and on unmount: a check answered under an older scope
  // belongs to something else and is dropped. Suggestions also go stale when the text changes.
  const scope = useRef(0);
  const suggestionsFor = useRef(0);
  const latest = useRef(value);

  useEffect(() => {
    latest.current = value;
  }, [value]);

  useEffect(() => {
    setOffer(null);
    setChecking(false);
    return () => {
      scope.current += 1;
    };
  }, [context]);

  useEffect(() => {
    setSuggestions([]);
    setSuggesting(false);
    return () => {
      suggestionsFor.current += 1;
    };
  }, [text]);

  const suggest = async () => {
    const wanted = text.trim();
    if (!wanted) return;
    const asked = [scope.current, suggestionsFor.current];
    setSuggesting(true);
    setError(null);
    const isCurrent = () => scope.current === asked[0] && suggestionsFor.current === asked[1];
    try {
      const r = await client.POST("/api/kb/tags/suggest", { body: { text: wanted, limit: SUGGESTIONS } });
      if (!isCurrent()) return;
      // Suggestions are a convenience: when they fail, the box still adds tags by name.
      setSuggestions(r.data ?? []);
    } catch (cause) {
      if (isCurrent()) setError(`Couldn't get suggestions: ${causeText(cause)}`);
    } finally {
      if (isCurrent()) setSuggesting(false);
    }
  };

  const add = (tag: string, isNew: boolean) => {
    const current = latest.current;
    if (!current.includes(tag)) onChange([...current, tag]);
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
    const asked = scope.current;
    try {
      const r = await client.GET("/api/kb/tags/check", { params: { query: { tag: typed } } });
      if (scope.current !== asked) return; // switched away (or unmounted) meanwhile
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
    } catch (cause) {
      if (scope.current === asked) setError(`Couldn't check that tag: ${causeText(cause)}`);
    } finally {
      if (scope.current === asked) setChecking(false);
    }
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
        <Button size="sm" variant="ghost" disabled={!text.trim()} loading={suggesting} onClick={() => void suggest()}>
          Suggest tags
        </Button>
      </div>
    </div>
  );
}
