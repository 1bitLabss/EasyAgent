import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests, sendMessage } from "@/stream";
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

function chat(messages: ChatMessage[], status: "idle" | "running" | "paused" = "paused"): Chat {
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
      last_activity_at: "2026-10-22T18:00:53Z",
      current_step: "",
      reason: "",
    },
  };
}

const PAUSED: ChatMessage[] = [
  { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
  { id: "u2", role: "user", content: "and the kiln", chip: "answered", created_at: "2026-10-22T18:00:40Z" },
  { id: "a2", role: "assistant", content: "The kiln is warm.", created_at: "2026-10-22T18:00:50Z" },
  { id: "paused", role: "assistant", kind: "status", content: "Paused, say continue to resume", created_at: "2026-10-22T18:00:51Z" },
];

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

function sse(event: Record<string, unknown>): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

describe("rc7 continue stays in order", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    streamPost.mockReset();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows the typed word above the resumed answer, and the same rows after reload", async () => {
    let release: (response: Response) => void = () => {};
    let push: (text: string) => void = () => {};
    let close: () => void = () => {};
    streamPost.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );
    paint(chat(PAUSED));
    expect(screenRows()).toEqual(PAUSED.map((row) => row.content));

    let pending: Promise<unknown> = Promise.resolve();
    await act(async () => {
      pending = sendMessage("bot-1", "chat-1", "continue");
      await Promise.resolve();
    });
    await waitFor(() => expect(streamPost).toHaveBeenCalled());
    const typed = ["check the east pier", "and the kiln", "The kiln is warm.", "continue"];
    expect(screenRows()).toEqual(typed);
    expect(screen.queryByText("Paused, say continue to resume")).toBeNull();
    expect(screen.queryByText("Resume the checklist where it stopped. Do not start over.")).toBeNull();
    expect(screen.queryByText("Interrupted, resuming")).toBeNull();

    const sent = streamPost.mock.calls[0]?.[1] as { content?: string; client_id?: string };
    expect(sent.content).toBe("continue");
    const clientId = sent.client_id || "";
    const body = new ReadableStream({
      start(controller) {
        const encoder = new TextEncoder();
        push = (text) => controller.enqueue(encoder.encode(text));
        close = () => controller.close();
      },
    });
    await act(async () => {
      release(new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }));
      await Promise.resolve();
    });

    await act(async () => {
      push(sse({
        type: "user",
        bot_id: "bot-1",
        chat_id: "chat-1",
        message: { id: "paused", role: "user", content: "continue", client_id: clientId },
      }));
      await Promise.resolve();
    });
    expect(screenRows()).toEqual(typed);

    await act(async () => {
      push(sse({
        type: "delta",
        text: "Picking up the pier.",
        bot_id: "bot-1",
        chat_id: "chat-1",
        run_id: "run-2",
      }));
    });
    const during = [...typed, "Picking up the pier."];
    await waitFor(() => expect(screenRows()).toEqual(during));
    expect(screenRows().indexOf("continue")).toBeLessThan(screenRows().indexOf("Picking up the pier."));

    const saved = chat(
      [
        PAUSED[0],
        PAUSED[1],
        PAUSED[2],
        { id: "paused", role: "user", content: "continue", client_id: clientId, created_at: "2026-10-22T18:00:51Z" },
        { id: "a3", role: "assistant", content: "Picking up the pier.", created_at: "2026-10-22T18:01:10Z" },
      ],
      "idle",
    );
    await act(async () => {
      push(sse({ type: "done", bot_id: "bot-1", chat_id: "chat-1", chat: saved }));
      close();
      await pending;
    });
    const finished = saved.messages.map((row) => row.content);
    expect(screenRows()).toEqual(finished);
    expect(screen.queryByText("Resume the checklist where it stopped. Do not start over.")).toBeNull();

    resetStreamForTests();
    await act(async () => {
      queryClient.setQueryData(["chat", "bot-1", "chat-1"], saved);
    });
    expect(screenRows()).toEqual(finished);
  });
});
