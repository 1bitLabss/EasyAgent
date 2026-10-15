import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useApp } from "@/store";
import type { Bot, Chat } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "ada";
const CHAT_ID = "ada-chat";

vi.mock("@/api", async () => {
  const { QueryClient: Client } = await import("@tanstack/react-query");
  const client = new Client({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string, options?: { json?: { on?: boolean } }) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/yolo")) {
        return { ...chat(), yolo: { on: Boolean(options?.json?.on), remaining_seconds: options?.json?.on ? 7200 : 0 } };
      }
      if (path.includes(`/chats/${CHAT_ID}`)) return chat();
      return [];
    }),
  };
});

function bot(): Bot {
  return {
    id: BOT_ID,
    name: "Ada",
    endpoint_id: "local",
    endpoint_name: "local",
    endpoint_base_url: null,
    model: null,
    context_tokens: 8192,
    face_color: "#6b8f71",
    face_color_set: true,
    check_enabled: false,
    learn_paused: true,
    learn_manual: true,
  };
}

function chat(): Chat {
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Ada",
    messages: [],
    message_count: 0,
    window_start: 0,
    run: {
      id: "run-idle",
      status: "idle",
      started_at: null,
      last_activity_at: null,
      current_step: "",
      reason: "",
    },
    yolo: { on: true, remaining_seconds: 3600 },
  };
}

afterEach(() => {
  cleanup();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("YOLO banner", () => {
  it("stays on screen and turns YOLO off in one click", async () => {
    const { queryClient, api } = await import("@/api");
    (queryClient as QueryClient).clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], chat());
    queryClient.setQueryData(["approvals", BOT_ID], []);

    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );

    const banner = await screen.findByTestId("yolo-banner");
    expect(banner.textContent).toContain("YOLO ON");
    fireEvent.click(screen.getByTestId("yolo-off"));
    expect(api).toHaveBeenCalledWith(`/api/bots/${BOT_ID}/chats/${CHAT_ID}/yolo`, {
      method: "POST",
      json: { on: false },
    });
  });
});
