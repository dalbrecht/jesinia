// SPDX-License-Identifier: AGPL-3.0-or-later
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

/** jsdom has no EventSource; tests that care replace this with their own fake. */
class QuietEventSource {
  onerror: (() => void) | null = null;
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
}
vi.stubGlobal("EventSource", QuietEventSource);

// jsdom has no scrollIntoView; the drag primitives' keyboard sensor calls it on pick-up.
Element.prototype.scrollIntoView ??= () => {};

// Opened tabs are kept in storage; every test starts with none.
beforeEach(() => localStorage.clear());
afterEach(() => cleanup());
