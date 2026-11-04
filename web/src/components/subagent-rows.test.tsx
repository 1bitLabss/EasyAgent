import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "ada-bot";
const CHAT_ID = "ada-chat";
const CHILD_BOT_ID = "cedar-bot";
const CHILD_CHAT_ID = "cedar-child-chat";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [parentBot(), childBot()];
      if (path.includes("/ongoing")) return path.startsWith(`/api/bots/${CHILD_BOT_ID}`) ? null : { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      if (path === `/api/bots/${BOT_ID}/chats`) return [{ id: CHAT_ID, bot_id: BOT_ID, title: "Ada" }];
      if (path === `/api/bots/${CHILD_BOT_ID}/chats`) return [{ id: CHILD_CHAT_ID, bot_id: CHILD_BOT_ID, title: "Look up the price" }];
      if (path.includes(`/chats/${CHILD_CHAT_ID}`)) return childChat();
      if (path.includes(`/chats/${CHAT_ID}`)) return parentChat([]);
      return [];
    }),
  };
});

function parentBot(): Bot {
  return {
    id: BOT_ID,
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

function childBot(): Bot {
  return { ...parentBot(), id: CHILD_BOT_ID, name: "Cedar" };
}

function parentChat(messages: ChatMessage[], status: "idle" | "running" = "idle"): Chat {
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Ada",
    messages,
    message_count: messages.length,
    window_start: 0,
    run: {
      id: status === "running" ? "run-1" : "",
      status,
      started_at: status === "running" ? "2026-11-03T18:00:00Z" : null,
      last_activity_at: status === "running" ? "2026-11-03T18:00:30Z" : null,
      current_step: "",
      reason: "",
    },
  };
}

function childChat(): Chat {
  return {
    id: CHILD_CHAT_ID,
    bot_id: CHILD_BOT_ID,
    title: "Look up the price",
    messages: [
      { id: "c-u", role: "user", content: "Look up the price." },
      { id: "c-a", role: "assistant", content: "It is 12 credits." },
    ],
    message_count: 2,
    window_start: 0,
    run: { id: "", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
  };
}

const asked: ChatMessage = { id: "u1", role: "user", content: "Ask Cedar for the price." };
const childResult: ChatMessage = {
  id: "child-1",
  role: "assistant",
  content: "Cedar: It is 12 credits.",
  speaker_name: "Cedar",
  subagent: true,
  child_chat_id: CHILD_CHAT_ID,
};
const helperResult: ChatMessage = {
  id: "helper-1",
  role: "assistant",
  content: "Helper: the folder is clean.",
  speaker_name: "Helper",
  subagent: true,
};

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

function seedChat(messages: ChatMessage[], status: "idle" | "running" = "idle") {
  queryClient.setQueryData(["bots"], [parentBot(), childBot()]);
  queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
  queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], parentChat(messages, status));
  queryClient.setQueryData(["approvals", BOT_ID], []);
  useApp.getState().openBot(BOT_ID, CHAT_ID);
}

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("child agent rows", () => {
  it("labels a child row and opens the child chat on the child bot", async () => {
    seedChat([asked, childResult]);
    paint();

    const label = await screen.findByTestId("subagent-label");
    expect(label.textContent).toBe("Child agent");
    const button = await screen.findByTestId("open-child-chat");
    expect(button.textContent).toBe("Open child chat");

    await act(async () => {
      fireEvent.click(button);
    });
    expect(useApp.getState().botId).toBe(CHILD_BOT_ID);
    expect(useApp.getState().chatId).toBe(CHILD_CHAT_ID);
  });

  it("labels a helper row but offers no chat to open", async () => {
    seedChat([asked, helperResult]);
    paint();

    expect(await screen.findByTestId("subagent-label")).toBeTruthy();
    expect(screen.queryByTestId("open-child-chat")).toBeNull();
  });

  it("leaves a plain row unlabeled", async () => {
    seedChat([asked, { id: "a1", role: "assistant", content: "Nothing to do with children." }]);
    paint();

    expect((await screen.findAllByTestId("message-text")).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("subagent-label")).toBeNull();
    expect(screen.queryByTestId("open-child-chat")).toBeNull();
  });
});

describe("child agents working line", () => {
  it("shows while a child result is the newest row and the run is busy", async () => {
    seedChat([asked, childResult], "running");
    paint();

    const line = await screen.findByTestId("child-working");
    expect(line.textContent).toContain("Child agents are working.");
  });

  it("stays hidden once the chat is idle", async () => {
    seedChat([asked, childResult], "idle");
    paint();

    expect((await screen.findAllByTestId("message-text")).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("child-working")).toBeNull();
  });
});
