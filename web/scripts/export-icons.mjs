// SPDX-License-Identifier: AGPL-3.0-or-later
// Export PWA icons from docs/design/icons/<app>/{source.jpg,symbol.svg} (brief §8a):
// icon-192.png, icon-512.png (full-bleed composite), maskable-512.png (symbol inside
// the 80% safe zone) and monochrome-512.png (symbol only), rendered in Chromium so they
// match the composite on docs/design/icons/index.html.
//
//   node scripts/export-icons.mjs <app> <out-dir>
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const [app, out] = process.argv.slice(2);
if (!app || !out) throw new Error("usage: export-icons.mjs <app> <out-dir>");
const icons = join(dirname(fileURLToPath(import.meta.url)), "../../docs/design/icons", app);
const img = `data:image/jpeg;base64,${readFileSync(join(icons, "source.jpg")).toString("base64")}`;
const sym = `data:image/svg+xml;base64,${readFileSync(join(icons, "symbol.svg")).toString("base64")}`;

const page = (size, { inset, photo }) => `<!doctype html><html><body style="margin:0;background:transparent">
<div style="width:${size}px;height:${size}px;position:relative;overflow:hidden;${photo ? `background:url(${img}) center/cover` : ""}">
${photo ? `<div style="position:absolute;inset:0;background:radial-gradient(circle at 50% 50%, transparent 35%, rgba(20,24,21,.45) 100%)"></div>` : ""}
<div style="position:absolute;inset:${inset}%;background:${photo ? "#fff" : "#000"};-webkit-mask:url(${sym}) center/contain no-repeat;mask:url(${sym}) center/contain no-repeat;${photo ? `filter:drop-shadow(0 ${size / 160}px ${size / 100}px rgba(20,24,21,.5))` : ""}"></div>
</div></body></html>`;

const variants = [
  ["icon-192.png", 192, { inset: 17, photo: true }],
  ["icon-512.png", 512, { inset: 17, photo: true }],
  ["maskable-512.png", 512, { inset: 26, photo: true }],
  ["monochrome-512.png", 512, { inset: 8, photo: false }],
];

mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
  const tab = await browser.newPage();
  for (const [name, size, opts] of variants) {
    await tab.setViewportSize({ width: size, height: size });
    await tab.setContent(page(size, opts));
    await tab.screenshot({
      path: join(out, name),
      omitBackground: !opts.photo,
      clip: { x: 0, y: 0, width: size, height: size },
    });
  }
} finally {
  await browser.close();
}
console.log(`${app}: ${variants.map((v) => v[0]).join(", ")} → ${out}`);
