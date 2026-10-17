// SPDX-License-Identifier: AGPL-3.0-or-later
// The PWA icon set from brief §8a, exported by `make icons`: every file the manifest and
// page reference must ship in public/.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const app = join(__dirname, "../..");
const BASE = "/rss";
const shipped = (href: string) => existsSync(join(app, "public", href.slice(BASE.length)));

type Icon = { src: string; sizes: string; type: string; purpose?: string };
const manifest = JSON.parse(readFileSync(join(app, "public/manifest.webmanifest"), "utf8")) as { icons: Icon[] };
const html = readFileSync(join(app, "index.html"), "utf8");
const links = [...html.matchAll(/<link rel="(icon|apple-touch-icon)"[^>]*>/g)].map((m) => m[0]);

describe("app icons", () => {
  it("the manifest lists 192 and 512 any, 512 maskable and an SVG monochrome, all shipped", () => {
    const by = (purpose: string | undefined, sizes: string) =>
      manifest.icons.find((i) => i.purpose === purpose && i.sizes === sizes);
    expect(by(undefined, "192x192")?.type).toBe("image/png");
    expect(by(undefined, "512x512")?.type).toBe("image/png");
    expect(by("maskable", "512x512")?.type).toBe("image/png");
    expect(by("monochrome", "any")?.type).toBe("image/svg+xml");
    for (const icon of manifest.icons) expect(shipped(icon.src), icon.src).toBe(true);
  });

  it("the page links 32 and 16 px favicons and the Apple touch icon, all shipped", () => {
    expect(links.some((l) => l.includes('sizes="32x32"') && l.includes("favicon-32.png"))).toBe(true);
    expect(links.some((l) => l.includes('sizes="16x16"') && l.includes("favicon-16.png"))).toBe(true);
    expect(links.some((l) => l.includes("apple-touch-icon") && l.includes("apple-touch-icon.png"))).toBe(true);
    for (const l of links) expect(shipped(/href="([^"]+)"/.exec(l)?.[1] ?? ""), l).toBe(true);
  });
});
