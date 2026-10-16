// SPDX-License-Identifier: AGPL-3.0-or-later
import { subscribe, UNAUTHORIZED_EVENT } from "@jesinia/client";
import { useCallback, useEffect, useState } from "react";
import { useClient } from "./context";
import type { Conflict, Health, Runners, SyncStatus } from "./types";

export type ServerStatus = {
  loading: boolean;
  signedOut: boolean;
  health: Health | null;
  sync: SyncStatus | null;
  conflicts: Conflict[];
  runners: Runners | null;
  refresh: () => Promise<void>;
  setSync: (s: SyncStatus) => void;
};

/** Health, sync, conflicts and runners, kept current through `/api/events`. */
export function useServerStatus(opts: { runners?: boolean } = {}): ServerStatus {
  const client = useClient();
  const [loading, setLoading] = useState(true);
  const [signedOut, setSignedOut] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [runners, setRunners] = useState<Runners | null>(null);
  const wantRunners = opts.runners ?? true;

  const refresh = useCallback(async () => {
    const [h, s, c, r] = await Promise.all([
      client.GET("/healthz"),
      client.GET("/api/kbsync/status"),
      client.GET("/api/kb/conflicts"),
      wantRunners ? client.GET("/api/agents/runners") : Promise.resolve(null),
    ]);
    if (h.data) setHealth(h.data as Health);
    if (s.data) setSync(s.data as unknown as SyncStatus);
    if (c.data) setConflicts(c.data as Conflict[]);
    if (r?.data) setRunners(r.data as Runners);
    setLoading(false);
  }, [client, wantRunners]);

  useEffect(() => {
    void refresh();
    const onSignedOut = () => setSignedOut(true);
    window.addEventListener(UNAUTHORIZED_EVENT, onSignedOut);
    const stop = subscribe<SyncStatus | { change: string }>(
      "/api/events?topics=kbsync.status,kb.page_changed",
      (e) => {
        if (e.topic === "kbsync.status") setSync(e.data as SyncStatus);
        else void client.GET("/api/kb/conflicts").then((c) => c.data && setConflicts(c.data as Conflict[]));
      },
      { events: ["kbsync.status", "kb.page_changed"] },
    );
    return () => {
      stop();
      window.removeEventListener(UNAUTHORIZED_EVENT, onSignedOut);
    };
  }, [client, refresh]);

  return { loading, signedOut, health, sync, conflicts, runners, refresh, setSync };
}
