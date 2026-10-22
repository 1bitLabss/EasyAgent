import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { PAUSE_TEXT, resetStreamForTests, setFlightForTests, type Live } from "@/stream";
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
    endpoint_id: "llm3",
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
      activity_started_at: "2026-10-22T18:00:00Z",
    },
  };
}

const asked: ChatMessage = {
  id: "u1",
  role: "user",
  content: "Check the page, then ask LLM3.",
  created_at: "2026-10-22T18:00:00Z",
};

const reply: ChatMessage = {
  id: "a1",
  role: "assistant",
  content: "LLM3 finished the lookup.",
  created_at: "2026-10-22T18:00:33Z",
};

function live(patch: Partial<Live>): Live {
  return {
    phase: "thinking",
    label: "Looking",
    text: "",
    reasoning: "",
    run: null,
    heardAt: Date.now(),
    steps: [{ id: "look", text: "Looking", state: "running", count: 1 }],
    ...patch,
  };
}

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

function expectSameStatus(status: HTMLElement) {
  const nodes = document.querySelectorAll("[data-testid=live-status]");
  expect(nodes).toHaveLength(1);
  expect(nodes[0]).toBe(status);
  expect(status.id).toBe("live-status");
  expect(document.querySelectorAll("[data-testid=run-state]")).toHaveLength(1);
  expect(document.querySelectorAll("[data-testid=activity-line], [data-testid=chat-working-pill]")).toHaveLength(0);
  expect(document.querySelectorAll("[data-testid=activity-step][data-state=running]")).toHaveLength(0);
}

describe("one live status", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("keeps a single status through a tool step, an ask, more streaming, and a resync", () => {
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
    queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([asked]));
    queryClient.setQueryData(["approvals", "bot-1"], []);
    useApp.getState().openBot("bot-1", "chat-1");
    paint();
    const status = document.getElementById("live-status");
    expect(status).toBeTruthy();
    expectSameStatus(status!);
    expect(status!.getAttribute("data-live")).toBe("false");
    expect(status!.textContent).toContain("Idle");

    act(() => {
      setFlightForTests("bot-1", "chat-1", live({}), true);
    });
    expectSameStatus(status!);
    expect(status!.getAttribute("data-live")).toBe("true");
    expect(status!.textContent).toContain("Looking");
    expect(document.querySelectorAll("[data-testid=activity-step]")).toHaveLength(0);

    act(() => {
      setFlightForTests("bot-1", "chat-1", live({
        label: "Writing",
        phase: "reply",
        text: "Starting the reply.",
        steps: [{ id: "look", text: "Looking", state: "done", count: 1 }],
      }), true);
    });
    expectSameStatus(status!);
    const steps = document.querySelectorAll("[data-testid=activity-step]");
    expect(steps).toHaveLength(1);
    expect(steps[0]?.textContent).toBe("Looking");
    expect(steps[0]?.getAttribute("data-state")).toBe("done");
    expect(status!.textContent).toMatch(/Writing/);
    expect(steps[0]!.compareDocumentPosition(status!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    act(() => {
      queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([asked, reply]));
      setFlightForTests("bot-1", "chat-1", live({
        label: "Writing",
        phase: "reply",
        text: "Starting the reply. Still going.",
        steps: [{ id: "look", text: "Looking", state: "done", count: 1 }],
      }), true);
    });
    expectSameStatus(status!);
    const looking = document.querySelector("[data-testid=activity-step]");
    const askedLine = document.querySelector("[data-testid=asked-line]");
    expect(askedLine?.textContent).toBe("Asked LLM3 → got reply (33s)");
    expect(looking!.compareDocumentPosition(askedLine!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(askedLine!.compareDocumentPosition(status!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(status!.textContent).toMatch(/Writing/);

    act(() => {
      setFlightForTests("bot-1", "chat-1", live({
        label: "Writing",
        phase: "reply",
        text: "Starting the reply. Still going. More tokens.",
        chars: 80,
        steps: [{ id: "look", text: "Looking", state: "done", count: 1 }],
      }), true);
    });
    expectSameStatus(status!);
    expect(status!.textContent).toMatch(/Writing/);
    expect(document.querySelector("[data-testid=asked-line]")?.textContent).toContain("got reply");

    act(() => {
      queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([
        asked,
        reply,
        { id: "live-status", role: "assistant", kind: "status", content: "Writing, 52s" },
      ], "running"));
      setFlightForTests("bot-1", "chat-1", live({
        label: "Writing",
        phase: "reply",
        text: "Starting the reply. Still going. More tokens.",
        chars: 80,
        heardAt: Date.now(),
        steps: [{ id: "look", text: "Looking", state: "done", count: 1 }],
      }), true);
    });
    expectSameStatus(status!);
    expect(document.querySelectorAll("[data-testid=status-note]")).toHaveLength(0);
    expect(status!.textContent).toMatch(/Writing/);
    expect(document.querySelector("[data-testid=activity-step]")?.textContent).toBe("Looking");
    expect(document.querySelector("[data-testid=asked-line]")?.textContent).toContain("got reply");

    act(() => {
      setFlightForTests("bot-1", "chat-1", live({
        label: PAUSE_TEXT,
        phase: "thinking",
        text: "Starting the reply. Still going. More tokens.",
        steps: [{ id: "look", text: "Looking", state: "done", count: 1 }],
      }), true);
    });
    expectSameStatus(status!);
    expect(status!.textContent).toContain("Paused, reading your message");

    cleanup();
    paint();
    const reloaded = document.querySelectorAll("[data-testid=live-status]");
    expect(reloaded).toHaveLength(1);
    expect(document.querySelectorAll("[data-testid=run-state]")).toHaveLength(1);
    expect(reloaded[0]?.id).toBe("live-status");
  });
});
