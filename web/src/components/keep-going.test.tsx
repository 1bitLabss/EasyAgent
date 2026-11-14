import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useApp } from "@/store";
import type { Bot, Chat, Plan } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "ada";
const CHAT_ID = "ada-chat";

vi.mock("@/api", async () => {
  const { QueryClient: Client } = await import("@tanstack/react-query");
  const client = new Client({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string, options?: { json?: { on?: boolean; budget?: number } }) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/plan/keep")) {
        const on = Boolean(options?.json?.on);
        return {
          ...chat(),
          plan: on
            ? { ...plan(), keep_going: true, budget: options?.json?.budget ?? 8, continued: 0 }
            : { ...plan(), keep_going: false },
        };
      }
      if (path.includes(`/chats/${CHAT_ID}`)) return chat();
      return [];
    }),
  };
});

function bot(): Bot {
  return {
    id: BOT_ID,
    name: "Ada",
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

function plan(overrides: Partial<Plan> = {}): Plan {
  return {
    title: "Notes",
    steps: [
      { id: "s1", text: "Write the summary", state: "done" },
      { id: "s2", text: "Send it", state: "todo" },
    ],
    keep_going: false,
    continued: 0,
    budget: 0,
    ...overrides,
  };
}

function chat(withPlan?: Plan): Chat {
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Ada",
    messages: [
      { id: "a1", role: "assistant", content: "Working on it.", created_at: "2026-11-14T18:00:00Z" },
    ],
    message_count: 1,
    window_start: 0,
    run: {
      id: "run-idle",
      status: "idle",
      started_at: null,
      last_activity_at: null,
      current_step: "",
      reason: "",
    },
    plan: withPlan ?? null,
  };
}

async function paint(saved: Chat) {
  const { queryClient } = await import("@/api");
  (queryClient as QueryClient).clear();
  useApp.getState().openBot(BOT_ID, CHAT_ID);
  (queryClient as QueryClient).setQueryData(["bots"], [bot()]);
  (queryClient as QueryClient).setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
  (queryClient as QueryClient).setQueryData(["chat", BOT_ID, CHAT_ID], saved);
  (queryClient as QueryClient).setQueryData(["approvals", BOT_ID], []);
  render(
    <QueryClientProvider client={queryClient as QueryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("keep-going control", () => {
  it("shows nothing when the chat has no plan", async () => {
    await paint(chat());
    await screen.findByText("Working on it.");
    expect(screen.queryByTestId("plan-checklist")).toBeNull();
    expect(screen.queryByTestId("keep-going-button")).toBeNull();
    expect(screen.queryByTestId("keep-going-chip")).toBeNull();
  });

  it("shows a Keep going button with a plan and turns it on", async () => {
    const { api } = await import("@/api");
    await paint(chat(plan()));
    const button = await screen.findByTestId("keep-going-button");
    fireEvent.click(button);
    expect(api).toHaveBeenCalledWith(`/api/bots/${BOT_ID}/chats/${CHAT_ID}/plan/keep`, {
      method: "POST",
      json: { on: true },
    });
    await waitFor(() => expect(screen.getByTestId("keep-going-chip")).toBeTruthy());
    expect(screen.queryByTestId("keep-going-button")).toBeNull();
  });

  it("chip shows continued over budget", async () => {
    await paint(chat(plan({ keep_going: true, continued: 3, budget: 8 })));
    const chip = await screen.findByTestId("keep-going-chip");
    expect(chip.textContent).toContain("Keep going 3/8");
  });

  it("stop click turns keep-going off", async () => {
    const { api } = await import("@/api");
    await paint(chat(plan({ keep_going: true, continued: 3, budget: 8 })));
    fireEvent.click(await screen.findByTestId("keep-going-stop"));
    expect(api).toHaveBeenCalledWith(`/api/bots/${BOT_ID}/chats/${CHAT_ID}/plan/keep`, {
      method: "POST",
      json: { on: false },
    });
    await waitFor(() => expect(screen.getByTestId("keep-going-button")).toBeTruthy());
    expect(screen.queryByTestId("keep-going-chip")).toBeNull();
  });
});
