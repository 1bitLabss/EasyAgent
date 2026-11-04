import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApp } from "@/store";
import { SearchScreen } from "./Search";

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

vi.mock("@/api", () => ({
  queryClient: new QueryClient(),
  api: vi.fn(async (path: string) => {
    if (path.includes("q=harbor")) {
      return {
        results: [
          {
            bot_id: "b1",
            bot_name: "Ada",
            chat_id: "c1",
            chat_title: "Notes",
            message_id: "m9",
            index: 3,
            role: "user",
            text: "We sail at harbor dawn.",
            created_at: "2026-11-01T10:00:00Z",
            score: 4.2,
          },
        ],
      };
    }
    return { results: [] };
  }),
}));

function renderScreen() {
  render(
    <QueryClientProvider client={client}>
      <SearchScreen />
    </QueryClientProvider>,
  );
}

describe("chat search screen", () => {
  beforeEach(() => {
    client.clear();
    useApp.setState({ botId: null, chatId: null, screen: "search", jumpTo: "" });
  });

  afterEach(() => {
    cleanup();
    client.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat", jumpTo: "" });
  });

  it("waits for two letters before searching", async () => {
    renderScreen();
    expect(screen.getByText("Type two or more letters.")).toBeTruthy();
    const { api } = await import("@/api");
    expect((api as ReturnType<typeof vi.fn>).mock.calls.some(([path]) => String(path).includes("chat-search"))).toBe(false);
  });

  it("lists matches for a typed query", async () => {
    renderScreen();
    fireEvent.change(screen.getByTestId("chat-search-input"), { target: { value: "harbor" } });
    expect(await screen.findByText("We sail at harbor dawn.")).toBeTruthy();
    expect(screen.getByText("Ada · Notes")).toBeTruthy();
  });

  it("opens the chat at the matched message", async () => {
    renderScreen();
    fireEvent.change(screen.getByTestId("chat-search-input"), { target: { value: "harbor" } });
    fireEvent.click(await screen.findByTestId("chat-search-row"));
    const state = useApp.getState();
    expect(state.botId).toBe("b1");
    expect(state.chatId).toBe("c1");
    expect(state.screen).toBe("chat");
    expect(state.jumpTo).toBe("m9");
  });

  it("says when nothing matched", async () => {
    renderScreen();
    fireEvent.change(screen.getByTestId("chat-search-input"), { target: { value: "zzz" } });
    expect(await screen.findByText("Nothing matched.")).toBeTruthy();
  });
});
