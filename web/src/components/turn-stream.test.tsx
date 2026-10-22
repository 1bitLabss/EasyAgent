import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { pushEventForTests, resetStreamForTests, setFlightForTests } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async () => chat([asked])),
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

const asked: ChatMessage = {
  id: "u1",
  role: "user",
  content: "Test memory, notes, and a script.",
  created_at: "2026-10-22T18:00:00Z",
};

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
      last_activity_at: "2026-10-22T18:02:07Z",
      current_step: "",
      reason: "",
    },
  };
}

const FIRST = "Let me do these in parallel where possible.";
const SECOND = "\n\nuser asked to test memory and run the script.";
const THOUGHT = `${FIRST}${SECOND}`;
const ANSWER = "Everything's operational. What's next?\n\nThe file is test_coding.py in the workspace.";

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

describe("one turn of reasoning, a tool, and the answer", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("keeps each reasoning segment once, ends the turn, and stays pinned to the bottom", async () => {
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
    queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([asked]));
    queryClient.setQueryData(["approvals", "bot-1"], []);
    useApp.getState().openBot("bot-1", "chat-1");
    paint();

    act(() => {
      setFlightForTests("bot-1", "chat-1", {
        phase: "thinking",
        label: "Waiting on model",
        text: "",
        reasoning: "",
        run: null,
        heardAt: Date.now(),
        replyId: "reply-1",
      }, true);
    });
    expect(screen.queryByTestId("jump-latest")).toBeNull();

    act(() => {
      pushEventForTests("bot-1", "chat-1", { type: "thinking", text: FIRST, offset: 0 });
      pushEventForTests("bot-1", "chat-1", { type: "thinking", text: FIRST, offset: 0 });
    });
    expect(screen.getByTestId("thinking-body").textContent).toBe(FIRST);
    expect(screen.queryByTestId("jump-latest")).toBeNull();

    act(() => {
      pushEventForTests("bot-1", "chat-1", { type: "status", text: "Running python", step_id: "py", state: "running" });
      pushEventForTests("bot-1", "chat-1", { type: "status", text: "Running python", step_id: "py", state: "done" });
      pushEventForTests("bot-1", "chat-1", { type: "thinking", text: SECOND, offset: FIRST.length });
      pushEventForTests("bot-1", "chat-1", { type: "thinking", text: SECOND, offset: FIRST.length });
      pushEventForTests("bot-1", "chat-1", { type: "thinking", text: "\nEverything", offset: THOUGHT.length });
    });
    expect(screen.getByTestId("thinking-body").textContent).toContain("Everything");
    expect(screen.queryByTestId("jump-latest")).toBeNull();

    act(() => {
      pushEventForTests("bot-1", "chat-1", { type: "delta", text: "Everything's operational. What's next?" });
      pushEventForTests("bot-1", "chat-1", { type: "delta", text: "\n\nThe file is test_coding.py in the workspace." });
      pushEventForTests("bot-1", "chat-1", { type: "delta", text: "\n\nThe file is test_coding.py in the workspace." });
    });

    expect(screen.queryByTestId("thinking-body")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Thinking/ }));
    expect(screen.getByTestId("thinking-body").textContent).toBe(THOUGHT);
    expect(screen.getByTestId("thinking-body").textContent).not.toContain("Everything");
    const bubble = document.querySelector(".bubble-bot")?.textContent || "";
    expect(bubble).toContain("Everything's operational. What's next?");
    expect(bubble.split("Everything's operational").length - 1).toBe(1);
    expect(bubble.split("The file is test_coding.py").length - 1).toBe(1);
    expect(screen.getAllByTestId("live-status")).toHaveLength(1);
    expect(screen.getByTestId("live-status").getAttribute("data-live")).toBe("true");
    expect(screen.getByTestId("run-state").textContent).toMatch(/Writing/);
    expect(screen.queryByTestId("jump-latest")).toBeNull();

    const saved = chat([
      asked,
      {
        id: "reply-1",
        client_id: "live-reply-1",
        role: "assistant",
        content: ANSWER,
        thinking: THOUGHT,
        created_at: "2026-10-22T18:02:07Z",
      },
    ]);
    act(() => {
      pushEventForTests("bot-1", "chat-1", { type: "done", chat: saved });
    });
    await waitFor(() => {
      expect(screen.getByTestId("run-state").textContent).toBe("Idle");
      expect(screen.getByTestId("live-status").getAttribute("data-live")).toBe("false");
      expect(document.querySelectorAll('[data-live="true"]')).toHaveLength(0);
      expect(document.querySelectorAll("[data-testid=activity-step][data-state=running]")).toHaveLength(0);
    }, { timeout: 1000 });
    act(() => {
      pushEventForTests("bot-1", "chat-1", { type: "status", text: "Writing", step_id: "again", state: "running" });
    });
    expect(document.querySelectorAll('[data-live="true"]')).toHaveLength(0);
    expect(document.querySelectorAll("[data-testid=activity-step][data-state=running]")).toHaveLength(0);
    expect(screen.getByTestId("run-state").textContent).toBe("Idle");
    expect(screen.queryByTestId("jump-latest")).toBeNull();

    const scroller = document.querySelector(".chat-scroll") as HTMLElement;
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 900 });
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: 200 });
    scroller.scrollTop = 0;
    fireEvent.wheel(scroller, { deltaY: -40 });
    fireEvent.scroll(scroller);
    expect(screen.getByTestId("jump-latest")).toBeTruthy();
  });
});
