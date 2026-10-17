// SPDX-License-Identifier: AGPL-3.0-or-later
// The Read view's Markdown (R-REN2): page links, local images, AI blocks, tags, front matter.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { kbAssetUrl, prepareMarkdown, ReadView } from "../read";

const ID = "0199e000-0000-7000-8000-000000000001";
const OTHER = "0199e000-0000-7000-8000-000000000002";

function read(markdown: string, title = "Kept post") {
  const open = vi.fn();
  const page = { id: ID, path: `Sources/${title}.md`, title };
  const view = render(<ReadView markdown={markdown} page={page} onOpenPage={open} />);
  return { ...view, open, root: view.container };
}

describe("kbAssetUrl", () => {
  it("maps a relative asset path, resolved against the page, to the asset route", () => {
    expect(kbAssetUrl(`../_assets/${ID}/a1b2.png`, { id: ID, path: "Sources/x.md" })).toBe(
      `/api/kb/assets/${ID}/a1b2.png`,
    );
    expect(kbAssetUrl(`../../_assets/${ID}/a.jpg`, { id: ID, path: "Reading/Feeds/x.md" })).toBe(
      `/api/kb/assets/${ID}/a.jpg`,
    );
    expect(kbAssetUrl(`_assets/${ID}/a.png`, { id: ID, path: "Top.md" })).toBe(`/api/kb/assets/${ID}/a.png`);
    expect(kbAssetUrl(`./../_assets/${ID}/a.png`, { id: ID, path: "Sources/x.md" })).toBe(`/api/kb/assets/${ID}/a.png`);
  });

  it("is pinned to the page's own asset folder, after normalizing", () => {
    const src = `../_assets/${ID}/../${OTHER}/a.png`;
    expect(kbAssetUrl(src, { id: ID, path: "Sources/x.md" })).toBeNull();
    expect(kbAssetUrl(src, { id: OTHER, path: "Sources/x.md" })).toBe(`/api/kb/assets/${OTHER}/a.png`);
    expect(kbAssetUrl(`../_assets/${OTHER}/a.png`, { id: ID, path: "Sources/x.md" })).toBeNull();
  });

  it("refuses anything that isn't exactly a page's asset", () => {
    for (const [src, path] of [
      [`../../_assets/${ID}/a.png`, "Sources/x.md"], // above the KB root
      [`../_assets/${ID}/a.png`, "Top.md"],
      [`_assets/${ID}/a.png`, "Sources/x.md"], // resolves to Sources/_assets
      [`../_assets/${ID}/.gitignore`, "Sources/x.md"],
      [`../_assets/${ID}/sub/a.png`, "Sources/x.md"],
      [`../_assets/not-an-id/a.png`, "Sources/x.md"],
      [`../_assets/${ID}/a%2e.png`, "Sources/x.md"],
      [`../_assets/${ID}/a.png?x=1`, "Sources/x.md"],
      [`../Notes/a.png`, "Sources/x.md"],
      [`https://cdn.example/_assets/${ID}/a.png`, "Sources/x.md"],
      [`/_assets/${ID}/a.png`, "Sources/x.md"],
      [`javascript:alert(1)`, "Sources/x.md"],
    ] as const) {
      expect(kbAssetUrl(src, { id: ID, path }), src).toBeNull();
    }
  });
});

describe("prepareMarkdown", () => {
  it("hides a front-matter block, and only a real one", () => {
    expect(prepareMarkdown(`---\nid: ${ID}\ntitle: T\n---\n# Body`)).toBe("# Body");
    expect(prepareMarkdown("---\n\nNot front matter.\n\n---\nText")).toContain("Not front matter.");
  });
});

