// SPDX-License-Identifier: AGPL-3.0-or-later
import { createClient, type JesiniaClient } from "@jesinia/client";
import { createContext, useContext, useMemo, type ReactNode } from "react";

const ClientContext = createContext<JesiniaClient | null>(null);

export function ClientProvider({ client, children }: { client?: JesiniaClient; children: ReactNode }) {
  const value = useMemo(() => client ?? createClient(), [client]);
  return <ClientContext.Provider value={value}>{children}</ClientContext.Provider>;
}

export function useClient(): JesiniaClient {
  const client = useContext(ClientContext);
  if (!client) throw new Error("useClient must be used inside <ClientProvider> (AppShell provides one)");
  return client;
}
