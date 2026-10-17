// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const out = fileURLToPath(new URL("../../../server/jesinia/static/rss", import.meta.url));

/** Ship the shell's service worker next to index.html. */
function serviceWorker(): Plugin {
  return {
    name: "jesinia-sw",
    writeBundle() {
      copyFileSync(fileURLToPath(import.meta.resolve("@jesinia/shell/sw.js")), `${out}/sw.js`);
    },
  };
}

// RSS is served at /rss/ (spec §3.5).
export default defineConfig({
  base: "/rss/",
  plugins: [react(), serviceWorker()],
  // No data: URIs (the CSP allows same-origin fonts and images only).
  build: { outDir: out, emptyOutDir: true, assetsInlineLimit: 0 },
  server: { proxy: { "/api": "http://127.0.0.1:5374", "/healthz": "http://127.0.0.1:5374" } },
});