describe("ReadView", () => {
  it("renders headings, emphasis and links (in a new tab)", () => {
    const { root } = read("## Article\n\nSome *text* and [a site](https://site.example/a).");
    expect(screen.getByRole("heading", { level: 2, name: "Article" })).toBeInTheDocument();
    expect(root.querySelector("em")).toHaveTextContent("text");
    const a = screen.getByRole("link", { name: "a site" });
    expect(a).toHaveAttribute("href", "https://site.example/a");
    expect(a).toHaveAttribute("target", "_blank");
    expect(root).not.toHaveTextContent("##");
  });

  it("never shows front matter", () => {
    const { root } = read(`---\nid: ${ID}\njesinia:\n  kind: source/rss\n---\nBody text.`);
    expect(root).toHaveTextContent("Body text.");
    expect(root).not.toHaveTextContent("kind");
    expect(root).not.toHaveTextContent(ID);
  });

  it("[[id|Title]] and [[id]] open that page in Notes", () => {
    const { open } = read(`See [[${OTHER}|Other page]] and [[${ID}]]. Not [[nope|x]].`);
    const link = screen.getByRole("link", { name: "Other page" });
    expect(link).toHaveAttribute("href", `/notes/page/${OTHER}`);
    expect(link).not.toHaveAttribute("target");
    link.click();
    expect(open).toHaveBeenCalledWith(OTHER);
    expect(screen.getByRole("link", { name: ID })).toHaveAttribute("href", `/notes/page/${ID}`);
    expect(screen.getByText(/Not \[\[nope\|x\]\]/)).toBeInTheDocument();
  });

  it("page links and tags at the very start and end of a text", () => {
    const { root } = read(`[[${OTHER}|First]] middle #last\n\n#first then [[${OTHER}]]`);
    expect(screen.getByRole("link", { name: "First" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: OTHER })).toBeInTheDocument();
    expect([...root.querySelectorAll("[data-tag]")].map((c) => c.textContent)).toEqual(["#last", "#first"]);
  });

  it("a page link's text is plain inside another link", () => {
    const { root } = read(`[see [[${OTHER}|x]] #tag](https://a.example/)`);
    const links = root.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveTextContent(`see [[${OTHER}|x]] #tag`);
    expect(root.querySelector("[data-tag]")).toBeNull();
  });

  it("a page link inside code stays code", () => {
    const { root } = read(`\`[[${OTHER}|In code]]\`\n\n    [[${OTHER}|Indented]]`);
    expect(root.querySelector("a")).toBeNull();
    expect(root.querySelector("code")).toHaveTextContent(`[[${OTHER}|In code]]`);
  });

  it("local images load from the asset route; remote ones are alt text and host", () => {
    const { root } = read(
      `![A chart](../_assets/${ID}/a1b2.png)\n\n![Remote](https://cdn.example/r.png)\n\n![Escape](../../etc/x.png)\n\n![Data](data:image/png;base64,AAAA)`,
    );
    const imgs = [...root.querySelectorAll("img")];
    expect(imgs.map((i) => i.getAttribute("src"))).toEqual([`/api/kb/assets/${ID}/a1b2.png`]);
    expect(imgs[0]).toHaveAttribute("alt", "A chart");
    expect(root).toHaveTextContent("Remote (cdn.example)");
    expect(root).toHaveTextContent("Escape");
    for (const img of root.querySelectorAll("img")) expect(img.getAttribute("src")).toMatch(/^\/api\/kb\/assets\//);
  });

  it("an AI provenance block is a labelled callout with its agent and model", () => {
    read(
      [
        "Mine.",
        "",
        `<!-- jesinia:ai agent=claude-code model=claude-opus-5-5 at=2026-10-16T09:20:00Z session=s1 -->`,
        "Takeaway: agents wrote this.",
        "<!-- /jesinia:ai -->",
        "",
        "Mine again.",
      ].join("\n"),
    );
    const callout = screen.getByRole("note", { name: "Written by an agent" });
    expect(callout).toHaveTextContent("AI · claude-code · claude-opus-5-5");
    expect(callout).toHaveTextContent("Takeaway: agents wrote this.");
    expect(callout).not.toHaveTextContent("Mine");
    expect(screen.getByText("Mine again.")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("jesinia:ai");
  });

  it("an AI block left open runs to the end; a stray close marker is dropped", () => {
    read("<!-- /jesinia:ai -->\n\nBefore.\n\n<!-- jesinia:ai agent=codex -->\nUnclosed.\n\nMore.");
    const callout = screen.getByRole("note", { name: "Written by an agent" });
    expect(callout).toHaveTextContent("AI · codex");
    expect(callout).toHaveTextContent("Unclosed.More.");
    expect(callout).not.toHaveTextContent("Before.");
    expect(document.body).not.toHaveTextContent("jesinia:ai");
  });

  it("a second open marker starts a new block; a marker without an agent still says AI", () => {
    read("<!-- jesinia:ai agent=a model=m -->\nOne.\n<!-- jesinia:ai -->\nTwo.\n<!-- /jesinia:ai -->");
    const callouts = screen.getAllByRole("note", { name: "Written by an agent" });
    expect(callouts.map((c) => c.textContent)).toEqual(["AI · a · mOne.", "AI · agentTwo."]);
  });

  it("tags are chips; an escaped \\# is a plain #, and code and headings have none", () => {
    const { root } = read(
      "# Title #notatag\n\nTags: #sqlite #wal-mode\n\nQuoted \\#hashtag, C# and a #b_c, `#code` and \\\\#real.",
    );
    const chips = [...root.querySelectorAll("[data-tag]")].map((c) => c.textContent);
    expect(chips).toEqual(["#sqlite", "#wal-mode"]); // `\\#real`: a backslash, then no tag
    expect(root).toHaveTextContent("Quoted #hashtag, C# and a #b_c, #code and \\#real.");
    expect(root.querySelector("code")).toHaveTextContent("#code");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Title #notatag");
  });

  it("follows the tag grammar: a trailing dash isn't part of a tag, (#a) in prose is one", () => {
    const { root } = read("#trail- and (#paren) and #ünïcode, not #x_y or a#b or ?q=#z or [l](#anchor)");
    const chips = [...root.querySelectorAll("[data-tag]")].map((c) => c.getAttribute("data-tag"));
    expect(chips).toEqual(["trail", "paren", "ünïcode"]);
  });

  it("escapes survive in code, link targets and image alt text", () => {
    const { root } = read(
      "`a \\#b`\n\n```\n\\#x\n```\n\n[l](https://h.example/\\#frag) ![alt \\#1](https://c.example/i.png)",
    );
    const codes = [...root.querySelectorAll("code")].map((c) => c.textContent);
    expect(codes).toEqual(["a \\#b", "\\#x\n"]);
    expect(screen.getByRole("link", { name: "l" })).toHaveAttribute("href", "https://h.example/#frag");
    expect(root).toHaveTextContent("alt #1 (c.example)");
  });

  it("hides a first h1 that repeats the page title (case and spacing aside)", () => {
    read("#  kept   POST \n\nBody.");
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    expect(screen.getByText("Body.")).toBeInTheDocument();
  });

  it("hides the title repeated as the first heading after ## Article", () => {
    read("A note.\n\n## Article\n\n![x](https://c.example/x.png)\n\n# Kept *post*\n\nText.\n\n# Kept post");
    expect(screen.getByRole("heading", { level: 2, name: "Article" })).toBeInTheDocument();
    // Only the first one goes: a later heading with the same text is the author's.
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("keeps a first heading that differs from the title, or isn't an h1", () => {
    read("# Something else\n\n## Article\n\n## Kept post\n\n# Kept post");
    const h1s = screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent);
    expect(h1s).toEqual(["Something else", "Kept post"]);
    expect(screen.getByRole("heading", { level: 2, name: "Kept post" })).toBeInTheDocument();
  });
});
