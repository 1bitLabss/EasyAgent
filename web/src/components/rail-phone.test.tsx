import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { useApp } from "@/store";

vi.mock("@/api", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/bots") return [];
    if (path === "/api/rooms") return [];
    return {};
  }),
  queryClient: new QueryClient(),
}));

afterEach(() => {
  cleanup();
  useApp.setState({ botId: null, chatId: null, screen: "chat", focus: "" });
});

describe("connect your phone rail entry", () => {
  it("shows a phone button in the rail tools", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Sidebar />
      </QueryClientProvider>,
    );
    const button = await screen.findByTestId("rail-phone");
    expect(button.getAttribute("aria-label")).toBe("Connect your phone");
  });

  it("opens the phone pairing screen on click", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Sidebar />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByTestId("rail-phone"));
    expect(useApp.getState().screen).toBe("settings");
    expect(useApp.getState().focus).toBe("phone");
  });
});
