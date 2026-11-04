import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api";
import { RoomPane } from "./RoomPane";
import { useApp } from "@/store";

const ROOM = {
  id: "room-1",
  name: "Desk",
  members: [{ id: "a", name: "Alpha" }],
  messages: [
    { id: "u", role: "user", content: "hi", speaker_name: "You" },
    { id: "m", role: "assistant", content: "hello", speaker: "a", speaker_name: "Alpha" },
  ],
  pending_approvals: [],
};

vi.mock("@/api", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/rooms/room-1") return ROOM;
    if (path === "/api/bots") return [{ id: "a", name: "Alpha", face_color: "#f00" }, { id: "b", name: "Bravo", face_color: "#0f0" }];
    return {};
  }),
  queryClient: new QueryClient(),
}));

function streamOf(events: unknown[]) {
  const text = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  return {
    ok: true,
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(text));
        controller.close();
      },
    }),
  };
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

afterEach(() => {
  cleanup();
  vi.mocked(api).mockClear();
  vi.unstubAllGlobals();
});

describe("RoomPane live reconnect", () => {
  it("restores 'is thinking' statuses from the sync event after a mid-turn reload", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([
      { type: "sync", room: ROOM, statuses: { a: { name: "Alpha", state: "thinking", label: "Alpha is thinking" } } },
    ])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const status = await screen.findByTestId("room-bot-status");
    expect(status.textContent).toBe("Alpha is thinking");
  });

  it("clears stale statuses when the live stream reports no run in flight", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room: ROOM }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    await screen.findByTestId("room-window");
    await waitFor(() => expect(screen.queryByTestId("room-bot-status")).toBeNull());
  });
});

describe("RoomPane read marker", () => {
  it("marks the room read while it is on screen", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room: ROOM }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    await waitFor(
      () => expect(vi.mocked(api)).toHaveBeenCalledWith("/api/rooms/room-1/read", expect.objectContaining({ method: "POST", json: { through: 2 } })),
      { timeout: 2000 },
    );
  });
});

describe("RoomPane members", () => {
  it("removes a member from the room header", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room: ROOM }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    await screen.findByTestId("room-window");
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith("/api/rooms/room-1/bots/a", expect.objectContaining({ method: "DELETE" })));
  });

  it("adds a bot to the room from the header", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room: ROOM }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    await screen.findByTestId("room-window");
    fireEvent.change(screen.getByDisplayValue("Add a bot"), { target: { value: "b" } });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith("/api/rooms/room-1/bots", expect.objectContaining({ method: "POST", json: { bot_id: "b" } })));
  });
});
