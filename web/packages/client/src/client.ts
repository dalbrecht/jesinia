// SPDX-License-Identifier: AGPL-3.0-or-later
import createFetchClient, { type Client } from "openapi-fetch";
import type { paths } from "./openapi";

export type JesiniaClient = Client<paths>;

/** Dispatched on `window` when the server answers 401 (the browser session ended). */
export const UNAUTHORIZED_EVENT = "jesinia:unauthorized";

export type ClientOptions = { baseUrl?: string; fetch?: (input: Request) => Promise<Response> };

/** A typed client for the Jesinia API. Auth is the HttpOnly session cookie. */
export function createClient(opts: ClientOptions = {}): JesiniaClient {
  const baseFetch = opts.fetch ?? ((input: Request) => globalThis.fetch(input));
  const client = createFetchClient<paths>({
    baseUrl: opts.baseUrl ?? globalThis.location?.origin ?? "",
    credentials: "same-origin",
    fetch: baseFetch,
  });
  client.use({
    onResponse({ response }) {
      if (response.status === 401) globalThis.dispatchEvent?.(new CustomEvent(UNAUTHORIZED_EVENT));
      return response;
    },
  });
  return client;
}

/** The header the server's write routes require (one key per user action). */
export const IDEMPOTENCY_HEADER = "Idempotency-Key";

export type IdempotencyHeaders = { "Idempotency-Key": string };

/**
 * Headers for a non-idempotent request. Pass the key of an earlier attempt to retry that same
 * action (the server answers a repeated key with the first response instead of acting twice);
 * omit it for a new action.
 *
 * @example client.POST("/api/kb/tags/retire", { body, params: { header: withIdempotency() } })
 */
export function withIdempotency(key: string = crypto.randomUUID()): IdempotencyHeaders {
  return { [IDEMPOTENCY_HEADER]: key };
}

/** The server's error detail (`{"detail": …}`), or a fallback message. */
export function errorText(error: unknown, fallback = "Something went wrong."): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
    }
  }
  return fallback;
}
