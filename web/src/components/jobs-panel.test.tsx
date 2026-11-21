import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api";
import { JobsPanel } from "./JobsPanel";
import { RoomPane } from "./RoomPane";
import { useApp } from "@/store";

vi.mock("@/api", () => ({
  api: vi.fn(async (path: string) => {
    if (path === "/api/routines") return [];
    if (path === "/api/usage/today") return { date: "2026-11-07", prompt_tokens: 1234, completion_tokens: 340 };
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("jobs and rooms", () => {
  it("shows an empty jobs panel that names the open-app rule", async () => {
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect((await screen.findByTestId("jobs-empty")).textContent).toMatch(/only while EasyAgent is open/i);
  });

  it("rolls today's tokens into a small line in the footer", async () => {
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect((await screen.findByTestId("usage-today")).textContent).toBe("Today: 1.2k in / 340 out");
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

function stubViewport(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("jobs panel phone layout", () => {
  it("goes full width at a narrow viewport instead of the fixed 360px aside", async () => {
    stubViewport(true);
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    const panel = await screen.findByTestId("jobs-panel");
    expect(panel.className).toContain("w-full");
    expect(panel.className).not.toContain("w-[360px]");
  });

  it("keeps the fixed 360px aside on desktop viewports", async () => {
    stubViewport(false);
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    const panel = await screen.findByTestId("jobs-panel");
    expect(panel.className).toContain("w-[360px]");
    expect(panel.className).not.toContain("w-full");
  });
});

describe("jobs panel footer resume all", () => {
  afterEach(() => {
    vi.mocked(api).mockReset();
  });

  function stubRoutines(rows: unknown[]) {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/api/routines") return rows;
      if (path === "/api/usage/today") return { date: "2026-11-18", prompt_tokens: 0, completion_tokens: 0 };
      return [];
    });
  }

  it("shows Resume all when every routine is paused and clicking it posts resume-all", async () => {
    stubRoutines([
      { id: "r1", bot_id: "b1", bot_name: "Alpha", name: "Morning", state: "paused" },
      { id: "r2", bot_id: "b2", bot_name: "Bravo", name: "Nightly", state: "paused" },
    ]);
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    const button = await screen.findByTestId("resume-all");
    expect(button.textContent).toBe("Resume all");
    expect(screen.queryByText("Pause all")).toBeNull();
    fireEvent.click(button);
    expect(vi.mocked(api)).toHaveBeenCalledWith("/api/routines/resume-all", { method: "POST" });
  });

  it("keeps Pause all when any routine is not paused", async () => {
    stubRoutines([
      { id: "r1", bot_id: "b1", bot_name: "Alpha", name: "Morning", state: "paused" },
      { id: "r2", bot_id: "b2", bot_name: "Bravo", name: "Nightly", state: "ok" },
    ]);
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect(await screen.findByText("Pause all")).toBeTruthy();
    expect(screen.queryByTestId("resume-all")).toBeNull();
  });
});

describe("jobs panel per-bot usage lines", () => {
  afterEach(() => {
    vi.mocked(api).mockReset();
  });

  it("renders one compact line per bot when the payload carries a bots map", async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/api/routines") return [];
      if (path === "/api/bots") {
        return [
          { id: "b1", name: "Alpha" },
          { id: "b2", name: "Bravo" },
        ];
      }
      if (path === "/api/usage/today") {
        return {
          date: "2026-11-21",
          prompt_tokens: 1534,
          completion_tokens: 343,
          bots: {
            b1: { prompt_tokens: 1200, completion_tokens: 300 },
            b2: { prompt_tokens: 334, completion_tokens: 43 },
          },
        };
      }
      return [];
    });
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    const lines = await screen.findAllByTestId("usage-bot");
    expect(lines.map((node) => node.textContent)).toEqual([
      "Alpha: 1.2k in / 300 out",
      "Bravo: 334 in / 43 out",
    ]);
    // The rollup line keeps its exact old shape.
    expect((await screen.findByTestId("usage-today")).textContent).toBe("Today: 1.5k in / 343 out");
  });

  it("falls back to the bot id when the bot list does not know it", async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/api/routines") return [];
      if (path === "/api/bots") return [];
      if (path === "/api/usage/today") {
        return {
          date: "2026-11-21",
          prompt_tokens: 5,
          completion_tokens: 2,
          bots: { ghost: { prompt_tokens: 5, completion_tokens: 2 } },
        };
      }
      return [];
    });
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    const lines = await screen.findAllByTestId("usage-bot");
    expect(lines.map((node) => node.textContent)).toEqual(["ghost: 5 in / 2 out"]);
  });

  it("shows no per-bot lines when the payload has no bots map (older server)", async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/api/routines") return [];
      if (path === "/api/usage/today") {
        return { date: "2026-11-21", prompt_tokens: 10, completion_tokens: 2 };
      }
      return [];
    });
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect(await screen.findByTestId("usage-today")).toBeTruthy();
    expect(screen.queryAllByTestId("usage-bot")).toHaveLength(0);
  });

  it("shows no per-bot lines when the bots map is empty", async () => {
    vi.mocked(api).mockImplementation(async (path: string) => {
      if (path === "/api/routines") return [];
      if (path === "/api/usage/today") {
        return { date: "2026-11-21", prompt_tokens: 10, completion_tokens: 2, bots: {} };
      }
      return [];
    });
    useApp.setState({ jobsOpen: true, highlightJob: "" });
    render(wrap(<JobsPanel />));
    expect(await screen.findByTestId("usage-today")).toBeTruthy();
    expect(screen.queryAllByTestId("usage-bot")).toHaveLength(0);
  });
});
