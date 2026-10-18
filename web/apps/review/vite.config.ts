// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const out = fileURLToPath(new URL("../../../server/jesinia/static/review", import.meta.url));

/** Ship the shell's service worker next to index.html. */
function serviceWorker(): Plugin {
  return {
    name: "jesinia-sw",
    writeBundle() {
      copyFileSync(fileURLToPath(import.meta.resolve("@jesinia/shell/sw.js")), `${out}/sw.js`);
    },
  };
}

// Review is served at /review/ (spec §3.5); its PR routes fall back to index.html.
export default defineConfig({
  base: "/review/",
  plugins: [react(), serviceWorker()],
  // No data: URIs (the CSP allows same-origin fonts and images only).
  build: { outDir: out, emptyOutDir: true, assetsInlineLimit: 0 },
  server: { proxy: { "/api": "http://127.0.0.1:5374", "/healthz": "http://127.0.0.1:5374" } },
});
