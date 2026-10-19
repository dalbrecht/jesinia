// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const out = fileURLToPath(new URL("../../../server/jesinia/static/issues", import.meta.url));

/** Ship the shell's service worker next to index.html. */
function serviceWorker(): Plugin {
  return {
    name: "jesinia-sw",
    writeBundle() {
      copyFileSync(fileURLToPath(import.meta.resolve("@jesinia/shell/sw.js")), `${out}/sw.js`);
    },
  };
}

// Issues is served at /issues/ (Issues spec §6); its issue routes fall back to index.html.
export default defineConfig({
  base: "/issues/",
  plugins: [react(), serviceWorker()],
  // No data: URIs (the CSP allows same-origin fonts and images only).
  build: { outDir: out, emptyOutDir: true, assetsInlineLimit: 0 },
  server: { proxy: { "/api": "http://127.0.0.1:5374", "/healthz": "http://127.0.0.1:5374" } },
});
