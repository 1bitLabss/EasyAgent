import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useUnreadWatch } from "@/lib/livechat";
import { useApp } from "@/store";
import type { Bot, Chat, ChatMessage, Unread } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "warbot";
const CHAT_ID = "long-chat";
const TOTAL = 150;
const TAIL = 80;

let posted = false;

vi.mock("@/api", async () => {
  const actual = await vi.importActual<typeof import("@/api")>("@/api");
  return {
    ...actual,
    api: vi.fn(async (path: string) => {
      if (path === "/api/unread") return unreadBody();
      if (path === "/api/bots") return [warbot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/read")) return {};
      if (path.includes(`/chats/${CHAT_ID}`)) return posted ? postedChat() : tailChat();
      return [];
    }),
  };
});

function questions(): ChatMessage[] {
  return Array.from({ length: TOTAL }, (_, index) => ({
    id: `q${index}`,
    role: "user" as const,
    content: `Question ${index}`,
  }));
}

function warbot(): Bot {
  return {
    id: BOT_ID,
    name: "WarBot",
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

function idleRun(): Chat["run"] {
  return {
    id: "idle",
    status: "idle",
    started_at: null,
    last_activity_at: null,
    current_step: "",
    reason: "",
  };
}

function tailChat(): Chat {
  const messages = questions().slice(-TAIL);
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Long chat",
    messages,
    message_count: TOTAL,
    window_start: TOTAL - TAIL,
    updated_at: "t1",
    run: idleRun(),
  };
}

function postedChat(): Chat {
  const all = [...questions(), { id: "other", role: "assistant" as const, content: "Sent from the other tab" }];
  const messages = all.slice(-TAIL);
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Long chat",
    messages,
    message_count: all.length,
    window_start: all.length - messages.length,
    updated_at: "t2",
    run: idleRun(),
  };
}

function unreadBody(): Unread {
  if (!posted) {
    return {
      total: 0,
      chats: [],
      revisions: [{ bot_id: BOT_ID, chat_id: CHAT_ID, message_count: TOTAL, updated_at: "t1" }],
      rooms: [],
    };
  }
  return {
    total: 1,
    chats: [{ bot_id: BOT_ID, chat_id: CHAT_ID, unread: 1 }],
    revisions: [{ bot_id: BOT_ID, chat_id: CHAT_ID, message_count: TOTAL + 1, updated_at: "t2" }],
    rooms: [],
  };
}

function loadedChat(): Chat {
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Long chat",
    messages: questions(),
    message_count: TOTAL,
    window_start: 0,
    updated_at: "t1",
    run: idleRun(),
  };
}

function Watch() {
  useUnreadWatch();
  return null;
}

describe("a turn from another client appears on the unread poll", () => {
  beforeEach(() => {
    posted = false;
    queryClient.clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], loadedChat());
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows the new turn within one unread poll and keeps loaded history", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <Watch />
        <ChatPane />
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });

    const scroller = document.querySelector(".overflow-y-auto") as HTMLElement;
    expect(scroller.querySelectorAll("ol > li")).toHaveLength(TOTAL);
    expect(scroller.textContent).toContain("Question 0");
    expect(scroller.textContent).not.toContain("Sent from the other tab");

    posted = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(scroller.querySelectorAll("ol > li")).toHaveLength(TOTAL + 1);
    expect(scroller.textContent).toContain("Question 0");
    expect(scroller.textContent).toContain("Sent from the other tab");
  });
});
