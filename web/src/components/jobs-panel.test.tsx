import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { JobsPanel } from "./JobsPanel";
import { RoomPane } from "./RoomPane";
import { useApp } from "@/store";

vi.mock("@/api", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/routines") return [];
    if (path.startsWith("/api/rooms/")) {
      return {
        id: "room-1",
        name: "Picnic",
        members: [
          { id: "a", name: "Alpha" },
          { id: "b", name: "Bravo" },
        ],
        messages: [
          { id: "u", role: "user", content: "picnic plan", speaker_name: "You" },
          { id: "m", role: "assistant", content: "I'll bring bread.", speaker: "a", speaker_name: "Alpha" },
        ],
        pending_approvals: [],
      };
    }
    return [];
  }),
  queryClient: new QueryClient(),
}));

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

describe("jobs and rooms", () => {
  it("shows an empty jobs panel that names the open-app rule", async () => {
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect((await screen.findByTestId("jobs-empty")).textContent).toMatch(/only while EasyAgent is open/i);
  });

  it("opens a room as a full window with member chips and a speaker", async () => {
    useApp.setState({ roomId: "room-1", screen: "room" });
    render(wrap(<RoomPane />));
    expect(await screen.findByTestId("room-window")).toBeTruthy();
    expect(screen.getAllByTestId("room-member-chip").map((node) => node.textContent)).toEqual(["Alpha", "Bravo"]);
    expect(screen.getByTestId("room-speaker").textContent).toBe("Alpha");
    expect(document.querySelector("#draft")).toBeNull();
  });
});
