import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient, streamPost } from "@/api";
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
      return chat(messages);
    }),
    reportClientEvent: vi.fn(async () => undefined),
    streamPost: vi.fn(async () => ({
      headers: { get: () => "application/json" },
      json: async () => ({ reply: "", chat: chat(messages) }),
    })),
  };
});

const QUESTIONS = [
  { question: "Which bakery is the page for?", choices: ["Oven Street", "Flour Power"] },
  { question: "Which day should it open?", choices: ["Friday", "Saturday"] },
];

let messages: ChatMessage[] = [];

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

function chat(msgs: ChatMessage[]): Chat {
  return {
    id: "chat-1",
    bot_id: "bot-1",
    title: "Ada",
    messages: msgs,
    message_count: msgs.length,
    window_start: 0,
    run: {
      id: "run-1",
      status: "idle",
      started_at: null,
      last_activity_at: null,
      current_step: "",
      reason: "",
    },
  };
}

function paint(msgs: ChatMessage[]) {
  messages = msgs;
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat(msgs));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
  vi.mocked(streamPost).mockClear();
  vi.mocked(api).mockClear();
});

describe("a batch of questions in one stop", () => {
  it("renders each question with its own choice chips", () => {
    paint([
      { id: "u1", role: "user", content: "set up the page", created_at: "" },
      {
        id: "a1",
        role: "assistant",
        content: QUESTIONS[0].question,
        choices: QUESTIONS[0].choices,
        questions: QUESTIONS,
        created_at: "",
      },
    ]);
    const card = document.querySelector("[data-testid=question-card]");
    expect(card).toBeTruthy();
    expect(card?.textContent).toContain("Which bakery is the page for?");
    expect(card?.textContent).toContain("Which day should it open?");
    expect(card?.querySelectorAll("[data-testid=question-choice-0]").length).toBe(2);
    expect(card?.querySelectorAll("[data-testid=question-choice-1]").length).toBe(2);
    // The batch card replaces the single-question chip row for this message.
    const bubble = document.querySelector(".bubble-bot");
    expect(bubble?.querySelectorAll("button")).toHaveLength(4);
  });

  it("sends one combined message once every question has an answer", async () => {
    paint([
      {
        id: "a1",
        role: "assistant",
        content: QUESTIONS[0].question,
        choices: QUESTIONS[0].choices,
        questions: QUESTIONS,
        created_at: "",
      },
    ]);
    fireEvent.click(document.querySelector("[data-testid=question-choice-0]")!);
    // Nothing is sent while a question is still open.
    expect(vi.mocked(streamPost)).not.toHaveBeenCalled();
    expect(document.querySelector('[data-testid=question-choice-0][aria-pressed=true]')).toBeTruthy();
    fireEvent.click(document.querySelectorAll("[data-testid=question-choice-1]")[1]!);
    await waitFor(() => expect(vi.mocked(streamPost)).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(streamPost).mock.calls[0];
    expect((body as { content: string }).content).toBe("Which bakery is the page for?\nOven Street\n\nWhich day should it open?\nSaturday");
  });

  it("keeps the first pick when a second choice is tapped for the same question", async () => {
    paint([
      {
        id: "a1",
        role: "assistant",
        content: QUESTIONS[0].question,
        choices: QUESTIONS[0].choices,
        questions: QUESTIONS,
        created_at: "",
      },
    ]);
    fireEvent.click(document.querySelectorAll("[data-testid=question-choice-0]")[1]!);
    fireEvent.click(document.querySelectorAll("[data-testid=question-choice-0]")[0]!);
    fireEvent.click(document.querySelector("[data-testid=question-choice-1]")!);
    await waitFor(() => expect(vi.mocked(streamPost)).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(streamPost).mock.calls[0];
    expect((body as { content: string }).content).toBe("Which bakery is the page for?\nFlour Power\n\nWhich day should it open?\nFriday");
  });

  it("leaves the single-question chip row exactly as it was", async () => {
    paint([
      {
        id: "a1",
        role: "assistant",
        content: "Which folder?",
        choices: ["src", "tests"],
        created_at: "",
      },
    ]);
    expect(document.querySelector("[data-testid=question-card]")).toBeNull();
    const chip = Array.from(document.querySelectorAll(".bubble-bot button")).find((b) => b.textContent === "tests")!;
    fireEvent.click(chip);
    await waitFor(() => expect(vi.mocked(streamPost)).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(streamPost).mock.calls[0];
    expect((body as { content: string }).content).toBe("tests");
  });
});
