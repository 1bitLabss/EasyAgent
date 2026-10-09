import { QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";

const reactionCss = (() => {
  const css = readFileSync(resolve("src/index.css"), "utf8");
  const start = css.indexOf(".message-row {");
  const end = css.indexOf(".composer {");
  if (start < 0 || end < start) throw new Error("reaction styles missing from index.css");
  return css.slice(start, end);
})();
import { useApp } from "@/store";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "warbot";
const CHAT_ID = "warbot-chat";
const COUNT = 60;
const ROW_PX = 480;
const VIEWPORT = 640;

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [warbot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/messages?")) return { messages: [], start: 0, total: COUNT };
      if (path.includes("/read")) return {};
      if (path.includes(`/chats/${CHAT_ID}`)) return chat();
      return [];
    }),
  };
});

function tallMessages(): ChatMessage[] {
  const thinking = `${"Looking across the whole board before answering.\n".repeat(12)}The check found nothing new.`;
  return Array.from({ length: COUNT }, (_, index) => ({
    id: `msg-${index}`,
    role: "assistant",
    content: `WarBot reply ${index}. ${"This answer stays mounted while the chat sits idle. ".repeat(8)}`,
    thinking,
    thought_seconds: 18,
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

function chat(): Chat {
  const messages = tallMessages();
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "WarBot",
    messages,
    message_count: messages.length,
    window_start: 0,
    run: {
      id: "run-idle",
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

function measuredHeight(element: HTMLElement): number {
  const rows = element.querySelectorAll("ol > li").length * ROW_PX;
  let spacers = 0;
  element.querySelectorAll<HTMLElement>("[style]").forEach((node) => {
    if (node === element) return;
    const raw = node.style.height;
    if (!raw.endsWith("px")) return;
    const value = Number.parseFloat(raw);
    if (value > 0) spacers += value;
  });
  return rows + spacers;
}

function installScrollMetrics() {
  const scrollTop = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
  const scrollHeight = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight");
  const clientHeight = Object.getOwnPropertyDescriptor(Element.prototype, "clientHeight");
  const tops = new WeakMap<Element, number>();
  let anchoring = false;

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
      if (isChatScroller(this)) return measuredHeight(this);
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
      const max = Math.max(0, measuredHeight(this) - VIEWPORT);
      const next = Math.min(Math.max(0, Number(value) || 0), max);
      const prev = tops.get(this) ?? 0;
      tops.set(this, next);
      if (next !== prev) this.dispatchEvent(new Event("scroll", { bubbles: false }));
    },
  });

  const observer = new MutationObserver(() => {
    if (anchoring) return;
    const element = document.querySelector(".overflow-y-auto");
    if (!isChatScroller(element)) return;
    const height = measuredHeight(element);
    const seen = Number(element.dataset.anchorHeight || "0");
    element.dataset.anchorHeight = String(height);
    if (!seen || height === seen) return;
    const top = element.scrollTop;
    const max = Math.max(0, height - VIEWPORT);
    const wasBottom = seen - top - VIEWPORT < 80;
    const next = Math.min(max, Math.max(0, wasBottom ? max : top + (height - seen)));
    if (next === top) return;
    anchoring = true;
    element.scrollTop = next;
    anchoring = false;
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["style"] });

  return () => {
    observer.disconnect();
    if (scrollTop) Object.defineProperty(HTMLElement.prototype, "scrollTop", scrollTop);
    else delete (HTMLElement.prototype as { scrollTop?: number }).scrollTop;
    if (scrollHeight) Object.defineProperty(HTMLElement.prototype, "scrollHeight", scrollHeight);
    if (clientHeight) Object.defineProperty(HTMLElement.prototype, "clientHeight", clientHeight);
  };
}

describe("chat row window", () => {
  let restoreScroll: (() => void) | null = null;

  beforeEach(() => {
    queryClient.clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    queryClient.setQueryData(["bots"], [warbot()]);
    queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], chat());
    queryClient.setQueryData(["approvals", BOT_ID], []);
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

  it("keeps sixty tall thinking messages mounted while the chat sits idle", async () => {
    let commits = 0;
    let countCommits = false;
    function Harness() {
      return (
        <QueryClientProvider client={queryClient}>
          <Profiler
            id="chat"
            onRender={() => {
              if (countCommits) commits += 1;
            }}
          >
            <ChatPane />
          </Profiler>
        </QueryClientProvider>
      );
    }

    render(<Harness />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const scroller = document.querySelector(".overflow-y-auto");
    expect(scroller).toBeTruthy();
    expect((scroller as HTMLElement).style.overflowAnchor).toBe("none");

    const before = [...document.querySelectorAll("ol > li")];
    expect(before).toHaveLength(COUNT);
    expect(document.body.textContent).toContain("WarBot reply 0.");
    expect(document.body.textContent).toContain("WarBot reply 59.");
    expect(document.querySelectorAll(".thinking-box")).toHaveLength(COUNT);

    const max = measuredHeight(scroller as HTMLElement) - VIEWPORT;
    expect((scroller as HTMLElement).scrollTop).toBe(max);

    const scrolls = { n: 0 };
    scroller?.addEventListener("scroll", () => {
      scrolls.n += 1;
    });
    countCommits = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    const after = [...document.querySelectorAll("ol > li")];
    expect(after).toHaveLength(COUNT);
    after.forEach((node, index) => expect(node).toBe(before[index]));
    expect(commits).toBeLessThan(40);
    expect(scrolls.n).toBe(0);
    expect((scroller as HTMLElement).scrollTop).toBe(max);
  });
});

function visibleEmoji(row: Element): string {
  const shown: string[] = [];
  row.querySelectorAll("button").forEach((button) => {
    let node: Element | null = button;
    while (node && node !== row.parentElement) {
      if (getComputedStyle(node).opacity === "0") return;
      node = node.parentElement;
    }
    shown.push(button.textContent || "");
  });
  return shown.join(" ");
}

describe("reaction picker", () => {
  beforeEach(() => {
    const style = document.createElement("style");
    style.setAttribute("data-reaction-styles", "true");
    style.textContent = reactionCss;
    document.head.appendChild(style);
    queryClient.clear();
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    const messages: ChatMessage[] = [
      { id: "plain", role: "assistant", content: "No reaction on this one." },
      { id: "loved", role: "user", content: "The one they marked.", reaction: "❤️", reaction_by: "bot" },
    ];
    queryClient.setQueryData(["bots"], [warbot()]);
    queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
    queryClient.setQueryData(["approvals", BOT_ID], []);
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], {
      id: CHAT_ID,
      bot_id: BOT_ID,
      title: "WarBot",
      messages,
      message_count: messages.length,
      window_start: 0,
      run: { id: "idle", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
    } satisfies Chat);
    vi.useFakeTimers();
  });

  afterEach(() => {
    document.querySelector("[data-reaction-styles]")?.remove();
    cleanup();
    vi.useRealTimers();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("hides an unused picker and leaves only the stored reaction pill", async () => {
    let commits = 0;
    let countCommits = false;
    render(
      <QueryClientProvider client={queryClient}>
        <Profiler
          id="reactions"
          onRender={() => {
            if (countCommits) commits += 1;
          }}
        >
          <ChatPane />
        </Profiler>
      </QueryClientProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    const rows = [...document.querySelectorAll("ol > li")];
    expect(rows).toHaveLength(2);
    const idle = rows[0];
    const marked = rows[1];

    expect(idle.querySelector("[data-testid='reaction-pill']")).toBeNull();
    expect(getComputedStyle(idle.querySelector(".reaction-picker") as Element).opacity).toBe("0");
    expect(visibleEmoji(idle)).not.toMatch(/👍|👎|❤️|👀/);

    const pill = marked.querySelector("[data-testid='reaction-pill']") as HTMLElement;
    expect(pill.textContent).toContain("❤️");
    expect(pill.textContent).toContain("WarBot");
    expect(getComputedStyle(pill).opacity).not.toBe("0");
    expect(getComputedStyle(marked.querySelector(".reaction-picker") as Element).opacity).toBe("0");
    expect(visibleEmoji(marked)).toContain("❤️");
    expect(visibleEmoji(marked)).toContain("WarBot");
    expect(visibleEmoji(marked)).not.toMatch(/👍|👎|👀/);

    countCommits = true;
    fireEvent.mouseMove(idle);
    fireEvent.mouseMove(marked);
    expect(commits).toBe(0);

    const pick = idle.querySelector(".reaction-pick") as HTMLButtonElement;
    act(() => {
      pick.focus();
    });
    expect(document.activeElement).toBe(pick);
    expect(commits).toBe(0);
    expect(reactionCss).toContain(".message-row:hover .reaction-picker");
    expect(reactionCss).toContain(".message-row:focus-within .reaction-picker");
    expect(reactionCss).toContain(".message-row:active .reaction-picker");
  });
});
