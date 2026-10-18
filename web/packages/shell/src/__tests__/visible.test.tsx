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
