// SPDX-License-Identifier: AGPL-3.0-or-later
import { errorText, type components } from "@jesinia/client";
import { Badge, Button, Input, List, Panel, Row, useToast } from "@jesinia/design";
import { useClient } from "@jesinia/shell";
import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
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

/**
 * Previews and applies a rename (one source) or a merge (sources into a target).
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
  const { mode, source } = target;
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<{ pages: PageRef[]; reserved: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const title = mode === "rename" ? `Rename #${source}` : `Merge #${source} into…`;
  const destination = name.trim().replace(/^#/, "");

  const runPreview = async () => {
    setBusy(true);
    setError(null);
    const r = await client.POST("/api/kb/tags/rename/preview", { body: { old: source, new: destination } });
    setBusy(false);
    if (!r.data) {
      setError(errorText(r.error, "Couldn't preview that change."));
      return;
    }
    setPreview({ pages: r.data.pages, reserved: r.data.skipped_reserved });
  };

  const apply = async () => {
    if (!preview) return;
    const revs = Object.fromEntries(preview.pages.map((p) => [p.id, p.rev]));
    setBusy(true);
    setError(null);
    const r =
      mode === "rename"
        ? await client.POST("/api/kb/tags/rename", { body: { old: source, new: destination, revs } })
        : await client.POST("/api/kb/tags/merge", { body: { sources: [source], into: destination, revs } });
    setBusy(false);
    if (!r.data) {
      setError(errorText(r.error, "Couldn't apply that change."));
      return;
    }
    const verb = mode === "rename" ? `Renamed #${source} to #${destination}` : `Merged #${source} into #${destination}`;
    const skipped = r.data.skipped_changed.length;
    const tail = skipped > 0 ? ` ${skipped} changed since the preview and were skipped.` : "";
    toast.show({ tone: "success", message: `${verb} in ${pagesLabel(r.data.updated.length)}.${tail}` });
    onApplied();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") onClose();
  };

  return (
    <div className={s.scrim} onKeyDown={onKeyDown}>
      <div role="dialog" aria-modal="true" aria-label={title} className={s.dialog}>
        <Panel
          variant="raised"
          title={title}
          headingLevel={3}
          footer={
            <div className={s.actions}>
              <Button onClick={onClose}>Cancel</Button>
              <Button loading={busy} disabled={destination === ""} onClick={() => void runPreview()}>
                Preview
              </Button>
              {preview ? (
                <Button variant="primary" loading={busy} onClick={() => void apply()}>
                  Apply
                </Button>
              ) : null}
            </div>
          }
        >
          <div className={s.form}>
            <Input
              label={mode === "rename" ? "New name" : "Merge into"}
              value={name}
              autoFocus
              onChange={(e) => {
                setName(e.target.value);
                setPreview(null);
              }}
              help={mode === "merge" ? "Pages tagged #" + source + " are retagged with this tag." : undefined}
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
      </div>
    </div>
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
