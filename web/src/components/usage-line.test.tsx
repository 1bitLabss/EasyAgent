import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane, usageNote } from "./ChatPane";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: "chat-1" };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      return chat([]);
    }),
  };
});

function bot(): Bot {
  return {
    id: "bot-1",
    name: "Ada",
    endpoint_id: "cedar",
    endpoint_name: "Cedar",
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

function chat(messages: ChatMessage[]): Chat {
  return {
    id: "chat-1",
    bot_id: "bot-1",
    title: "Ada",
    messages,
    message_count: messages.length,
    window_start: 0,
    run: {
      id: "run-1",
      status: "idle",
      started_at: "2026-11-07T18:00:00Z",
      last_activity_at: "2026-11-07T18:00:53Z",
      current_step: "",
      reason: "",
      activity_started_at: "2026-11-07T18:00:00Z",
    },
  };
}

function paint(messages: ChatMessage[]) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat(messages));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

const ask: ChatMessage = { id: "u1", role: "user", content: "how many boats", created_at: "2026-11-07T18:00:00Z" };

function answer(extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id: "a1", role: "assistant", content: "Twelve boats.", created_at: "2026-11-07T18:00:12Z", ...extra };
}

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("the quiet usage footer", () => {
  it("shows time and tokens when the server reported both", () => {
    paint([ask, answer({ elapsed_ms: 12000, prompt_tokens: 1234, completion_tokens: 340 })]);
    expect(screen.getByTestId("usage-line").textContent).toBe("12s · 1.2k in / 340 out");
  });

  it("shows time alone when the server gave no usage", () => {
    paint([ask, answer({ elapsed_ms: 12000 })]);
    expect(screen.getByTestId("usage-line").textContent).toBe("12s");
  });

  it("shows nothing when nothing is known", () => {
    paint([ask, answer()]);
    expect(screen.queryByTestId("usage-line")).toBeNull();
  });

  it("stays off a live row that has not finished", () => {
    paint([ask, answer({ elapsed_ms: 12000, prompt_tokens: 1234, completion_tokens: 340, live: true })]);
    expect(screen.queryByTestId("usage-line")).toBeNull();
  });

  it("formats counts short and joins the parts with a dot", () => {
    expect(usageNote({ role: "assistant", content: "", elapsed_ms: 65000, prompt_tokens: 12000, completion_tokens: 999 })).toBe(
      "1m 5s · 12k in / 999 out",
    );
    expect(usageNote({ role: "assistant", content: "" })).toBe("");
  });
});
