// SPDX-License-Identifier: AGPL-3.0-or-later
// Export every app icon from docs/design/icons/<app>/{source.jpg,symbol.svg} (brief §8a)
// into docs/design/icons/<app>/export/, then copy the built apps' icons into their
// public/ folders. Rendered in Chromium so they match docs/design/icons/index.html.
//
//   node scripts/export-icons.mjs            (all apps; run through `make icons`)
//   node scripts/export-icons.mjs notes rss  (only these)
import { copyFileSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "../..");
const iconsDir = join(root, "docs/design/icons");

/** Each app's identity hue (brief §8a); Launcher and Settings are near-neutral paper. */
const APPS = new Map([
  ["notes", "sage"],
  ["rss", "marigold"],
  ["review", "plum"],
  ["issues", "cornflower"],
  ["launcher", "paper"],
  ["settings", "paper"],
]);

/** Built apps: where their icons ship, and which design icons they carry. */
const SHIPPED = {
  "web/apps/launcher/public/icons": { app: "launcher", glyphs: ["settings"] },
  "web/apps/notes/public/icons": { app: "notes", glyphs: [] },
  "web/apps/rss/public/icons": { app: "rss", glyphs: [] },
};

/** The photo exports; `inset` is the symbol's margin (17% ≈ 66% of the tile, 26% keeps it in the maskable zone). */
const PHOTO = [
  ["master-1024.png", 1024, 17],
  ["icon-512.png", 512, 17],
  ["icon-192.png", 192, 17],
  ["maskable-512.png", 512, 26],
  ["apple-touch-icon.png", 180, 17],
];

/** Token values straight from the design package, so exports follow token changes. */
function tokens() {
  const css = readFileSync(join(root, "web/packages/design/src/tokens.css"), "utf8");
  const all = new Map([...css.matchAll(/--jx-([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  const value = (name) => {
    const v = all.get(name);
    if (!v) throw new Error(`token --jx-${name} not found in tokens.css`);
    return v;
  };
  return { value, paper0: value("paper-0"), paper950: value("paper-950") };
}

function dataUrl(path, type) {
  return `data:${type};base64,${readFileSync(path).toString("base64")}`;
}

const masked = (sym, inset, color, extra = "") =>
  `<div style="position:absolute;inset:${inset}%;background:${color};-webkit-mask:url(${sym}) center/contain no-repeat;mask:url(${sym}) center/contain no-repeat;${extra}"></div>`;

function photoPage(size, inset, img, sym, t) {
  const shadow = `filter:drop-shadow(0 ${size / 160}px ${size / 100}px color-mix(in oklch, ${t.paper950} 50%, transparent))`;
  return `<div style="width:${size}px;height:${size}px;position:relative;overflow:hidden;background:url(${img}) center/cover">
<div style="position:absolute;inset:0;background:radial-gradient(circle at 50% 50%, transparent 35%, color-mix(in oklch, ${t.paper950} 45%, transparent) 100%)"></div>
${masked(sym, inset, t.paper0, shadow)}</div>`;
}

/** ≤ 32 px: no image, a flat app-hue tile with the symbol (brief §8a). */
const flatPage = (size, sym, fill, t) =>
  `<div style="width:${size}px;height:${size}px;position:relative;border-radius:22.5%;background:${fill}">${masked(sym, 12, t.paper0)}</div>`;

/** 16 px: the symbol alone. */
const symbolPage = (size, sym, t) =>
  `<div style="width:${size}px;height:${size}px;position:relative">${masked(sym, 0, t.paper950)}</div>`;

async function shoot(tab, html, size, path, transparent) {
  await tab.setViewportSize({ width: size, height: size });
  await tab.setContent(`<!doctype html><html><body style="margin:0;background:transparent">${html}</body></html>`);
  await tab.screenshot({ path, omitBackground: transparent, clip: { x: 0, y: 0, width: size, height: size } });
}

async function exportApp(tab, app, t) {
  const src = join(iconsDir, app);
  const out = join(src, "export");
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  const img = dataUrl(join(src, "source.jpg"), "image/jpeg");
  const sym = dataUrl(join(src, "symbol.svg"), "image/svg+xml");
  for (const [name, size, inset] of PHOTO)
    await shoot(tab, photoPage(size, inset, img, sym, t), size, join(out, name), false);
  await shoot(tab, flatPage(32, sym, t.value(`${APPS.get(app)}-600`), t), 32, join(out, "favicon-32.png"), true);
  await shoot(tab, symbolPage(16, sym, t), 16, join(out, "favicon-16.png"), true);
  copyFileSync(join(src, "symbol.svg"), join(out, "monochrome.svg"));
  return out;
}

/** Copy the exports a built app serves; the design glyphs it shows go in as `<app>.svg`. */
function ship() {
  for (const [dir, { app, glyphs }] of Object.entries(SHIPPED)) {
    const dest = join(root, dir);
    rmSync(dest, { recursive: true, force: true });
    mkdirSync(dest, { recursive: true });
    const from = join(iconsDir, app, "export");
    for (const name of [
      "icon-192.png",
      "icon-512.png",
      "maskable-512.png",
      "apple-touch-icon.png",
      "favicon-32.png",
      "favicon-16.png",
      "monochrome.svg",
    ]) {
      copyFileSync(join(from, name), join(dest, name));
    }
    for (const glyph of glyphs) copyFileSync(join(iconsDir, glyph, "symbol.svg"), join(dest, `${glyph}.svg`));
  }
}

const wanted = process.argv.slice(2);
for (const app of wanted)
  if (!APPS.has(app)) throw new Error(`unknown app ${app}; known: ${[...APPS.keys()].join(", ")}`);
const t = tokens();
const browser = await chromium.launch();
try {
  const tab = await browser.newPage();
  for (const app of wanted.length ? wanted : APPS.keys()) {
    const out = await exportApp(tab, app, t);
    console.log(`${app}: ${out.replace(`${root}/`, "")}`);
  }
} finally {
  await browser.close();
}
ship();
console.log(`shipped: ${Object.keys(SHIPPED).join(", ")}`);
