// SPDX-License-Identifier: AGPL-3.0-or-later
// Hidden and direction-changing characters made visible (R62), shared by every app.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { hasHidden, revealHidden, VisibleText, visibleText } from "../visible";

describe("revealHidden", () => {
  it("splits text into plain runs and tokens for each hidden character", () => {
    expect(revealHidden("plain")).toEqual([{ text: "plain" }]);
    expect(revealHidden("")).toEqual([{ text: "" }]);
    expect(revealHidden("a\u202Eb\u200Bc\u0007\u2066")).toEqual([
      { text: "a" },
      { code: "U+202E", kind: "bidi" },
      { text: "b" },
      { code: "U+200B", kind: "invisible" },
      { text: "c" },
      { code: "U+0007", kind: "control" },
      { code: "U+2066", kind: "bidi" },
    ]);
  });

  it("treats a tab and a line's final CR as ordinary", () => {
    expect(hasHidden("tab\tis fine")).toBe(false);
    expect(hasHidden("crlf line\r")).toBe(false);
    expect(hasHidden("a\rb")).toBe(true);
    expect(hasHidden("x\u2069")).toBe(true);
    expect(hasHidden("soft\u00ADhyphen")).toBe(true);
    expect(hasHidden("\u200F")).toBe(true);
    expect(hasHidden("\u0085")).toBe(true);
  });

  it("reveals the line and paragraph separators, which browsers break lines at", () => {
    expect(revealHidden("a\u2028b\u2029")).toEqual([
      { text: "a" },
      { code: "U+2028", kind: "invisible" },
      { text: "b" },
      { code: "U+2029", kind: "invisible" },
    ]);
  });

  it("keeps LF and CRLF as ordinary text in multiline mode, everything else still hidden", () => {
    const multiline = { multiline: true };
    expect(revealHidden("a\nb\r\nc", multiline)).toEqual([{ text: "a\nb\r\nc" }]);
    expect(hasHidden("one\ntwo\r\nthree\r\n", multiline)).toBe(false);
    expect(revealHidden("a\rb", multiline)).toEqual([{ text: "a" }, { code: "U+000D", kind: "control" }, { text: "b" }]); // prettier-ignore
    expect(hasHidden("a\r", multiline)).toBe(false); // the final-CR rule is unchanged
    for (const hidden of ["\u2028", "\u2029", "\u202E", "\u200B", "\u0007", "\u0085"])
      expect(hasHidden(`x\n${hidden}y`, multiline)).toBe(true);
    expect(visibleText("a\nb\u202E", multiline)).toBe("a\nb⟪U+202E⟫");
  });

  it("still tokenises newlines by default, for names", () => {
    expect(visibleText("a\nb")).toBe("a⟪U+000A⟫b");
    expect(visibleText("a\r\nb")).toBe("a⟪U+000D⟫⟪U+000A⟫b");
  });

  it("spells the tokens out for plain strings and accessible names", () => {
    expect(visibleText("ab\u202Ecd")).toBe("ab⟪U+202E⟫cd");
  });
});

describe("VisibleText", () => {
  it("renders plain text as it is, and markup only as text", () => {
    const { container } = render(<VisibleText text="<b>bold</b>" />);
    expect(container.textContent).toBe("<b>bold</b>");
    expect(container.querySelector("b")).toBeNull();
  });

  it("marks each hidden character with what it is", () => {
    render(<VisibleText text={"src\u202Etxt.exe\u200B"} />);
    const bidi = screen.getByText("⟪U+202E⟫");
    expect(bidi).toHaveAttribute("data-kind", "bidi");
    expect(bidi).toHaveAttribute("title", "U+202E: a direction-changing (bidi) character");
    expect(screen.getByText("⟪U+200B⟫")).toHaveAttribute("title", "U+200B: an invisible character");
    expect(screen.getByText("src")).toBeInTheDocument();
  });

  it("names a control character", () => {
    render(<VisibleText text={"a\u0001"} />);
    expect(screen.getByText("⟪U+0001⟫")).toHaveAttribute("title", "U+0001: a control character");
  });
});

describe("VisibleText multiline", () => {
  it("shows line breaks as text, not tokens, and still tokenises a bidi control", () => {
    const { container } = render(<VisibleText text={"one\ntwo\u202E"} multiline />);
    expect(container.textContent).toBe("one\ntwo⟪U+202E⟫");
  });

  it("tokenises the newline when not multiline", () => {
    const { container } = render(<VisibleText text={"one\ntwo"} />);
    expect(container.textContent).toBe("one⟪U+000A⟫two");
  });
});
