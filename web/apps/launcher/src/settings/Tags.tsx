// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, type components } from "@jesinia/client";
import { Badge, Button, Input, List, Panel, Row, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useRef, useState, type FormEvent, type RefObject } from "react";
import { createPortal } from "react-dom";
import s from "../settings.module.css";

type TagInfo = components["schemas"]["TagInfoOut"];
type UncatalogedTag = components["schemas"]["UncatalogedOut"];
type PageRef = components["schemas"]["PageRefOut"];
type ChangeMode = "rename" | "merge";
type ChangeTarget = { mode: ChangeMode; source: string };
type Suggestion = { near: string[] };
type SortKey = "name" | "count";

const pagesLabel = (n: number) => `${n} page${n === 1 ? "" : "s"}`;

function matchesFilter(info: TagInfo, needle: string): boolean {
  const haystack = [info.tag, info.meaning, ...info.aliases].join(" ").toLowerCase();
  return haystack.includes(needle.toLowerCase());
}

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

/** Modal behaviour: focus in, Tab trapped, page behind inert, Escape closes, focus restored. */
function useModalBehaviour(
  dialogRef: RefObject<HTMLDivElement | null>,
  scrimRef: RefObject<HTMLDivElement | null>,
  onClose: () => void,
) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Captured during the first render: autoFocus moves focus before effects run.
  const [invoker] = useState(() => document.activeElement as HTMLElement | null);
  useEffect(() => {
    const inerted = Array.from(document.body.children).filter(
      (el) => el !== scrimRef.current && !el.hasAttribute("inert"),
    );
    inerted.forEach((el) => el.setAttribute("inert", ""));
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const nodes = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && (active === last || !dialogRef.current?.contains(active))) {
        e.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      inerted.forEach((el) => el.removeAttribute("inert"));
      invoker?.focus();
    };
  }, [dialogRef, scrimRef, invoker]);
}

/**
 * Previews and applies a rename (one source) or a merge (several sources into a target).
 * The server has no merge preview, so a merge previews each source as a rename and
 * combines the pages and revisions.
 */
