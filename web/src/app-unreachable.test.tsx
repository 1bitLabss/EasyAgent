// FIX: with every request failing, the app sat on "Loading bots…" / a raw error
// string. It must show a plain-language unreachable state with a retry.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { queryClient } from "@/api";

vi.mock("@/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@/api");
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    ...actual,
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") throw new Error("Failed to fetch");
      if (path === "/api/token/status") return { required: false, configured: true };
      if (path === "/api/setup/status") return { needs_setup: false };
      if (path.startsWith("/api/pair/invites")) return { invites: [] };
      return [];
    }),
  };
});

beforeEach(() => {
  queryClient.clear();
});

afterEach(() => {
  cleanup();
  queryClient.clear();
});

describe("unreachable server state", () => {
  it("shows a friendly message and a Try again button instead of Loading bots…", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    );
    const panel = await screen.findByTestId("unreachable");
    expect(panel.textContent).toContain("Can't reach EasyAgent");
    expect(screen.getByText("Try again")).toBeTruthy();
    expect(screen.queryByText("Loading bots…")).toBeNull();
    expect(screen.queryByText("Failed to fetch")).toBeNull();
  });

  it("Try again refetches the bot list", async () => {
    const { api } = await import("@/api");
    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    );
    await screen.findByTestId("unreachable");
    (api as ReturnType<typeof vi.fn>).mockClear();
    (api as ReturnType<typeof vi.fn>).mockImplementation(async (path: string) => {
      if (path === "/api/bots") throw new Error("still down");
      if (path === "/api/token/status") return { required: false, configured: true };
      if (path === "/api/setup/status") return { needs_setup: false };
      return [];
    });
    screen.getByText("Try again").click();
    await waitFor(() => {
      expect(api).toHaveBeenCalledWith("/api/bots");
    });
  });
});
