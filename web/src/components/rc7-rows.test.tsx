import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { noteActivity } from "@/lib/activity";
import { mergeChat } from "@/lib/window";
import { useApp } from "@/store";
import { resetStreamForTests, sendMessage, setFlightForTests, type Live } from "@/stream";
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
      current_step: status === "running" ? "Writing, 33s" : "",
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

const USER: ChatMessage = { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" };
const PARTIAL = "The east pier is half listed.";

function live(text: string): Live {
  return {
    phase: "reply",
    label: "Writing, 33s",
    text,
    reasoning: "",
    replyId: "a-part",
    heardAt: Date.now(),
    run: {
      id: "run-1",
      status: "running",
      started_at: "2026-10-22T18:00:00Z",
      last_activity_at: "2026-10-22T18:00:33Z",
      current_step: "Writing, 33s",
      reason: "",
      reply_id: "a-part",
    },
  };
}

describe("rc7 pause row and late answer", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    streamPost.mockReset();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("inserts the partial and the pause row in the same frame as the bubble", async () => {
    streamPost.mockImplementation(() => new Promise<Response>(() => {}));
    paint(chat([USER]));
    setFlightForTests("bot-1", "chat-1", live(PARTIAL), true);
    await act(async () => {
      void sendMessage("bot-1", "chat-1", "and the kiln");
      await Promise.resolve();
    });
    await waitFor(() => expect(streamPost).toHaveBeenCalled());
    const first = ["check the east pier", PARTIAL, "Paused, you sent a message", "and the kiln"];
    expect(screenRows()).toEqual(first);
    const bubbleAt = screenRows().indexOf("and the kiln");
    const cached = queryClient.getQueryData<Chat>(["chat", "bot-1", "chat-1"]);
    const clientId = (cached?.messages || []).find((row) => row.content === "and the kiln")?.client_id || "";
    const server = chat([
      USER,
      { id: "a-part", role: "assistant", content: PARTIAL, client_id: "live-a-part", created_at: "2026-10-22T18:00:20Z" },
      { id: "pause-server", role: "assistant", kind: "status", content: "Paused, you sent a message", created_at: "2026-10-22T18:00:21Z" },
      { id: "u2", role: "user", content: "and the kiln", chip: "queued", client_id: clientId, created_at: "2026-10-22T18:00:22Z" },
    ]);
    await act(async () => {
      queryClient.setQueryData<Chat>(["chat", "bot-1", "chat-1"], (prev) => (prev ? mergeChat(prev, server) : server));
    });
    expect(screenRows()).toEqual(first);
    expect(screenRows().indexOf("and the kiln")).toBe(bubbleAt);
    expect(screenRows().filter((row) => row === "Paused, you sent a message")).toHaveLength(1);
  });

  it("does not insert a pause row for a tool step with no text", async () => {
    streamPost.mockImplementation(() => new Promise<Response>(() => {}));
    paint(chat([USER]));
    setFlightForTests("bot-1", "chat-1", { ...live(""), label: "Reading pier.md", text: "" }, true);
    await act(async () => {
      void sendMessage("bot-1", "chat-1", "and the kiln");
      await Promise.resolve();
    });
    await waitFor(() => expect(streamPost).toHaveBeenCalled());
    expect(screenRows()).toEqual(["check the east pier", "and the kiln"]);
    expect(screen.queryByText("Paused, you sent a message")).toBeNull();
  });

  it("does not hide a saved answer under an empty frozen draft", () => {
    const saved = chat([
      USER,
      { id: "a-part", role: "assistant", content: "The east pier is listed.", client_id: "live-a-part", created_at: "2026-10-22T18:00:33Z" },
      { id: "u2", role: "user", content: "and the kiln", chip: "queued", created_at: "2026-10-22T18:00:34Z" },
    ], "running");
    paint(saved);
    setFlightForTests("bot-1", "chat-1", live(""), true, { replyId: "a-part", text: "", reasoning: "" });
    expect(screen.getByText("The east pier is listed.")).toBeTruthy();
    expect(screenRows()).toEqual(["check the east pier", "The east pier is listed.", "and the kiln"]);
  });

  it("does not say Idle while a message still says Queued", () => {
    paint(chat([
      USER,
      { id: "a-part", role: "assistant", content: "The east pier is listed.", created_at: "2026-10-22T18:00:33Z" },
      { id: "u2", role: "user", content: "and the kiln", chip: "queued", created_at: "2026-10-22T18:00:34Z" },
    ], "idle"));
    expect(screen.getByText("The east pier is listed.")).toBeTruthy();
    expect(screen.getByText("Queued")).toBeTruthy();
    expect(screen.getByTestId("run-state").textContent).not.toBe("Idle");
    expect(screenRows()).toEqual(["check the east pier", "The east pier is listed.", "and the kiln"]);
  });

  it("does not copy a pause sentence onto a tool step", () => {
    const steps = noteActivity(
      [{ id: "step-1", text: "Reading pier.md", state: "running", count: 1 }],
      { text: "Paused, you sent a message" },
    );
    expect(steps.map((step) => step.text)).toEqual(["Reading pier.md"]);
    const again = noteActivity(steps, { text: "Paused, say continue to resume", stepId: "step-2", state: "running" });
    expect(again.map((step) => step.text)).toEqual(["Reading pier.md"]);
  });
});
