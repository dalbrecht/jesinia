// SPDX-License-Identifier: AGPL-3.0-or-later
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

/** jsdom has no EventSource; tests that care replace this with their own fake. */
class QuietEventSource {
  onerror: (() => void) | null = null;
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
}
vi.stubGlobal("EventSource", QuietEventSource);

afterEach(() => cleanup());
