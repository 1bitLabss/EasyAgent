import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { pushEventForTests, resetStreamForTests, setFlightForTests } from "@/stream";
import type { Approval } from "@/components/ApprovalCard";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

let approvals: Approval[] = [];

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
      if (path.includes("/approvals")) return approvals;
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

function chat(messages: ChatMessage[], status: "idle" | "running" = "idle"): Chat {
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

function card(): Approval {
  return {
    id: "card-1",
    bot_id: "bot-1",
    tier: "ask",
    rule: "routine",
    why: "Save this routine?",
    detail: "Morning check\nWeekdays at 8:00 AM",
    offer_always: false,
    proposal: { kind: "routine" },
  };
}

function paint(saved: Chat) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], saved);
  queryClient.setQueryData(["approvals", "bot-1"], approvals);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

const LONGER = "Checking the east pier. Then the kiln, the harbor, and eight tool logs that were not saved.";

function chain(): ChatMessage[] {
  return [
    { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
    { id: "a1", role: "assistant", content: "Checking the east pier.", interrupted: true, created_at: "2026-10-22T18:00:20Z" },
    { id: "u2", role: "user", content: "and the kiln", chip: "answered", created_at: "2026-10-22T18:00:30Z" },
    { id: "a2", role: "assistant", content: "The pier is clear.", created_at: "2026-10-22T18:00:53Z" },
  ];
}

describe("rc5 routine card and transcript", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    approvals = [];
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows a routine card from the event stream, and again after a reload", async () => {
    approvals = [];
    paint(chat([{ id: "u1", role: "user", content: "how about cron jobs?", created_at: "2026-10-22T18:00:00Z" }]));
    await waitFor(() => expect(screen.queryByTestId("approval-card")).toBeNull());
    act(() => {
      setFlightForTests("bot-1", "chat-1", {
        phase: "reply",
        label: "Writing",
        text: "A routine can do that.",
        reasoning: "",
        run: { id: "run-1", status: "running", started_at: "2026-10-22T18:00:00Z", last_activity_at: "2026-10-22T18:00:01Z", current_step: "Writing", reason: "" },
        heardAt: Date.now(),
        replyId: "a1",
      }, true);
      pushEventForTests("bot-1", "chat-1", {
        type: "done",
        bot_id: "bot-1",
        chat_id: "chat-1",
        chat: chat([
          { id: "u1", role: "user", content: "how about cron jobs?", created_at: "2026-10-22T18:00:00Z" },
          { id: "a1", role: "assistant", content: "A routine can do that.", created_at: "2026-10-22T18:00:02Z" },
        ]),
        approvals: [card()],
      });
    });
    expect(await screen.findByTestId("approval-card")).toBeTruthy();
    expect(screen.getByTestId("approval-card").textContent).toContain("Save this routine?");

    act(() => {
      pushEventForTests("bot-1", "chat-1", {
        type: "approval",
        bot_id: "bot-1",
        chat_id: "chat-1",
        cards: [card()],
      });
    });
    expect(screen.getByTestId("approval-card").textContent).toContain("Confirm this routine");

    cleanup();
    resetStreamForTests();
    queryClient.clear();
    approvals = [card()];
    paint(chat([{ id: "u1", role: "user", content: "how about cron jobs?", created_at: "2026-10-22T18:00:00Z" }]));
    expect(await screen.findByTestId("approval-card")).toBeTruthy();
    expect(screen.getByTestId("approval-card").textContent).toContain("Save this routine?");
  });

  it("shows the saved transcript, not the longer draft or a step summary", async () => {
    const saved = chat(chain());
    approvals = [];
    paint(saved);
    const steps = Array.from({ length: 8 }, (_, index) => ({
      id: `s${index}`,
      text: `Reading pier ${index}`,
      state: "done" as const,
      count: 1,
    }));
    act(() => {
      setFlightForTests("bot-1", "chat-1", {
        phase: "thinking",
        label: "Reading pier 7",
        text: "",
        reasoning: "",
        run: { id: "run-1", status: "running", started_at: "2026-10-22T18:00:00Z", last_activity_at: "2026-10-22T18:00:40Z", current_step: "Reading pier 7", reason: "" },
        heardAt: Date.now(),
        replyId: "a1",
        steps,
      }, true, { replyId: "a1", text: LONGER, reasoning: "" });
      pushEventForTests("bot-1", "chat-1", {
        type: "done",
        bot_id: "bot-1",
        chat_id: "chat-1",
        chat: saved,
      });
    });
    await waitFor(() => {
      const texts = Array.from(document.querySelectorAll("[data-testid=message-text]")).map((node) => node.textContent);
      expect(texts).toEqual(chain().map((row) => row.content));
    });
    expect(document.body.textContent || "").not.toContain(LONGER);
    expect(screen.queryByTestId("activity-summary")).toBeNull();
    expect(screen.queryByText(/Used \d+ steps/)).toBeNull();
    expect(screen.getByText("Answered")).toBeTruthy();
    expect(screen.getAllByTestId("paused-mark").length).toBeGreaterThan(0);
  });
});
