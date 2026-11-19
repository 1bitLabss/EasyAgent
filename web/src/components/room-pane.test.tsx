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
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/rooms/room-1") return ROOM;
    if (path === "/api/bots") return [{ id: "a", name: "Alpha", face_color: "#f00" }, { id: "b", name: "Bravo", face_color: "#0f0" }];
    return {};
  });
  vi.unstubAllGlobals();
});

function stubRoom(view: unknown) {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === "/api/rooms/room-1") return view;
    if (path === "/api/bots") return [{ id: "a", name: "Alpha", face_color: "#f00" }, { id: "b", name: "Bravo", face_color: "#0f0" }];
    return {};
  });
}

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

describe("RoomPane working indicator", () => {
  it("draws an animated row for every queued or thinking bot", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([
      {
        type: "sync",
        room: ROOM,
        statuses: {
          a: { name: "Alpha", state: "thinking", label: "Alpha is thinking…" },
          b: { name: "Bravo", state: "queued", label: "Bravo queued" },
        },
      },
    ])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const rows = await screen.findAllByTestId("room-bot-status");
    expect(rows).toHaveLength(2);
    const states = rows.map((row) => row.getAttribute("data-state"));
    expect(states).toContain("thinking");
    expect(states).toContain("queued");
    expect(rows[0].textContent).toContain("Alpha");
    expect(rows[1].textContent).toContain("Bravo");
    // The motion lives in CSS (.run-step shimmer + .run-dots ellipsis); the
    // rows must carry both hooks or the pane is back to static text.
    expect(screen.getAllByTestId("room-working-dots")).toHaveLength(2);
    for (const row of rows) expect(row.querySelector(".run-step")).not.toBeNull();
  });

  it("animates the waiting-approval state too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([
      { type: "sync", room: ROOM, statuses: { a: { name: "Alpha", state: "waiting_approval", label: "Alpha waiting for approval" } } },
    ])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const row = await screen.findByTestId("room-bot-status");
    expect(row.getAttribute("data-state")).toBe("waiting_approval");
    expect(screen.getByTestId("room-working-dots")).toBeTruthy();
  });

  it("replaces the working row with the bot's answer when it arrives", async () => {
    const answered = {
      ...ROOM,
      messages: [...ROOM.messages, { id: "new-1", role: "assistant", speaker: "a", speaker_name: "Alpha", content: "Here is the answer." }],
    };
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([
      { type: "sync", room: ROOM, statuses: { a: { name: "Alpha", state: "thinking", label: "Alpha is thinking…" } } },
      { type: "message", message: { id: "new-1", role: "assistant", speaker: "a", speaker_name: "Alpha", content: "Here is the answer." } },
      { type: "done", room: answered },
    ])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    await screen.findByText("Here is the answer.");
    expect(screen.queryByTestId("room-bot-status")).toBeNull();
  });

  it("shows no working row for a finished bot", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([
      { type: "sync", room: ROOM, statuses: { a: { name: "Alpha", state: "done", label: "Alpha replied" } } },
    ])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const row = await screen.findByTestId("room-bot-status");
    expect(row.getAttribute("data-state")).toBe("done");
    expect(screen.queryByTestId("room-working-dots")).toBeNull();
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

describe("RoomPane live connection status", () => {
  it("shows a reconnecting line once the live socket drops", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(streamOf([{ type: "done", room: ROOM }]))
      .mockRejectedValue(new Error("socket died")));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const line = await screen.findByTestId("room-reconnecting");
    expect(line.getAttribute("role")).toBe("status");
    expect(line.textContent).toContain("Reconnecting");
  });
});

describe("RoomPane error rows", () => {
  it("renders a failed bot reply as an error row", async () => {
    const room = {
      ...ROOM,
      messages: [...ROOM.messages, { id: "e1", role: "assistant", speaker: "a", speaker_name: "Alpha", content: "Alpha could not answer.", error: true }],
    };
    stubRoom(room);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const row = await screen.findByTestId("room-error-row");
    expect(row.textContent).toBe("Alpha could not answer.");
    expect(row.className).toContain("text-danger");
    const plain = screen.getByText("hello");
    expect(plain.className).not.toContain("text-danger");
    expect(plain.getAttribute("data-testid")).toBeNull();
  });
});

describe("RoomPane ghost members", () => {
  it("marks a missing bot in the header without touching live members", async () => {
    const room = { ...ROOM, members: [{ id: "a", name: "Alpha" }, { id: "g", name: "Missing bot", missing: true }] };
    stubRoom(room);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([{ type: "done", room }])));
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    const chips = await screen.findAllByTestId("room-member-chip");
    const ghost = chips.find((chip) => chip.getAttribute("data-missing") === "true");
    expect(ghost?.textContent).toContain("Missing bot");
    expect(ghost?.className).toContain("text-danger");
    const alpha = chips.find((chip) => chip.textContent?.includes("Alpha"));
    expect(alpha).toBeTruthy();
    expect(alpha?.hasAttribute("data-missing")).toBe(false);
    expect(alpha?.className).not.toContain("text-danger");
  });
});
