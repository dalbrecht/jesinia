// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from "vitest";
import { createClient, errorText, UNAUTHORIZED_EVENT } from "../client";

describe("createClient", () => {
  it("sends cookies, types responses, and announces 401", async () => {
    const fetch = vi.fn(async (req: Request) => {
      expect(req.credentials).toBe("same-origin");
      if (req.url.endsWith("/healthz")) return Response.json({ ok: true, modules: {} });
      return Response.json({ detail: "authentication required" }, { status: 401 });
    });
    const client = createClient({ baseUrl: "http://127.0.0.1:5374", fetch });
    const health = await client.GET("/healthz");
    expect(health.data).toEqual({ ok: true, modules: {} });
    const signedOut = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, signedOut);
    const r = await client.GET("/api/kb/conflicts");
    expect(r.response.status).toBe(401);
    expect(errorText(r.error)).toBe("authentication required");
    expect(signedOut).toHaveBeenCalledOnce();
  });

  it("defaults to the page origin and the global fetch", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ ok: true, modules: {} }));
    const client = createClient();
    await client.GET("/healthz");
    expect((spy.mock.calls[0]?.[0] as Request).url).toBe("http://localhost:3000/healthz");
    spy.mockRestore();
  });

  it("formats validation errors and falls back", () => {
    expect(errorText({ detail: [{ msg: "field required" }, "x"] })).toBe("field required; x");
    expect(errorText(null)).toBe("Something went wrong.");
    expect(errorText({ detail: 3 }, "fallback")).toBe("fallback");
  });
});
