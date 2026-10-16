// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, describe, expect, it, vi } from "vitest";
import { subscribe } from "../sse";

class FakeSource {
  static all: FakeSource[] = [];
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  closed = false;
  constructor(
    public url: string,
    public init?: EventSourceInit,
  ) {
    FakeSource.all.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(name, fn);
  }
  close() {
    this.closed = true;
  }
  emit(name: string, id: string, data: string) {
    this.listeners.get(name)?.(new MessageEvent(name, { data, lastEventId: id }));
  }
}

const Impl = FakeSource as unknown as typeof EventSource;

afterEach(() => {
  FakeSource.all = [];
  vi.useRealTimers();
});

describe("subscribe", () => {
  it("reconnect resumes from the last id", () => {
    vi.useFakeTimers();
    const seen: number[] = [];
    const stop = subscribe<{ n: number }>("/api/events?topics=kb.page_changed", (e) => seen.push(e.data.n), {
      EventSource: Impl,
      events: ["kb.page_changed"],
      retryMs: 100,
    });
    const first = FakeSource.all[0] as FakeSource;
    expect(first.url).toBe("http://localhost:3000/api/events?topics=kb.page_changed");
    expect(first.init).toEqual({ withCredentials: true });
    first.emit("kb.page_changed", "1", '{"n":1}');
    first.emit("kb.page_changed", "2", '{"n":2}');
    first.onerror?.();
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(100);
    const second = FakeSource.all[1] as FakeSource;
    expect(new URL(second.url).searchParams.get("after")).toBe("2");
    second.emit("kb.page_changed", "2", '{"n":2}'); // replayed duplicate: dropped
    second.emit("kb.page_changed", "3", '{"n":3}');
    expect(seen).toEqual([1, 2, 3]);
    stop();
    expect(second.closed).toBe(true);
  });

  it("starts after a given id, ignores bad JSON, and stops retrying once closed", () => {
    vi.useFakeTimers();
    const seen: unknown[] = [];
    const stop = subscribe("/x", (e) => seen.push(e), { EventSource: Impl, after: 7 });
    const src = FakeSource.all[0] as FakeSource;
    expect(new URL(src.url).searchParams.get("after")).toBe("7");
    src.emit("message", "", "not json");
    src.emit("message", "", '{"ok":true}');
    expect(seen).toEqual([{ id: 0, data: { ok: true }, topic: "message" }]);
    stop();
    src.onerror?.();
    vi.advanceTimersByTime(5000);
    expect(FakeSource.all).toHaveLength(1);
  });

  it("uses the global EventSource by default", () => {
    const original = globalThis.EventSource;
    globalThis.EventSource = Impl;
    try {
      const stop = subscribe("/y", () => {});
      expect(FakeSource.all).toHaveLength(1);
      stop();
    } finally {
      globalThis.EventSource = original;
    }
  });
});
