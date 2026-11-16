// The offline shell fallback in easyagent/static/sw.js (FIX: /?pair= dead offline).
//
// sw.js is a plain service-worker script, so the test loads its source and runs it
// against a fake `self`, then drives the registered fetch handler directly.

import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

type SwEvent = {
  request: { url: string; method: string; mode: string };
  respondWith: (response: Promise<unknown>) => void;
};

function loadServiceWorker(): (event: SwEvent) => void {
  const source = readFileSync(resolve("../easyagent/static/sw.js"), "utf8");
  const listeners: Record<string, (event: SwEvent) => void> = {};
  const selfStub = {
    location: { href: "http://localhost/sw.js", origin: "http://localhost" },
    addEventListener: (type: string, handler: (event: SwEvent) => void) => {
      listeners[type] = handler;
    },
  };
  new Function("self", source)(selfStub);
  if (!listeners.fetch) throw new Error("sw.js never registered a fetch handler");
  return listeners.fetch;
}

const shellResponse = { ok: true, label: "cached-shell" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("offline navigation fallback", () => {
  it("serves the cached / shell when a navigation to /?pair=<token> fails", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    const match = vi.fn(async (request: unknown) => (request === "/" ? shellResponse : undefined));
    vi.stubGlobal("caches", { match });
    const onFetch = loadServiceWorker();

    let captured: Promise<unknown> | null = null;
    onFetch({
      request: { url: "http://localhost/?pair=tok123", method: "GET", mode: "navigate" },
      respondWith: (response) => {
        captured = response;
      },
    });
    expect(captured).not.toBeNull();
    await expect(captured).resolves.toBe(shellResponse);
    expect(match).toHaveBeenCalledWith("/");
  });

  it("still matches the exact request for non-navigation GETs", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    const request = { url: "http://localhost/static/face.svg", method: "GET", mode: "cors" };
    const match = vi.fn(async (incoming: unknown) => (incoming === request ? shellResponse : undefined));
    vi.stubGlobal("caches", { match });
    const onFetch = loadServiceWorker();

    let captured: Promise<unknown> | null = null;
    onFetch({
      request,
      respondWith: (response) => {
        captured = response;
      },
    });
    await expect(captured).resolves.toBe(shellResponse);
    expect(match).toHaveBeenCalledWith(request);
  });
});
