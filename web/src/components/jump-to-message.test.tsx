import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import type { Chat } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "warbot";
const CHAT_ID = "warbot-chat";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
    if (path === "/api/bots") return [{ id: BOT_ID, name: "WarBot", face_color: "#6b8f71" }];
    if (path.includes("/ongoing")) return { id: CHAT_ID };
    if (path.includes("/approvals")) return [];
    if (path.includes("/read")) return {};
    if (path.includes("/messages?")) {
      return {
        messages: [
          { id: "msg-0", role: "user", content: "The oldest note." },
          { id: "msg-1", role: "assistant", content: "The second note." },
        ],
        start: 0,
        total: 4,
      };
    }
    if (path.includes(`/chats/${CHAT_ID}`)) {
      return {
        id: CHAT_ID,
        bot_id: BOT_ID,
        title: "WarBot",
        messages: [
          { id: "msg-2", role: "user", content: "The third note." },
          { id: "msg-3", role: "assistant", content: "The newest note." },
        ],
        message_count: 4,
        window_start: 2,
        run: { id: "idle", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
      } satisfies Chat;
    }
      return {};
    }),
  };
});

describe("jump to message", () => {
  let scrollIntoView: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    queryClient.clear();
    scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    useApp.setState({ botId: null, chatId: null, screen: "chat", jumpTo: "" });
  });

  afterEach(() => {
    cleanup();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat", jumpTo: "" });
  });

  it("loads older pages until the target shows up, then scrolls to it", async () => {
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    useApp.getState().setJumpTo("msg-0");
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );

    const row = await screen.findByText("The oldest note.");
    const target = row.closest("li");
    expect(target?.getAttribute("data-message-id")).toBe("msg-0");
    expect(scrollIntoView).toHaveBeenCalled();
    expect(target?.classList.contains("message-jump")).toBe(true);
    expect(useApp.getState().jumpTo).toBe("");
  });

  it("gives up when the message is not in the chat", async () => {
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    useApp.getState().setJumpTo("missing");
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("The oldest note.")).toBeTruthy();
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(useApp.getState().jumpTo).toBe("");
  });
});
