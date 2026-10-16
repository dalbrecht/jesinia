// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// tokens.md §5 is the fixture: every listed pair, resolved from tokens.css in each theme,
// must meet its minimum WCAG 2.x contrast ratio.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(fileURLToPath(new URL("../tokens.css", import.meta.url)), "utf8");
const doc = readFileSync(fileURLToPath(new URL("../../../../../docs/design/tokens.md", import.meta.url)), "utf8");

type Vars = Map<string, string>;

function blocks(source: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  const stripped = source.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const m of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: (m[1] ?? "").replace(/\s+/g, " ").trim(), body: m[2] ?? "" });
  }
  return out;
}

function declarations(body: string, into: Vars): void {
  for (const m of body.matchAll(/(--jx-[\w-]+)\s*:\s*([^;]+);/g)) into.set(m[1] ?? "", (m[2] ?? "").trim());
}

const LIGHT = new Set([":root", ':root, [data-theme="light"]', ":root, [data-theme]"]);

function theme(name: "light" | "dark"): Vars {
  const vars: Vars = new Map();
  for (const b of blocks(css)) {
    if (LIGHT.has(b.selector) || (name === "dark" && b.selector === '[data-theme="dark"]')) {
      declarations(b.body, vars);
    }
  }
  return vars;
}

function resolve(vars: Vars, name: string, depth = 0): string {
  const value = vars.get(name);
  if (value === undefined || depth > 20) throw new Error(`unresolved ${name}`);
  const ref = /^var\((--jx-[\w-]+)\)$/.exec(value);
  return ref ? resolve(vars, ref[1] ?? "", depth + 1) : value;
}

/** oklch(L% C H) → linear sRGB (gamut-clipped) → WCAG relative luminance. */
export function luminance(oklch: string): number {
  const m = /^oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)\s*\)$/.exec(oklch);
  if (!m) throw new Error(`not an opaque oklch color: ${oklch}`);
  const L = Number(m[1]) / 100;
  const C = Number(m[2]);
  const h = (Number(m[3]) * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const mm = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const clip = (x: number) => Math.min(1, Math.max(0, x));
  const r = clip(4.0767416621 * l - 3.3077115913 * mm + 0.2309699292 * s);
  const g = clip(-1.2684380046 * l + 2.6097574011 * mm - 0.3413193965 * s);
  const bl = clip(-0.0041960863 * l - 0.7034186147 * mm + 1.707614701 * s);
  return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
}

export function ratio(fg: string, bg: string): number {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const table = doc
  .slice(doc.indexOf("## 5. Contrast table"), doc.indexOf("## 6. Full CSS"))
  .split("\n")
  .map((line) => /^\| `(--jx-[\w-]+)` \| `(--jx-[\w-]+)` \| ([\d.]+) \|/.exec(line))
  .filter((m): m is RegExpExecArray => m !== null)
  .map((m) => ({ fg: m[1] ?? "", bg: m[2] ?? "", min: Number(m[3]) }));

describe("tokens.md §5 contrast pairs", () => {
  it("parses the whole table", () => {
    expect(table.length).toBeGreaterThan(60);
  });

  for (const name of ["light", "dark"] as const) {
    const vars = theme(name);
    it.each(table)(`${name}: $fg on $bg ≥ $min`, ({ fg, bg, min }) => {
      expect(ratio(resolve(vars, fg), resolve(vars, bg))).toBeGreaterThanOrEqual(min);
    });
  }

  it("defines the modal scrim as translucent paper-950", () => {
    expect(theme("light").get("--jx-scrim")).toBe("oklch(19.5% 0.012 150 / 0.25)");
  });

  it("fails loudly on unknown tokens and non-oklch values", () => {
    expect(() => resolve(theme("light"), "--jx-nope")).toThrow("unresolved");
    expect(() => luminance("red")).toThrow("not an opaque");
    expect(ratio("oklch(100% 0 0)", "oklch(0% 0 0)")).toBeCloseTo(21, 0);
  });
});
