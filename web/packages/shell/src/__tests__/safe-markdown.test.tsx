// SPDX-License-Identifier: AGPL-3.0-or-later
// Ruling R17: Markdown from feeds and agents reaches the page only through react-markdown with
// raw HTML skipped and http(s)-only URLs. Moved from the RSS app with the shared chat.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { httpOnly, renderMarkdown, resolveHttpUrl, SafeMarkdown, safeHttpUrl } from "../index";

/** Nothing in the tree can execute: no handlers, no script-ish elements, no script URLs. */
function expectInert(root: Element) {
  expect(
    root.querySelector("script, iframe, object, embed, style, form, input, svg, math, base, link, meta"),
  ).toBeNull();
  for (const el of root.querySelectorAll("*")) {
    for (const attr of el.attributes) {
      expect(attr.name.startsWith("on"), `${el.tagName} ${attr.name}`).toBe(false);
      expect(attr.value.trim().toLowerCase().startsWith("javascript:"), attr.value).toBe(false);
      expect(attr.value.trim().toLowerCase().startsWith("data:"), attr.value).toBe(false);
    }
  }
}

describe("SafeMarkdown", () => {
  it("only http(s) URLs pass, resolved against an http(s) base", () => {
    expect(safeHttpUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("/x", "javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("::")).toBeNull();
    expect(httpOnly("/two", "https://blog.example/one")).toBe("https://blog.example/two");
    expect(httpOnly("/two")).toBe("");
    expect(httpOnly("mailto:a@b.example")).toBe("");
    expect(resolveHttpUrl(null, "https://blog.example/")).toBeNull(); // an attribute that isn't there
    expect(resolveHttpUrl(" /p.png ", "https://blog.example/a/")).toBe("https://blog.example/p.png");
  });

  it("renders Markdown with safe links", () => {
    const { container } = render(<SafeMarkdown markdown={"# Title\n\nSee [docs](https://docs.example/a)."} />);
    expect(container.querySelector("h1")).toHaveTextContent("Title");
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://docs.example/a");
    expect(a?.getAttribute("target")).toBe("_blank");
    expect(a?.getAttribute("rel")).toBe("noopener noreferrer nofollow");
  });

  it("a javascript: link renders inert", () => {
    const { container } = render(
      <SafeMarkdown markdown={"[x](javascript:alert(1)) and ![i](data:image/png;base64,AAAA) <javascript:alert(2)>"} />,
    );
    expect(container).toHaveTextContent("x");
    expect(container.querySelector("a[href]")).toBeNull();
    expect(container.querySelector("img[src]")).toBeNull();
    expectInert(container);
  });

  it("raw HTML in Markdown is skipped", () => {
    const { container } = render(
      <SafeMarkdown
        markdown={
          "Hello <img src=x onerror=alert(1)> <script>alert(1)</script>\n\n<iframe src='https://e.example'></iframe>"
        }
      />,
    );
    expect(container).toHaveTextContent("Hello");
    expect(container.querySelector("img, script, iframe")).toBeNull();
    expectInert(container);
  });

  it("keeps http(s) images, lazy and without a referrer", () => {
    const { container } = render(<SafeMarkdown markdown={"![chart](https://img.example/c.png)"} />);
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("https://img.example/c.png");
    expect(img?.getAttribute("alt")).toBe("chart");
    expect(img?.getAttribute("loading")).toBe("lazy");
    expect(img?.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("resolves relative Markdown links and images against the post", () => {
    const { container } = render(
      <SafeMarkdown
        markdown={"[next](/two) ![pic](img/p.png) [bad](javascript:x)"}
        base="https://blog.example/posts/one"
      />,
    );
    expect(container.querySelector("a")?.getAttribute("href")).toBe("https://blog.example/two");
    expect(container.querySelector("img")?.getAttribute("src")).toBe("https://blog.example/posts/img/p.png");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });

  it("renderMarkdown is the same renderer, for ChatTranscript", () => {
    const { container } = render(<div>{renderMarkdown("**bold** [y](javascript:void(0))")}</div>);
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("a[href]")).toBeNull();
    expectInert(container);
  });
});
