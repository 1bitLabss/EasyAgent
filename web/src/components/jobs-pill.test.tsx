import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import type { Bot, Chat } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "pillbot";
const CHAT_ID = "pillbot-chat";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/messages?")) return { messages: [], start: 0, total: 0 };
      if (path.includes(`/chats/${CHAT_ID}`)) return chat();
      return [];
    }),
  };
});

function bot(): Bot {
  return {
    id: BOT_ID,
    name: "PillBot",
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
    title: "PillBot",
    messages: [],
    message_count: 0,
    window_start: 0,
    run: { id: "run-idle", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
  };
}

function renderPane() {
  return render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

describe("Jobs pill vs the open jobs panel", () => {
  beforeEach(() => {
    queryClient.clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], chat());
    queryClient.setQueryData(["approvals", BOT_ID], []);
  });

  afterEach(() => {
    cleanup();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat", jobsOpen: false });
  });

  it("shows the Jobs pill while the jobs panel is closed", () => {
    useApp.setState({ jobsOpen: false });
    renderPane();
    expect(screen.getByTestId("open-jobs")).toBeTruthy();
  });

  it("hides the Jobs pill while the jobs panel is open so its Close button stays tappable", () => {
    useApp.setState({ jobsOpen: true });
    renderPane();
    expect(screen.queryByTestId("open-jobs")).toBeNull();
  });
});
