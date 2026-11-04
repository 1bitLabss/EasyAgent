import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { useApp } from "@/store";

vi.mock("@/api", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/rooms") return [{ id: "room-1", name: "Desk" }];
    if (path === "/api/bots") return [];
    return {};
  }),
  queryClient: new QueryClient(),
}));

afterEach(() => {
  cleanup();
  useApp.setState({ roomId: null, screen: "chat", botId: null });
});

function wrap(client: QueryClient) {
  return <QueryClientProvider client={client}><Sidebar /></QueryClientProvider>;
}

describe("room rail unread", () => {
  it("shows an unread dot and count on a room with unread replies", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["unread"], { total: 2, chats: [], rooms: [{ room_id: "room-1", unread: 2 }], busy: [] });
    render(wrap(client));
    const rail = await screen.findByTestId("room-rail");
    expect(screen.getByTestId("room-unread-dot")).toBeTruthy();
    expect(rail.getAttribute("aria-label")).toBe("Desk, 2 unread");
  });

  it("shows no dot and opens the live room pane on click", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["unread"], { total: 0, chats: [], rooms: [], busy: [] });
    render(wrap(client));
    const rail = await screen.findByTestId("room-rail");
    expect(screen.queryByTestId("room-unread-dot")).toBeNull();
    expect(rail.getAttribute("aria-label")).toBe("Desk");
    fireEvent.click(rail);
    expect(useApp.getState().screen).toBe("room");
    expect(useApp.getState().roomId).toBe("room-1");
  });
});
