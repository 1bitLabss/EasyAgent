import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "warbot";
const CHAT_ID = "long-chat";
const TOTAL = 150;
const TAIL = 80;
const ROW_PX = 40;
const VIEWPORT = 400;

vi.mock("@/api", async () => {
  const actual = await vi.importActual<typeof import("@/api")>("@/api");
  return {
    ...actual,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [warbot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      if (path.includes("/messages?")) {
        return { messages: questions().slice(0, TOTAL - TAIL), start: 0, total: TOTAL };
      }
      if (path.includes(`/chats/${CHAT_ID}`)) return tailChat();
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

function tailChat(): Chat {
  const messages = questions().slice(-TAIL);
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Long chat",
    messages,
    message_count: TOTAL,
    window_start: TOTAL - TAIL,
    run: {
      id: "idle",
      status: "idle",
      started_at: null,
      last_activity_at: null,
      current_step: "",
      reason: "",
    },
  };
}

function isChatScroller(element: Element | null): element is HTMLElement {
  return element instanceof HTMLElement && element.classList.contains("overflow-y-auto");
}

function installScrollMetrics() {
  const scrollTop = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
  const scrollHeight = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight");
  const clientHeight = Object.getOwnPropertyDescriptor(Element.prototype, "clientHeight");
  const tops = new WeakMap<Element, number>();

  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      if (isChatScroller(this)) return VIEWPORT;
      return clientHeight?.get?.call(this) ?? 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      if (isChatScroller(this)) return this.querySelectorAll("ol > li").length * ROW_PX;
      return scrollHeight?.get?.call(this) ?? 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get() {
      if (isChatScroller(this)) return tops.get(this) ?? 0;
      return scrollTop?.get?.call(this) ?? 0;
    },
    set(value: number) {
      if (!isChatScroller(this)) {
        scrollTop?.set?.call(this, value);
        return;
      }
      const max = Math.max(0, this.scrollHeight - VIEWPORT);
      const next = Math.min(Math.max(0, Number(value) || 0), max);
      const prev = tops.get(this) ?? 0;
      tops.set(this, next);
      if (next !== prev) this.dispatchEvent(new Event("scroll", { bubbles: false }));
    },
  });

  return () => {
    if (scrollTop) Object.defineProperty(HTMLElement.prototype, "scrollTop", scrollTop);
    if (scrollHeight) Object.defineProperty(HTMLElement.prototype, "scrollHeight", scrollHeight);
    if (clientHeight) Object.defineProperty(HTMLElement.prototype, "clientHeight", clientHeight);
  };
}

function firstVisibleText(scroller: HTMLElement): string {
  const rows = [...scroller.querySelectorAll("ol > li")];
  const index = Math.min(rows.length - 1, Math.floor(scroller.scrollTop / ROW_PX));
  return rows[index]?.textContent || "";
}

describe("loaded history survives a visibility change", () => {
  let restoreScroll: (() => void) | null = null;

  beforeEach(() => {
    queryClient.clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    restoreScroll = installScrollMetrics();
    vi.useFakeTimers();
  });

  afterEach(() => {
    restoreScroll?.();
    restoreScroll = null;
    cleanup();
    vi.useRealTimers();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("keeps the row count and the first visible row after load older", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const earlier = [...document.querySelectorAll("button")].find((button) => button.textContent === "Earlier messages");
    expect(earlier).toBeTruthy();
    await act(async () => {
      fireEvent.click(earlier as HTMLButtonElement);
      await vi.advanceTimersByTimeAsync(0);
    });

    const scroller = document.querySelector(".overflow-y-auto") as HTMLElement;
    expect(scroller.querySelectorAll("ol > li")).toHaveLength(TOTAL);
    expect(scroller.querySelector("ol > li")?.textContent).toContain("Question 0");
    scroller.scrollTop = 5000;
    const visible = firstVisibleText(scroller);
    const top = scroller.scrollTop;
    expect(visible).toContain("Question 125");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      window.dispatchEvent(new Event("visibilitychange"));
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
      window.dispatchEvent(new Event("visibilitychange"));
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(scroller.querySelectorAll("ol > li")).toHaveLength(TOTAL);
    expect(scroller.querySelector("ol > li")?.textContent).toContain("Question 0");
    expect(scroller.scrollTop).toBe(top);
    expect(firstVisibleText(scroller)).toBe(visible);
  });
});
