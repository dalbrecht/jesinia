// SPDX-License-Identifier: AGPL-3.0-or-later
import { Button, Panel, useToast } from "@jesinia/design";
import { APPS, RUNNER_NAMES, useClient, type ServerStatus, type SyncStatus } from "@jesinia/shell";
import { clock, greeting, MODE_LABEL } from "./format";
import s from "./launcher.module.css";

export function Launcher({ status }: { status: ServerStatus }) {
  const client = useClient();
  const toast = useToast();
  const kb = status.health?.modules.kb;
  const kbOk = kb?.ok === true;

  const syncNow = async () => {
    const r = await client.POST("/api/kbsync/run");
    const body = r.data as { result: { outcome: string; message: string }; status: SyncStatus } | undefined;
    if (body) {
      status.setSync(body.status);
      toast.show({
        tone: body.result.outcome === "error" ? "danger" : "success",
        message: `Sync: ${body.result.message}`,
      });
    }
  };

  return (
    <div className={s.page}>
      <section className={s.greeting}>
        <h1>{greeting()}</h1>
        <p>{kbOk ? `Your knowledge base: ${kb.detail}.` : "Set up your knowledge base folder to get started."}</p>
      </section>

      <nav aria-label="Apps" className={s.tiles}>
        {APPS.map((app) => {
          const body = (
            <>
              <span className={s.tileIcon} data-app={app.id} aria-hidden="true" />
              <span className={s.tileName}>{app.name}</span>
              <span className={s.tileDesc}>{app.description}</span>
              {app.built ? null : <span className={s.muted}>Coming soon</span>}
            </>
          );
          return app.built && kbOk ? (
            <a key={app.id} className={s.tile} href={app.href}>
              {body}
            </a>
          ) : (
            <div key={app.id} className={`${s.tile} ${s.soon}`} aria-disabled="true">
              {body}
            </div>
          );
        })}
      </nav>

      <div className={s.status} aria-label="System status summary" role="group">
        <Panel title="Knowledge base" headingLevel={2}>
          <div className={s.fact}>
            {status.loading ? (
              <div className={s.skeleton} />
            ) : kbOk ? (
              <span>{kb.detail}</span>
            ) : (
              <>
                <span>{kb?.detail ?? "Unavailable"}</span>
                <Button size="sm" href="/settings/#kb">
                  Open Settings
                </Button>
              </>
            )}
          </div>
        </Panel>
        <Panel title="Sync" headingLevel={2}>
          <div className={s.fact}>
            {status.sync ? (
              <>
                <span>{MODE_LABEL[status.sync.state.mode]}</span>
                <span className={s.muted}>
                  Last {clock(status.sync.last_run)} · next {clock(status.sync.next_run)}
                </span>
                {status.sync.last_result ? (
                  <span className={s.muted} data-testid="last-result">
                    {status.sync.last_result.message}
                  </span>
                ) : null}
                {status.sync.state.mode !== "off" ? (
                  <div className={s.line}>
                    <Button size="sm" onClick={() => void syncNow()}>
                      Sync now
                    </Button>
                  </div>
                ) : null}
              </>
            ) : (
              <div className={s.skeleton} />
            )}
          </div>
        </Panel>
        <Panel title="Agents" headingLevel={2}>
          <div className={s.fact}>
            {status.runners ? (
              Object.entries(status.runners).map(([id, r]) => (
                <span key={id} className={s.line}>
                  <strong>{RUNNER_NAMES[id] ?? id}</strong>
                  {r.available ? (
                    <span className={s.muted}>{r.version ?? "available"}</span>
                  ) : (
                    <span className={s.muted}>{r.detail}</span>
                  )}
                  {r.available ? null : (
                    <Button size="sm" variant="ghost" href="/settings/#agents">
                      How to fix
                    </Button>
                  )}
                </span>
              ))
            ) : (
              <div className={s.skeleton} />
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}
