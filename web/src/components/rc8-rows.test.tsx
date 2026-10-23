import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { noteActivity } from "@/lib/activity";
import { useApp } from "@/store";
import { PAUSE_TEXT, PAUSE_TOOL_TEXT, pushEventForTests, resetStreamForTests, sendMessage, setFlightForTests, type Live } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

const { streamPost } = vi.hoisted(() => ({ streamPost: vi.fn() }));

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
    streamPost,
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

function chat(messages: ChatMessage[], status: "idle" | "running" | "paused" = "running"): Chat {
  return {
    id: "chat-1",
    bot_id: "bot-1",
    title: "Ada",
    messages,
    message_count: messages.length,
    window_start: 0,
    run: {
      id: "run-1",
      status,
      started_at: "2026-10-22T18:00:00Z",
      last_activity_at: "2026-10-22T18:00:33Z",
      current_step: status === "running" ? "Running sleep 4; echo first-check" : "",
      reason: "",
      reply_id: "a-part",
    },
  };
}

function paint(saved: Chat) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], saved);
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

function screenRows(): string[] {
  return Array.from(document.querySelectorAll(".message-row")).map((row) => {
    const note = row.querySelector(
      "[data-testid=status-note], [data-testid=pickup-card], [data-testid=message-text], [data-testid=interrupted-resuming]",
    );
    return note?.textContent || "";
  });
}

function pauseLines(): string[] {
  const activity = Array.from(document.querySelectorAll("[data-testid=activity-step]")).map((row) => row.textContent || "");
  return [...screenRows(), ...activity].filter((text) => text.startsWith("Paused"));
}

const USER: ChatMessage = { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" };
const PARTIAL = "The east pier is half listed.";
const RUNNING = "Running sleep 4; echo first-check";

function live(text: string): Live {
  return {
    phase: "reply",
    label: RUNNING,
    text,
    reasoning: "",
    replyId: "a-part",
    heardAt: Date.now(),
    steps: [{ id: "sleep", text: RUNNING, state: "running", count: 1 }],
    run: {
      id: "run-1",
      status: "running",
      started_at: "2026-10-22T18:00:00Z",
      last_activity_at: "2026-10-22T18:00:33Z",
      current_step: RUNNING,
      reason: "",
      reply_id: "a-part",
    },
  };
}

describe("rc8 one pause row", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    streamPost.mockReset();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows one pause row per frame and drops the finished tool line", async () => {
    streamPost.mockImplementation(() => new Promise<Response>(() => {}));
    paint(chat([USER]));
    setFlightForTests("bot-1", "chat-1", live(PARTIAL), true);
    await act(async () => {
      void sendMessage("bot-1", "chat-1", "and the kiln");
      await Promise.resolve();
    });
    await waitFor(() => expect(streamPost).toHaveBeenCalled());

    const frames = [
      { type: "status", text: PAUSE_TOOL_TEXT },
      { type: "status", text: PAUSE_TEXT },
      { type: "run", run: { id: "run-1", status: "running", current_step: PAUSE_TOOL_TEXT, reply_id: "a-part" } },
      { type: "status", text: RUNNING, step_id: "sleep", state: "done" },
    ];
    const expected = ["check the east pier", PARTIAL, "Paused, you sent a message", "and the kiln"];
    expect(screenRows()).toEqual(expected);
    expect(pauseLines()).toEqual(["Paused, you sent a message"]);
    expect(screen.getByTestId("run-state").textContent).toBe(PAUSE_TEXT);
    expect(screen.queryByText(PAUSE_TOOL_TEXT)).toBeNull();
    expect(screen.queryByText(RUNNING)).toBeNull();

    for (const event of frames) {
      await act(async () => {
        pushEventForTests("bot-1", "chat-1", event);
      });
      expect(screenRows()).toEqual(expected);
      expect(pauseLines()).toEqual(["Paused, you sent a message"]);
      expect(screen.getByTestId("run-state").textContent).toBe(PAUSE_TEXT);
      expect(screen.queryByText(PAUSE_TOOL_TEXT)).toBeNull();
      expect(screen.queryByText(RUNNING)).toBeNull();
    }
  });

  it("does not keep say continue after a complete answer is reloaded", () => {
    const full = "The east pier is listed.";
    paint(chat([
      USER,
      { id: "a1", role: "assistant", content: full, created_at: "2026-10-22T18:00:40Z" },
      { id: "u2", role: "user", content: "and the kiln", created_at: "2026-10-22T18:00:41Z" },
      { id: "a2", role: "assistant", content: "The kiln is warm.", created_at: "2026-10-22T18:00:50Z" },
    ], "idle"));
    expect(screen.queryByText("Paused, say continue to resume")).toBeNull();
    expect(screenRows()).toEqual(["check the east pier", full, "and the kiln", "The kiln is warm."]);
    expect(pauseLines()).toEqual([]);
  });

  it("does not turn a pause sentence into a second step", () => {
    const steps = noteActivity(
      [{ id: "sleep", text: RUNNING, state: "running", count: 1 }],
      { text: PAUSE_TOOL_TEXT, stepId: "pause", state: "running" },
    );
    expect(steps.map((step) => step.text)).toEqual([RUNNING]);
    expect(steps[0]?.state).toBe("running");
    const done = noteActivity(steps, { text: PAUSE_TEXT });
    expect(done.map((step) => step.text)).toEqual([RUNNING]);
  });
});
