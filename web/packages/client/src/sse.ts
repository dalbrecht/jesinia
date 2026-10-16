// SPDX-License-Identifier: AGPL-3.0-or-later
export type SseEvent<T> = { id: number; data: T; topic: string };
export type SubscribeOptions = {
  after?: number;
  /** Event names to listen for (SSE `event:` field). Defaults to unnamed messages. */
  events?: string[];
  retryMs?: number;
  /** For tests: the EventSource implementation. */
  EventSource?: typeof EventSource;
};

/**
 * An EventSource wrapper that survives disconnects: on error it reconnects with
 * `after=<last id>`, so nothing is missed or repeated. Returns an unsubscribe function.
 */
export function subscribe<T>(url: string, onEvent: (e: SseEvent<T>) => void, opts: SubscribeOptions = {}): () => void {
  const Impl = opts.EventSource ?? globalThis.EventSource;
  const retryMs = opts.retryMs ?? 2000;
  let last = opts.after ?? 0;
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const handle = (msg: MessageEvent<string>) => {
    const id = Number(msg.lastEventId);
    if (Number.isFinite(id) && id > 0) {
      if (id <= last) return; // already delivered before a reconnect
      last = id;
    }
    let data: T;
    try {
      data = JSON.parse(msg.data) as T;
    } catch {
      return;
    }
    onEvent({ id, data, topic: msg.type });
  };

  const connect = () => {
    const u = new URL(url, globalThis.location?.origin ?? "http://localhost");
    if (last > 0) u.searchParams.set("after", String(last));
    source = new Impl(u.toString(), { withCredentials: true });
    for (const name of opts.events ?? ["message"]) source.addEventListener(name, handle as EventListener);
    source.onerror = () => {
      source?.close();
      source = null;
      if (!closed) timer = setTimeout(connect, retryMs);
    };
  };

  connect();
  return () => {
    closed = true;
    if (timer) clearTimeout(timer);
    source?.close();
  };
}
