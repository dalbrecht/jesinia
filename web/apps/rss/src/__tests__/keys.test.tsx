// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { CHORD_TIMEOUT_MS, KEY_HELP, useKeys, type KeyMap } from "../keys";

/** Every binding from the spec's key table (§2.2), as `useKeys` names them. */
const ALL = ["j", "k", "Enter", "o", "n", "e", "m", "Shift+A", "g a", "g i", "/", "?"] as const;

function Harness({ map }: { map: KeyMap }) {
  useKeys(map);
  return (
    <div>
      <input aria-label="field" />
      <textarea aria-label="area" />
      <div aria-label="editable" contentEditable suppressContentEditableWarning />
      <button type="button">button</button>
    </div>
  );
}

function setup() {
  const map = Object.fromEntries(ALL.map((k) => [k, vi.fn<() => void>()])) as Record<
    (typeof ALL)[number],
    Mock<() => void>
  >;
  const view = render(<Harness map={map} />);
  return { map, view };
}

const press = (key: string, init: KeyboardEventInit = {}, target: Element = document.body) =>
  fireEvent.keyDown(target, { key, bubbles: true, ...init });

afterEach(() => vi.useRealTimers());

describe("useKeys", () => {
  it("runs every key in the map once", () => {
    const { map } = setup();
    press("j");
    press("k");
    press("Enter");
    press("o");
    press("n");
    press("e");
    press("m");
    press("A", { shiftKey: true });
    press("g");
    press("a");
    press("g");
    press("i");
    press("/");
    press("?", { shiftKey: true });
    for (const key of ALL) expect(map[key], key).toHaveBeenCalledTimes(1);
  });

  it("is quiet while an input, a textarea or a contenteditable has focus", () => {
    const { map, view } = setup();
    for (const label of ["field", "area", "editable"]) {
      const el = view.getByLabelText(label);
      el.focus();
      expect(document.activeElement).toBe(el);
      press("j", {}, el);
      press("A", { shiftKey: true }, el);
      press("/", {}, el);
    }
    expect(map.j).not.toHaveBeenCalled();
    expect(map["Shift+A"]).not.toHaveBeenCalled();
    expect(map["/"]).not.toHaveBeenCalled();
  });

  it("leaves Enter to a focused button, and ignores keys held with ⌘, Ctrl or Alt", () => {
    const { map, view } = setup();
    const button = view.getByRole("button", { name: "button" });
    button.focus();
    press("Enter", {}, button);
    expect(map.Enter).not.toHaveBeenCalled();
    press("j", {}, button);
    expect(map.j).toHaveBeenCalledTimes(1);
    button.blur();
    press("k", { metaKey: true });
    press("k", { ctrlKey: true });
    press("k", { altKey: true });
    expect(map.k).not.toHaveBeenCalled();
  });

  it("Caps Lock letters still work, and held-down repeats are ignored", () => {
    const { map } = setup();
    press("J");
    press("M");
    expect(map.j).toHaveBeenCalledTimes(1);
    expect(map.m).toHaveBeenCalledTimes(1);
    press("j", { repeat: true });
    expect(map.j).toHaveBeenCalledTimes(1);
    expect(map["Shift+A"]).not.toHaveBeenCalled();
  });

  it("a plain A is not Shift+A, and an unmapped key does nothing", () => {
    const { map } = setup();
    press("a");
    press("x");
    expect(map["Shift+A"]).not.toHaveBeenCalled();
    expect(map["g a"]).not.toHaveBeenCalled();
  });

  it("the g chord times out after 1 s", () => {
    vi.useFakeTimers();
    const { map } = setup();
    expect(CHORD_TIMEOUT_MS).toBe(1000);
    press("g");
    act(() => vi.advanceTimersByTime(CHORD_TIMEOUT_MS + 1));
    press("a");
    expect(map["g a"]).not.toHaveBeenCalled();
    press("g");
    act(() => vi.advanceTimersByTime(CHORD_TIMEOUT_MS - 100));
    press("i");
    expect(map["g i"]).toHaveBeenCalledTimes(1);
  });

  it("a key after g that finishes no chord is dropped, not run on its own", () => {
    const { map } = setup();
    press("g");
    press("j");
    expect(map.j).not.toHaveBeenCalled();
    press("j");
    expect(map.j).toHaveBeenCalledTimes(1);
  });

  it("uses the latest handlers and stops listening on unmount", () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = render(<Harness map={{ j: first }} />);
    view.rerender(<Harness map={{ j: second }} />);
    press("j");
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    view.unmount();
    press("j");
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("the help list names every binding", () => {
    const listed = KEY_HELP.flatMap((h) => h.keys);
    for (const key of ALL) expect(listed, key).toContain(key);
  });
});
