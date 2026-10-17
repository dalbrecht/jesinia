// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, type components } from "@jesinia/client";
import { Badge, Button, Input, toneFor } from "@jesinia/design";
import {
  forwardRef,
  useEffect,
  useLayoutEffect,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
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
  /**
   * Ask for suggestions once, as the picker opens: for a surface the user opened in order to
   * tag something (RSS Keep, spec §3.1). It runs in the background and never blocks typing.
   */
  suggestOnOpen?: boolean;
};

/** What a caller about to save can ask of the picker. */
export type TagPickerHandle = {
  /**
   * Add the tag still typed in the box, as Enter would. Resolves to the tags then chosen
   * (as they are when nothing is typed), or null when the author must decide first (a
   * near-match offer, an invalid tag, a failed check).
   */
  commitDraft: () => Promise<string[] | null>;
};

/**
 * Where focus goes once the control that had it unmounts (a picked suggestion, a removed
 * chip): the item that slides into the same position, else the box, or the offer if one shows.
 */
type FocusRequest = { from: "suggestions" | "chips"; index: number };

/** A near-match the user can take instead of the tag they typed. */
type Offer = { typed: string; tag: string };

const SUGGESTIONS = 5;

/** The reason a request failed to even get a response (offline, refused, aborted). */
function causeText(cause: unknown): string {
  return cause instanceof Error && cause.message ? cause.message : "the request failed.";
}

/**
 * Tag chips for something about to be saved, plus a box to add a tag by name. Suggestions
 * (which may run the user's agent) are fetched only when the user asks (Suggest tags, or opening
 * a surface with `suggestOnOpen`), never on every change of `text`. Tags outside the catalog carry a *new* badge (saving adds them);
 * a typed tag close to an existing one offers the existing one instead, so near-duplicates are
 * never created by accident.
 */
export const TagPicker = forwardRef<TagPickerHandle, TagPickerProps>(function TagPicker(
  { text, context, value, onChange, suggestOnOpen = false },
  ref,
) {
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
  const root = useRef<HTMLDivElement>(null);
  const focusRequest = useRef<FocusRequest | null>(null);

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

  // Runs after the render that removed the focused control, so the DOM is already settled.
  useLayoutEffect(() => {
    const request = focusRequest.current;
    if (!request || !root.current) return;
    focusRequest.current = null;
    const container = root.current;
    const offerButton = container.querySelector<HTMLElement>(`.${s.offer} button`);
    const group = request.from === "chips" ? `.${s.chips} button` : `.${s.suggestions} button`;
    const sibling = container.querySelectorAll<HTMLElement>(group)[request.index];
    (offerButton ?? sibling ?? container.querySelector<HTMLElement>("input"))?.focus();
  });

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
      // The box still adds tags by name when this fails, but the failure is shown, not hidden.
      if (r.data) setSuggestions(r.data);
      else setError(errorText(r.error, "Couldn't get suggestions."));
    } catch (cause) {
      if (isCurrent()) setError(`Couldn't get suggestions: ${causeText(cause)}`);
    } finally {
      if (isCurrent()) setSuggesting(false);
    }
  };

  const add = (tag: string, isNew: boolean): string[] => {
    const current = latest.current;
    const next = current.includes(tag) ? current : [...current, tag];
    if (next !== current) {
      latest.current = next; // a save right after this sees the tag before the parent re-renders
      onChange(next);
    }
    setFresh((previous) => {
      const next = new Set(previous);
      if (isNew) next.add(tag);
      else next.delete(tag);
      return next;
    });
    return next;
  };

  const choose = async (raw: string, refocus?: FocusRequest): Promise<string[] | null> => {
    const typed = raw.trim().replace(/^#/, "");
    if (!typed) return latest.current;
    setError(null);
    setOffer(null);
    setChecking(true);
    const asked = scope.current;
    try {
      const r = await client.GET("/api/kb/tags/check", { params: { query: { tag: typed } } });
      if (scope.current !== asked) return null; // switched away (or unmounted) meanwhile
      if (!r.data) {
        setError(errorText(r.error, "Couldn't check that tag."));
        return null;
      }
      if (refocus) focusRequest.current = refocus;
      setDraft("");
      const { normalised, canonical, near, same_meaning: sameMeaning } = r.data;
      const closest = sameMeaning ?? near[0];
      if (canonical) return add(canonical, false);
      if (closest) {
        setOffer({ typed: normalised, tag: closest });
        return null;
      }
      return add(normalised, true);
    } catch (cause) {
      if (scope.current === asked) setError(`Couldn't check that tag: ${causeText(cause)}`);
      return null;
    } finally {
      if (scope.current === asked) setChecking(false);
    }
  };

  // Once per mount (on open only, with the text it opened with), after the text effect above
  // has reset the suggestions. No guard ref: under StrictMode's remount the first answer is
  // dropped as stale, so the second ask is the one that shows.
  useEffect(() => {
    if (suggestOnOpen) void suggest();
  }, []);

  useImperativeHandle(ref, () => ({ commitDraft: () => choose(draft) }), [draft]);

  const pick = (suggestion: Suggestion, index: number) => {
    const refocus: FocusRequest = { from: "suggestions", index };
    // A new suggestion is checked like a typed tag: it may be a near-duplicate.
    if (suggestion.new) void choose(suggestion.tag, refocus);
    else {
      focusRequest.current = refocus;
      add(suggestion.tag, false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
    e.preventDefault();
    void choose(draft);
  };

  const offered = suggestions.filter((suggestion) => !value.includes(suggestion.tag));
  return (
    <div ref={root} className={s.picker}>
      {value.length > 0 ? (
        <ul className={s.chips} aria-label="Tags">
          {value.map((tag, index) => (
            <li key={tag} className={s.chip}>
              <Badge tone={toneFor(tag)}>#{tag}</Badge>
              {fresh.has(tag) ? <Badge>new</Badge> : null}
              <Button
                size="sm"
                variant="ghost"
                icon="×"
                aria-label={`Remove #${tag}`}
                onClick={() => {
                  focusRequest.current = { from: "chips", index };
                  onChange(value.filter((other) => other !== tag));
                }}
              />
            </li>
          ))}
        </ul>
      ) : null}
      {offered.length > 0 ? (
        <div className={s.suggestions} role="group" aria-label="Suggested tags">
          {offered.map((suggestion, index) => (
            <Button key={suggestion.tag} size="sm" onClick={() => pick(suggestion, index)}>
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
});
