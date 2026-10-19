// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// Served by the Jesinia server at /design/ (a reference page, not an app).
export default defineConfig({
  base: "/design/",
  plugins: [react()],
  build: {
    outDir: fileURLToPath(new URL("../../../server/jesinia/static/design", import.meta.url)),
    emptyOutDir: true,
    assetsInlineLimit: 0, // no data: URIs (CSP)
    // drag.html: the drag-and-drop primitives in a real browser (the e2e suite drives it).
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL("index.html", import.meta.url)),
        drag: fileURLToPath(new URL("drag.html", import.meta.url)),
      },
    },
  },
});
