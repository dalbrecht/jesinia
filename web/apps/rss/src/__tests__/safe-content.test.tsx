// SPDX-License-Identifier: AGPL-3.0-or-later
// Ruling R17: feed HTML and Markdown reach the page only as React elements built through an
// allowlist; nothing from a feed can run.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeHtml } from "../SafeContent";

const BASE = "https://blog.example/posts/one";

function html(source: string) {
  return render(<SafeHtml html={source} base={BASE} />).container;
}

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

describe("SafeHtml", () => {
  it("keeps allowed structure and text", () => {
    const root = html(
      "<h2>Head</h2><p>One <em>two</em> <strong>three</strong> <b>b</b> <i>i</i> <code>x</code></p>" +
        "<blockquote>q</blockquote><ul><li>a</li></ul><ol><li>b</li></ol><pre>p</pre><hr><br>" +
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>d</td></tr></tbody></table>" +
        "<figure><img src='/i.png' alt='pic'><figcaption>cap</figcaption></figure>",
    );
    for (const tag of ["h2", "p", "em", "strong", "b", "i", "code", "blockquote", "ul", "ol", "li", "pre", "hr", "br"])
      expect(root.querySelector(tag), tag).not.toBeNull();
    for (const tag of ["table", "thead", "tbody", "tr", "th", "td", "figure", "img", "figcaption"])
      expect(root.querySelector(tag), tag).not.toBeNull();
    expect(root).toHaveTextContent("One two three b i x");
  });

  it("resolves links against the post and opens them safely", () => {
    const root = html("<p><a href='/two' title='t'>next</a> <a href='https://other.example/x'>abs</a></p>");
    const links = root.querySelectorAll("a");
    expect(links[0]?.getAttribute("href")).toBe("https://blog.example/two");
    expect(links[1]?.getAttribute("href")).toBe("https://other.example/x");
    for (const a of links) {
      expect(a.getAttribute("target")).toBe("_blank");
      expect(a.getAttribute("rel")).toBe("noopener noreferrer nofollow");
      expect(a.hasAttribute("title")).toBe(false);
    }
  });

  it("a javascript: href becomes plain text", () => {
    const root = html("<p><a href='javascript:alert(1)'>click</a> <a href=' JaVaScRiPt:alert(1)'>two</a></p>");
    expect(root.querySelector("a")).toBeNull();
    expect(root).toHaveTextContent("click two");
    expectInert(root);
  });

  it("a data: image is dropped; others load lazily without a referrer", () => {
    const root = html(
      "<p><img src='data:image/svg+xml,<svg onload=alert(1)>' alt='x'><img src='pic.png' alt='ok'></p>",
    );
    const imgs = root.querySelectorAll("img");
    expect(imgs).toHaveLength(1);
    expect(imgs[0]?.getAttribute("src")).toBe("https://blog.example/posts/pic.png");
    expect(imgs[0]?.getAttribute("loading")).toBe("lazy");
    expect(imgs[0]?.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(imgs[0]?.getAttribute("alt")).toBe("ok");
    expectInert(root);
  });

  it("an http: image is its alt text: the reader's CSP would block it (https only)", () => {
    const root = html(
      "<p><img src='http://plain.example/a.png' alt='a chart'><img src='http://plain.example/b.png'></p>",
    );
    expect(root.querySelector("img")).toBeNull();
    expect(root).toHaveTextContent("a chart");
    // A relative image on an http: page resolves to http: too.
    const page = render(<SafeHtml html="<img src='pic.png' alt='rel'>" base="http://plain.example/post" />).container;
    expect(page.querySelector("img")).toBeNull();
    expect(page).toHaveTextContent("rel");
  });

  it("drops onerror and every other attribute outside the allowlist", () => {
    const root = html(
      "<p style='color:red' class='x' id='y' onclick='alert(1)'><img src='x.png' onerror='alert(1)'></p>",
    );
    const p = root.querySelector("p");
    expect(p?.attributes).toHaveLength(0);
    expect(root.querySelector("img")?.hasAttribute("onerror")).toBe(false);
    expectInert(root);
  });

  it("script, iframe, style and other unknown elements produce nothing executable", () => {
    const root = html(
      "<p>keep</p><script>alert(1)</script><iframe src='https://evil.example'></iframe>" +
        "<style>p{}</style><object data='x'></object><form><input></form>" +
        "<svg><a href='javascript:alert(1)'>s</a></svg><div><span>inner text</span></div>",
    );
    expect(root).toHaveTextContent("keep");
    expect(root).toHaveTextContent("inner text");
    expect(root).not.toHaveTextContent("alert(1)");
    expectInert(root);
  });

  it("an unresolvable URL is dropped, and an http(s) base is required to resolve one", () => {
    const root = html("<a href='http://[bad'>bad</a><img src='https://[x'>");
    expect(root.querySelector("a")).toBeNull();
    expect(root.querySelector("img")).toBeNull();
    const noBase = render(
      <SafeHtml html="<a href='/rel'>rel</a><a href='https://ok.example/'>ok</a>" base="" />,
    ).container;
    expect(noBase.querySelectorAll("a")).toHaveLength(1);
    expect(noBase.querySelector("a")?.getAttribute("href")).toBe("https://ok.example/");
  });

  it("an anchor without href, comments and layout whitespace leave only the text", () => {
    const root = html("<p><a name='x'>anchor</a><!-- note --></p><table>\n  <tr>\n <td>cell</td>\n </tr>\n</table>");
    expect(root.querySelector("a")).toBeNull();
    expect(root).toHaveTextContent("anchor");
    expect(root.querySelector("td")).toHaveTextContent("cell");
    expect(root.innerHTML).not.toContain("note");
  });
});
