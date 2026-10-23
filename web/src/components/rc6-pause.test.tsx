import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
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

const EMPTY_JOB: ChatMessage[] = [
  { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
  { id: "u2", role: "user", content: "and the kiln", chip: "answered", created_at: "2026-10-22T18:00:40Z" },
  { id: "a2", role: "assistant", content: "The kiln is warm.", created_at: "2026-10-22T18:00:50Z" },
  { id: "paused", role: "assistant", kind: "status", content: "Paused, say continue to resume", created_at: "2026-10-22T18:00:51Z" },
];

const PARTIAL_JOB: ChatMessage[] = [
  { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
  { id: "a1", role: "assistant", content: "The east pier is half listed.", created_at: "2026-10-22T18:00:20Z" },
  { id: "note", role: "assistant", kind: "status", content: "Paused, say continue to resume", created_at: "2026-10-22T18:00:21Z" },
  { id: "u2", role: "user", content: "and the kiln", chip: "answered", created_at: "2026-10-22T18:00:40Z" },
  { id: "a2", role: "assistant", content: "The kiln is warm.", created_at: "2026-10-22T18:00:50Z" },
];

const PENDING_JOB: ChatMessage[] = [
  { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
  { id: "a1", role: "assistant", content: "The east pier is half listed.", created_at: "2026-10-22T18:00:20Z" },
  { id: "note", role: "assistant", kind: "status", content: "Paused, you sent a message", created_at: "2026-10-22T18:00:21Z" },
  { id: "u2", role: "user", content: "and the kiln", chip: "queued", created_at: "2026-10-22T18:00:40Z" },
];

describe("rc6 mid-chain pause", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows the saved rows of an eight-step job after a mid-chain message", () => {
    paint(chat(EMPTY_JOB));
    const saved = EMPTY_JOB.map((row) => row.content);
    expect(screenRows()).toEqual(saved);
    expect(screen.queryByText("Interrupted, resuming")).toBeNull();
    expect(screen.getByText("Paused, say continue to resume")).toBeTruthy();
  });

  it("keeps partial text and one continue row after the side message is answered", () => {
    paint(chat(PARTIAL_JOB));
    expect(screenRows()).toEqual(PARTIAL_JOB.map((row) => row.content));
    expect(screen.queryByText("Interrupted, resuming")).toBeNull();
    expect(screen.queryByText("Paused, you sent a message")).toBeNull();
    expect(screen.getByText("Paused, say continue to resume")).toBeTruthy();
  });

  it("shows one you-sent row while the side message is still queued", () => {
    paint(chat(PENDING_JOB, "running"));
    expect(screenRows()).toEqual(PENDING_JOB.map((row) => row.content));
    expect(screen.queryByText("Paused, say continue to resume")).toBeNull();
    expect(screen.getByText("Paused, you sent a message")).toBeTruthy();
  });

  it("does not invent Interrupted, resuming for an empty assistant", () => {
    paint(chat([
      { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
      { id: "a-empty", role: "assistant", content: "", thinking: "planning the pier", created_at: "2026-10-22T18:00:20Z" },
      { id: "u2", role: "user", content: "and the kiln", created_at: "2026-10-22T18:00:40Z" },
      { id: "a2", role: "assistant", content: "The kiln is warm.", created_at: "2026-10-22T18:00:50Z" },
    ], "idle"));
    expect(screen.queryByText("Interrupted, resuming")).toBeNull();
    expect(document.querySelector("[data-testid=interrupted-resuming]")).toBeNull();
    expect(document.querySelector("[data-testid=thinking-panel]")).toBeNull();
    expect(screenRows()).toEqual(["check the east pier", "and the kiln", "The kiln is warm."]);
  });

  it("shows Interrupted, resuming only when that sentence was saved", () => {
    paint(chat([
      { id: "u1", role: "user", content: "check the east pier", created_at: "2026-10-22T18:00:00Z" },
      { id: "a-status", role: "assistant", kind: "status", content: "Interrupted, resuming", created_at: "2026-10-22T18:00:33Z" },
      { id: "a-note", role: "assistant", kind: "resume-note", content: "Picked up where it stopped.", created_at: "2026-10-22T18:00:34Z" },
    ], "paused"));
    expect(screenRows()).toEqual(["check the east pier", "Interrupted, resuming", "Picked up where it stopped."]);
  });
});