function ChangeDialog({
  target,
  onClose,
  onApplied,
}: {
  target: ChangeTarget;
  onClose: () => void;
  onApplied: () => void;
}) {
  const client = useClient();
  const toast = useToast();
  const { mode } = target;
  const [sources, setSources] = useState<string[]>([target.source]);
  const [extra, setExtra] = useState("");
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<{ pages: PageRef[]; reserved: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const scrimRef = useRef<HTMLDivElement>(null);
  useModalBehaviour(dialogRef, scrimRef, onClose);
  const title = mode === "rename" ? `Rename #${target.source}` : `Merge #${target.source} into…`;
  const destination = name.trim().replace(/^#/, "");
  const sourceList = sources.map((x) => `#${x}`).join(", ");

  const addSource = () => {
    const tag = extra.trim().replace(/^#/, "");
    if (tag !== "" && !sources.includes(tag)) {
      setSources([...sources, tag]);
      setPreview(null);
    }
    setExtra("");
  };

  const runPreview = async () => {
    setBusy(true);
    setError(null);
    const pages = new Map<string, PageRef>();
    const reserved = new Set<string>();
    for (const source of sources) {
      const r = await client.POST("/api/kb/tags/rename/preview", { body: { old: source, new: destination } });
      if (!r.data) {
        setBusy(false);
        setError(errorText(r.error, "Couldn't preview that change."));
        return;
      }
      r.data.pages.forEach((p) => pages.has(p.id) || pages.set(p.id, p));
      r.data.skipped_reserved.forEach((path) => reserved.add(path));
    }
    setBusy(false);
    setPreview({ pages: [...pages.values()], reserved: [...reserved] });
  };

  const apply = async () => {
    if (!preview) return;
    const revs = Object.fromEntries(preview.pages.map((p) => [p.id, p.rev]));
    setBusy(true);
    setError(null);
    const r =
      mode === "rename"
        ? await client.POST("/api/kb/tags/rename", { body: { old: target.source, new: destination, revs } })
        : await client.POST("/api/kb/tags/merge", { body: { sources, into: destination, revs } });
    setBusy(false);
    if (!r.data) {
      setError(errorText(r.error, "Couldn't apply that change."));
      return;
    }
    const verb =
      mode === "rename" ? `Renamed #${target.source} to #${destination}` : `Merged ${sourceList} into #${destination}`;
    const skipped = r.data.skipped_changed.length;
    const tail = skipped > 0 ? ` ${skipped} changed since the preview and were skipped.` : "";
    toast.show({ tone: "success", message: `${verb} in ${pagesLabel(r.data.updated.length)}.${tail}` });
    onApplied();
  };

  return createPortal(
    <div className={s.scrim} ref={scrimRef}>
      <div role="dialog" aria-modal="true" aria-label={title} className={s.dialog} ref={dialogRef}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (destination !== "") void runPreview();
          }}
        >
          <Panel
            variant="raised"
            title={title}
            headingLevel={3}
            footer={
              <div className={s.actions}>
                <Button type="button" onClick={onClose}>
                  Cancel
                </Button>
                <Button type="submit" loading={busy} disabled={destination === ""}>
                  Preview
                </Button>
                {preview ? (
                  <Button type="button" variant="primary" loading={busy} onClick={() => void apply()}>
                    Apply
                  </Button>
                ) : null}
              </div>
            }
          >
            <div className={s.form}>
              {mode === "merge" ? (
                <>
                  <ul className={s.chips} aria-label="Tags to merge">
                    {sources.map((x) => (
                      <li key={x}>
                        <Badge>{`#${x}`}</Badge>
                        {sources.length > 1 ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            aria-label={`Remove #${x}`}
                            onClick={() => {
                              setSources(sources.filter((y) => y !== x));
                              setPreview(null);
                            }}
                          >
                            ×
                          </Button>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                  <div className={s.actions}>
                    <Input
                      label="Also merge"
                      value={extra}
                      onChange={(e) => setExtra(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          addSource();
                        }
                      }}
                    />
                    <Button type="button" onClick={addSource}>
                      Add
                    </Button>
                  </div>
                </>
              ) : null}
              <Input
                label={mode === "rename" ? "New name" : "Merge into"}
                value={name}
                autoFocus
                onChange={(e) => {
                  setName(e.target.value);
                  setPreview(null);
                }}
                help={mode === "merge" ? `Pages tagged ${sourceList} are retagged with this tag.` : undefined}
                error={error ?? undefined}
              />
              {preview ? (
                <>
                  <h4>Pages to update</h4>
                  {preview.pages.length === 0 ? (
                    <p className={s.muted}>No pages use this tag yet.</p>
                  ) : (
                    <ul className={s.plainList}>
                      {preview.pages.map((p) => (
                        <li key={p.id}>{p.title}</li>
                      ))}
                    </ul>
                  )}
                  <h4>Not updated (private folder)</h4>
                  {preview.reserved.length === 0 ? (
                    <p className={s.muted}>No pages are skipped.</p>
                  ) : (
                    <ul className={s.plainList}>
                      {preview.reserved.map((path) => (
                        <li key={path} className={s.mono}>
                          {path}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              ) : null}
            </div>
          </Panel>
        </form>
      </div>
    </div>,
    document.body,
  );
}

function AddTagForm({ onAdded }: { onAdded: () => void }) {
  const client = useClient();
  const toast = useToast();
  const [tag, setTag] = useState("");
  const [meaning, setMeaning] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (force: boolean) => {
    setBusy(true);
    setError(null);
    const r = await client.POST("/api/kb/tags", { body: { tag, meaning, force } });
    setBusy(false);
    if (r.response.ok) {
      toast.show({ tone: "success", message: `Added #${tag.trim().replace(/^#/, "")}.` });
      setTag("");
      setMeaning("");
      setSuggestion(null);
      onAdded();
      return;
    }
    const body = r.error as { detail?: string; near?: string[] | null } | undefined;
    if (r.response.status === 409 && body?.near && body.near.length > 0) {
      setSuggestion({ near: body.near });
      return;
    }
    setSuggestion(null);
    setError(errorText(r.error, "Couldn't add that tag."));
  };

  const acceptNear = (near: string) => {
    toast.show({ message: `Using #${near}.` });
    setTag("");
    setMeaning("");
    setSuggestion(null);
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit(false);
  };

  const nearest = suggestion?.near[0];
  return (
    <form className={s.form} onSubmit={onSubmit}>
      <Input
        label="New tag"
        value={tag}
        onChange={(e) => {
          setTag(e.target.value);
          setSuggestion(null);
        }}
        error={error ?? undefined}
        help="Lowercase words joined with hyphens, like machine-learning."
      />
      <Input label="Meaning" value={meaning} onChange={(e) => setMeaning(e.target.value)} />
      {suggestion && nearest ? (
        <div className={s.actions} role="status">
          <span>Use #{nearest}?</span>
          <Button variant="primary" onClick={() => acceptNear(nearest)}>
            {`Use #${nearest}`}
          </Button>
          <Button loading={busy} onClick={() => void submit(true)}>
            Add anyway
          </Button>
        </div>
      ) : null}
      <div className={s.actions}>
        <Button type="submit" variant="primary" loading={busy}>
          Add tag
        </Button>
      </div>
    </form>
  );
}

export function TagsSection() {
  const client = useClient();
  const toast = useToast();
  const [catalog, setCatalog] = useState<TagInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uncataloged, setUncataloged] = useState<UncatalogedTag[]>([]);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<SortKey>("name");
  const [dialog, setDialog] = useState<ChangeTarget | null>(null);

  const reload = useCallback(async () => {
    const [tags, loose] = await Promise.all([client.GET("/api/kb/tags"), client.GET("/api/kb/tags/uncataloged")]);
    if (tags.data) {
      setCatalog(tags.data);
      setLoadError(null);
    } else {
      setLoadError(errorText(tags.error, "Couldn't load the tag catalog."));
    }
    setUncataloged(loose.data ?? []);
  }, [client]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const act = async (path: "retire" | "adopt" | "ignore", tag: string, done: string) => {
    const r = await client.POST(`/api/kb/tags/${path}`, { body: { tag } });
    if (!r.response.ok) {
      toast.show({ tone: "danger", message: errorText((r as { error?: unknown }).error, "That didn't work.") });
      return;
    }
    toast.show({ tone: "success", message: done });
    await reload();
  };

  const visible = (catalog ?? [])
    .filter((info) => matchesFilter(info, filter))
    .sort((a, b) => (sort === "count" ? b.count - a.count : a.tag.localeCompare(b.tag)));
  const emptyText = filter ? "No tags match that filter." : "No tags yet. Add one above.";

  return (
    <Panel title="Tags" id="tags">
      <div className={s.form}>
        <p className={s.muted}>
          The catalog keeps tag names consistent across your pages. Renaming or merging edits each page's tags.
        </p>
        <AddTagForm onAdded={() => void reload()} />
        <div className={s.actions}>
          <Input label="Filter tags" variant="search" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <Button size="sm" aria-pressed={sort === "name"} onClick={() => setSort("name")}>
            Sort by name
          </Button>
          <Button size="sm" aria-pressed={sort === "count"} onClick={() => setSort("count")}>
            Sort by count
          </Button>
        </div>
        {loadError ? (
          <p role="alert">{loadError}</p>
        ) : (
          <List
            aria-label="Tag catalog"
            items={visible}
            getKey={(info) => info.tag}
            loading={catalog === null}
            empty={emptyText}
            renderRow={(info) => (
              <Row
                title={
                  <>
                    <span data-testid="tag-name">{`#${info.tag}`}</span>{" "}
                    {info.retired ? (
                      <Badge tone="warn" icon="!">
                        Retired
                      </Badge>
                    ) : null}
                  </>
                }
                excerpt={info.meaning || undefined}
                meta={info.aliases.length > 0 ? `Also written: ${info.aliases.join(", ")}` : undefined}
                trailing={
                  <span className={s.actions}>
                    <span className={s.muted}>{pagesLabel(info.count)}</span>
                    <Button size="sm" onClick={() => setDialog({ mode: "rename", source: info.tag })}>
                      Rename
                    </Button>
                    <Button size="sm" onClick={() => setDialog({ mode: "merge", source: info.tag })}>
                      Merge into…
                    </Button>
                    {info.retired ? null : (
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => void act("retire", info.tag, `Retired #${info.tag}.`)}
                      >
                        Retire
                      </Button>
                    )}
                  </span>
                }
              />
            )}
          />
        )}
        <Panel title="Uncataloged" headingLevel={3}>
          <List
            aria-label="Uncataloged tags"
            items={uncataloged}
            getKey={(info) => info.tag}
            empty="Every tag in your pages is in the catalog."
            renderRow={(info) => (
              <Row
                title={`#${info.tag}`}
                trailing={
                  <span className={s.actions}>
                    <span className={s.muted}>{pagesLabel(info.count)}</span>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => void act("adopt", info.tag, `Adopted #${info.tag}.`)}
                    >
                      Adopt
                    </Button>
                    <Button size="sm" onClick={() => setDialog({ mode: "merge", source: info.tag })}>
                      Merge into…
                    </Button>
                    <Button size="sm" onClick={() => void act("ignore", info.tag, `Ignored #${info.tag}.`)}>
                      Ignore
                    </Button>
                  </span>
                }
              />
            )}
          />
        </Panel>
      </div>
      {dialog ? (
        <ChangeDialog
          target={dialog}
          onClose={() => setDialog(null)}
          onApplied={() => {
            setDialog(null);
            void reload();
          }}
        />
      ) : null}
    </Panel>
  );
}
