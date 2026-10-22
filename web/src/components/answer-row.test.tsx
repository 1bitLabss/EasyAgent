import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

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

const PIER = "The east pier has twelve boats.";
const MARK = "The pier mark is set.";
const KILN = "The kiln note is the one that was written.";

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
      started_at: "2026-10-22T18:00:00Z",
      last_activity_at: "2026-10-22T18:00:53Z",
      current_step: "",
      reason: "",
      activity_started_at: "2026-10-22T18:00:00Z",
    },
  };
}

function user(text: string): ChatMessage {
  return { id: "u1", role: "user", content: text, created_at: "2026-10-22T18:00:00Z" };
}

function answer(text: string): ChatMessage {
  return { id: "a1", role: "assistant", content: text, created_at: "2026-10-22T18:00:33Z" };
}

function paint(saved: string) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([user("How many boats are at the east pier?"), answer(saved)]));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

describe("a finished turn has an answer row", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it.each([
    ["ask another bot", PIER],
    ["step cap", MARK],
    ["discarded revise", KILN],
  ])("shows the saved %s transcript in the row", (_label, saved) => {
    paint(saved);
    const rows = document.querySelectorAll("[data-role=assistant]");
    expect(rows.length).toBeGreaterThan(0);
    const bubble = Array.from(rows).map((row) => row.textContent || "").join("\n");
    expect(bubble).toContain(saved);
    expect(bubble).not.toContain("CHILD-ONLY");
    expect(document.body.textContent || "").not.toContain("Retry");
    expect(document.body.textContent || "").not.toContain("Continue");
  });
});
