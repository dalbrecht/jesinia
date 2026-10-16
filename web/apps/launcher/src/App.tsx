// SPDX-License-Identifier: AGPL-3.0-or-later
import type { JesiniaClient } from "@jesinia/client";
import { AppShell } from "@jesinia/shell";
import { Launcher } from "./Launcher";
import { Settings } from "./Settings";

/** One PWA, two views: "/" (launcher) and "/settings/". */
export function App({ path, client }: { path: string; client?: JesiniaClient }) {
  const settings = path.startsWith("/settings");
  return (
    <AppShell app={{ id: settings ? "settings" : "launcher", name: settings ? "Settings" : "Jesinia", glyph: settings ? "/icons/settings.svg" : "/icons/monochrome.svg" }} {...(client ? { client } : {})}>
      {(status) => (settings ? <Settings status={status} /> : <Launcher status={status} />)}
    </AppShell>
  ); // prettier-ignore
}
