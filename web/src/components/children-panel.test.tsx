import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { useApp } from "@/store";
import { pushEventForTests, resetStreamForTests, setFlightForTests } from "@/stream";
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
      if (path.endsWith("/child-steer")) return { answer: "The child will read it before its next step." };
      if (path.includes("/ongoing")) return path.startsWith(`/api/bots/${CHILD_BOT_ID}`) ? null : { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      if (path === `/api/bots/${BOT_ID}/chats`) return [{ id: CHAT_ID, bot_id: BOT_ID, title: "Ada" }];
      if (path === `/api/bots/${CHILD_BOT_ID}/chats`) return [{ id: CHILD_CHAT_ID, bot_id: CHILD_BOT_ID, title: "Look up the price" }];
      if (path.includes(`/chats/${CHILD_CHAT_ID}`)) return childChat();
      if (path.includes(`/chats/${CHAT_ID}`)) return parentChat([], "running");
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
      started_at: status === "running" ? "2026-11-06T18:00:00Z" : null,
      last_activity_at: status === "running" ? "2026-11-06T18:00:30Z" : null,
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

const asked: ChatMessage = { id: "u1", role: "user", content: "Ask three bots for a price." };

function seedChat(messages: ChatMessage[], status: "idle" | "running" = "running") {
  queryClient.setQueryData(["bots"], [parentBot(), childBot()]);
  queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
  queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], parentChat(messages, status));
  queryClient.setQueryData(["approvals", BOT_ID], []);
  useApp.getState().openBot(BOT_ID, CHAT_ID);
}

function startFlight() {
  act(() => {
    setFlightForTests(BOT_ID, CHAT_ID, {
      phase: "thinking",
      label: "Working",
      text: "",
      reasoning: "",
      run: {
        id: "run-1",
        status: "running",
        started_at: "2026-11-06T18:00:00Z",
        last_activity_at: "2026-11-06T18:00:30Z",
        current_step: "",
        reason: "",
      },
      heardAt: Date.now(),
    }, true);
  });
}

