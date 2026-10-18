// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings → Agents: whether each runner is available, and the model it uses. A model comes from
// the setting saved here, else the runner's environment variable, else the CLI's own default;
// a change applies from the runner's next turn. Each Save sends only its own runner, with a
// fresh Idempotency-Key, so another runner's model is never put back.
import { errorText, withIdempotency, type components } from "@jesinia/client";
import { Badge, Button, Input, Panel, useToast } from "@jesinia/design";
import { RUNNER_NAMES, useClient, type ServerStatus } from "@jesinia/shell";
import { useEffect, useState, type FormEvent } from "react";
import s from "../settings.module.css";
import { unreachable } from "./unreachable";

type AgentSettings = components["schemas"]["AgentSettingsOut"];
type RunnerModel = components["schemas"]["RunnerModelOut"];
type Patch = components["schemas"]["AgentSettingsPatch"];

/** Display names for the server's runner ids (the shell's map predates `claude-code`). */
const NAMES: Record<string, string> = { "claude-code": "Claude Code", codex: "Codex" };
const runnerName = (id: string) => NAMES[id] ?? RUNNER_NAMES[id] ?? id;

/** Where the effective model comes from, in words. */
function sourceText(name: string, m: RunnerModel): string {
  if (m.source === "setting") return "Set here.";
  if (m.source === "environment") return `From ${m.variable}.`;
  return `Default: ${name} picks its own model.`;
}

function ModelField({
  runner,
  model,
  onSaved,
}: {
  runner: string;
  model: RunnerModel;
  onSaved: (settings: AgentSettings) => void;
}) {
  const client = useClient();
  const toast = useToast();
  const name = runnerName(runner);
  const [value, setValue] = useState(model.setting ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => setValue(model.setting ?? ""), [model.setting]);

  const wanted = value.trim() || null;
  const changed = wanted !== model.setting;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    // Only this runner: the server keeps every runner left out; null clears this one.
    const body = { [runner]: wanted } as Patch;
    const r = await client
      .PATCH("/api/agents/settings", { params: { header: withIdempotency() }, body })
      .catch((err: unknown) => {
        setError(`Couldn't save: ${unreachable(err)}`);
        return null;
      });
    setSaving(false);
    if (r === null) return;
    if (!r.data) {
      setError(errorText(r.error, "Couldn't save that model."));
      return;
    }
    onSaved(r.data);
    toast.show({ tone: "success", message: `${name} model saved.` });
  };

  return (
    <form onSubmit={(e) => void save(e)}>
      <fieldset className={s.repo}>
        <legend className={s.fieldName}>{`${name} model`}</legend>
        <Input
          label="Model"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={model.environment ?? `${name} default`}
          maxLength={100}
          spellCheck={false}
          autoComplete="off"
          error={error ?? undefined}
          help={sourceText(name, model)}
        />
        <div className={s.actions}>
          <Button type="submit" variant="primary" loading={saving} disabled={!changed}>
            Save
          </Button>
        </div>
      </fieldset>
    </form>
  );
}

export function AgentsSection({ status }: { status: ServerStatus }) {
  const client = useClient();
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    client.GET("/api/agents/settings").then(
      (r) => {
        if (r.data) setSettings(r.data);
        else setLoadError(`Couldn't load the agent models: ${errorText(r.error, `HTTP ${r.response.status}`)}`);
      },
      (e: unknown) => setLoadError(`Couldn't load the agent models: ${unreachable(e)}`),
    );
  }, [client]);

  return (
    <Panel
      title="Agents"
      id="agents"
      actions={
        <Button size="sm" onClick={() => void status.refresh()}>
          Check again
        </Button>
      }
    >
      <div className={s.form}>
        {status.runners ? (
          <ul className={s.list}>
            {Object.entries(status.runners).map(([id, r]) => (
              <li key={id} className={s.item}>
                <strong>{runnerName(id)}</strong>
                {r.available ? (
                  <span>
                    <Badge tone="success" icon="✓">
                      Available
                    </Badge>{" "}
                    <span className={s.muted}>{r.version ?? ""}</span>
                  </span>
                ) : (
                  <span>
                    <Badge tone="warn" icon="!">
                      Unavailable
                    </Badge>{" "}
                    <span className={s.muted}>{r.detail}</span>
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className={s.muted}>Checking…</p>
        )}
        {settings ? (
          Object.entries(settings.models).map(([runner, model]) => (
            <ModelField key={runner} runner={runner} model={model} onSaved={setSettings} />
          ))
        ) : loadError ? (
          <p className={s.muted}>{loadError}</p>
        ) : null}
        <p className={s.muted}>A new model applies from each agent's next turn.</p>
      </div>
    </Panel>
  );
}
