import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { noteActivity, type ActivityStep } from "@/lib/activity";
import { useApp } from "@/store";
import { resetStreamForTests, setFlightForTests } from "@/stream";
import type { Bot, Chat } from "@/types";
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
      return { id: "chat-1", bot_id: "bot-1", title: "Ada", messages: [], message_count: 0, window_start: 0 };
    }),
  };
});

function bot(): Bot {
  return {
    id: "bot-1",
    name: "Ada",
    endpoint_id: "local",
    endpoint_name: "LLM3",
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

function chat(messages: Chat["messages"]): Chat {
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
      started_at: "2026-10-15T05:00:00Z",
      last_activity_at: "2026-10-15T05:00:04Z",
      current_step: "",
      reason: "",
    },
  };
}

function feed(events: { text: string; stepId?: string; state?: string }[]): ActivityStep[] {
  return events.reduce<ActivityStep[]>((steps, event) => noteActivity(steps, event), []);
}

function renderChat(messages: Chat["messages"], steps: ActivityStep[]) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat(messages));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  setFlightForTests("bot-1", "chat-1", {
    phase: "thinking",
    label: "Writing states.html",
    text: "",
    reasoning: "",
    run: null,
    heardAt: Date.now(),
    steps,
  });
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

describe("activity log", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows each tool step once and says the reply arrived", () => {
    const steps = feed([
      { text: "Running PowerShell: Get-Content index.html", stepId: "ps", state: "running" },
      { text: "Thinking" },
      { text: "Running PowerShell: Get-Content index.html", stepId: "ps", state: "running" },
      { text: "Running PowerShell: Get-Content index.html", stepId: "ps", state: "done" },
      { text: "Reading index.html", stepId: "r1", state: "running" },
      { text: "Thinking" },
      { text: "Reading index.html", stepId: "r1", state: "done" },
      { text: "Reading index.html", stepId: "r2", state: "running" },
      { text: "Reading index.html", stepId: "r2", state: "done" },
      { text: "Reading index.html", stepId: "r3", state: "running" },
      { text: "Reading index.html", stepId: "r3", state: "done" },
      { text: "Revising", stepId: "rev", state: "running" },
      { text: "Revising", stepId: "rev", state: "running" },
      { text: "Revising", stepId: "rev", state: "done" },
      { text: "Writing states.html", stepId: "w", state: "running" },
      { text: "Waiting on model, 2s" },
      { text: "Writing states.html", stepId: "w", state: "failed" },
    ]);
    renderChat(
      [
        { id: "u", role: "user", content: "build the page", created_at: "2026-10-15T05:00:00Z" },
        { id: "a", role: "assistant", content: "The page is ready.", created_at: "2026-10-15T05:00:04Z" },
      ],
      steps,
    );
    const lines = screen.getAllByTestId("activity-step").map((row) => row.textContent);
    expect(lines).toEqual([
      "Running PowerShell: Get-Content index.html",
      "Reading index.html ×3",
      "Revising",
      "Writing states.html — failed",
    ]);
    expect(screen.getByTestId("asked-line").textContent).toBe("Asked LLM3 → got reply (4s)");
    expect(screen.queryByText(/Messaged/)).toBeNull();
  });

  it("says the ask failed when the only reply is a stop", () => {
    renderChat(
      [
        { id: "u", role: "user", content: "build the page", created_at: "2026-10-15T05:00:00Z" },
        { id: "a", role: "assistant", content: "Stopped: 500 from the model", error: true, created_at: "2026-10-15T05:00:02Z" },
      ],
      [],
    );
    expect(screen.getByTestId("asked-line").textContent).toBe("Asked LLM3 → failed: 500 from the model");
  });
});