function childEvent(childId: string, name: string, state: string, extra: Record<string, unknown> = {}) {
  return {
    type: "child_status",
    child_id: childId,
    name,
    task_excerpt: `Task for ${name}`,
    state,
    child_chat_id: null,
    elapsed: 5,
    ...extra,
  };
}

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("live children panel", () => {
  it("shows one live row per child in a fan-out", async () => {
    seedChat([asked]);
    paint();
    startFlight();

    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c2", "Basil", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c3", "Sage", "running"));
    });

    const rows = screen.getAllByTestId("child-row");
    expect(rows.length).toBe(3);
    expect(screen.getByTestId("children-panel")).toBeTruthy();
    expect(screen.getByTestId("child-name-c2").textContent).toBe("Basil");
    for (const id of ["c1", "c2", "c3"]) {
      expect(screen.getByTestId(`child-state-${id}`).textContent).toContain("Working");
      expect(screen.getByTestId(`child-elapsed-${id}`).textContent).toContain("5s");
    }
    // The heuristic line is not needed once the server reports children.
    expect(screen.queryByTestId("child-working")).toBeNull();
  });

  it("updates a child in place and flags only the failed one", async () => {
    seedChat([asked]);
    paint();
    startFlight();

    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c2", "Basil", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c3", "Sage", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "done", { elapsed: 12 }));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c2", "Basil", "error", { elapsed: 9 }));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c3", "Sage", "done", { elapsed: 20 }));
    });

    expect(screen.getAllByTestId("child-row").length).toBe(3);
    expect(screen.getByTestId("child-state-c1").textContent).toContain("Done");
    expect(screen.getByTestId("child-state-c2").textContent).toContain("Failed");
    expect(screen.getByTestId("child-state-c3").textContent).toContain("Done");
    const failedRow = screen.getAllByTestId("child-row").find((row) => row.getAttribute("data-child-id") === "c2");
    const doneRow = screen.getAllByTestId("child-row").find((row) => row.getAttribute("data-child-id") === "c1");
    expect(failedRow?.className).toContain("bg-danger/10");
    expect(doneRow?.className).not.toContain("bg-danger");
  });

  it("opens the child chat from a live row", async () => {
    seedChat([asked]);
    paint();
    startFlight();

    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "running", { child_chat_id: CHILD_CHAT_ID }));
    });

    const button = await screen.findByTestId("open-child-chat");
    await act(async () => {
      fireEvent.click(button);
    });
    expect(useApp.getState().botId).toBe(CHILD_BOT_ID);
    expect(useApp.getState().chatId).toBe(CHILD_CHAT_ID);
  });

  it("settles running children to stopped when the run fails", async () => {
    seedChat([asked]);
    paint();
    startFlight();

    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "running"));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c2", "Basil", "done"));
      pushEventForTests(BOT_ID, CHAT_ID, { type: "error", detail: "The model stopped answering." });
    });

    const rows = screen.getAllByTestId("child-row");
    expect(rows.length).toBe(2);
    expect(screen.getAllByTestId("child-row").find((row) => row.getAttribute("data-child-id") === "c1")?.getAttribute("data-state")).toBe("stopped");
    expect(screen.getAllByTestId("child-row").find((row) => row.getAttribute("data-child-id") === "c2")?.getAttribute("data-state")).toBe("done");
  });

  it("keeps the old heuristic line when no child_status events arrive", async () => {
    seedChat([asked, { id: "child-1", role: "assistant", content: "Cedar: It is 12 credits.", subagent: true }], "running");
    paint();

    const line = await screen.findByTestId("child-working");
    expect(line.textContent).toContain("Child agents are working.");
    expect(screen.queryByTestId("children-panel")).toBeNull();
  });

  it("flags only the failed child result row red", async () => {
    seedChat(
      [
        asked,
        { id: "child-1", role: "assistant", content: "Cedar: It is 12 credits.", subagent: true, child_chat_id: CHILD_CHAT_ID },
        { id: "child-2", role: "assistant", content: "Basil: the lookup failed.", subagent: true, error: true },
      ],
      "idle",
    );
    paint();

    await screen.findAllByTestId("subagent-label");
    const bubbles = document.querySelectorAll(".message-row[data-message-id] .bubble-bot");
    const failed = document.querySelectorAll(".message-row[data-message-id] .bubble-bot.bg-danger\\/10");
    expect(bubbles.length).toBeGreaterThanOrEqual(2);
    expect(failed.length).toBe(1);
    expect(failed[0]?.textContent).toContain("Basil");
  });

  it("offers a steer line only while the child runs and posts the note", async () => {
    seedChat([asked]);
    paint();
    startFlight();

    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "running", { child_chat_id: CHILD_CHAT_ID }));
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c2", "Basil", "running"));
    });

    // Only the running child with its own chat gets the input.
    const input = await screen.findByTestId("child-steer-input");
    expect(screen.getAllByTestId("child-steer-input").length).toBe(1);
    fireEvent.change(input, { target: { value: "go faster" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("child-steer-send"));
    });
    await screen.findByTestId("child-steered");
    expect(api).toHaveBeenCalledWith(
      `/api/bots/${BOT_ID}/chats/${CHILD_CHAT_ID}/child-steer`,
      expect.objectContaining({ method: "POST", json: { text: "go faster" } }),
    );

    // When the child finishes, the steer line goes away.
    act(() => {
      pushEventForTests(BOT_ID, CHAT_ID, childEvent("c1", "Cedar", "done", { child_chat_id: CHILD_CHAT_ID, elapsed: 12 }));
    });
    expect(screen.queryByTestId("child-steer-input")).toBeNull();
    expect(screen.queryByTestId("child-steered")).toBeNull();
  });
});

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}
