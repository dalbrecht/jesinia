// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const out = fileURLToPath(new URL("../../../server/jesinia/static/launcher", import.meta.url));

/** Ship the shell's service worker next to index.html. */
function serviceWorker(): Plugin {
  return {
    name: "jesinia-sw",
    writeBundle() {
      copyFileSync(fileURLToPath(import.meta.resolve("@jesinia/shell/sw.js")), `${out}/sw.js`);
    },
  };
}

// The launcher owns "/" and "/settings/" (spec §3.5).
export default defineConfig({
  base: "/",
  plugins: [react(), serviceWorker()],
  build: { outDir: out, emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:4870", "/healthz": "http://127.0.0.1:4870" } },
});
