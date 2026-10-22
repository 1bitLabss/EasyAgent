import { QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, cleanup, render } from "@testing-library/react";
import { chromium } from "playwright";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: CHAT_ID };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      return chat();
    }),
  };
});

import { queryClient } from "@/api";
import { resetStreamForTests, setFlightForTests, type Live } from "@/stream";
import { useApp } from "@/store";
import type { Bot, Chat, Run } from "@/types";
import { ChatPane } from "./ChatPane";

const BOT_ID = "hl-bot";
const CHAT_ID = "hl-chat";
const REPLY = [
  "Here is the function.",
  "",
  "```python",
  "def add(left, right):",
  "    return left + right",
  "```",
  "",
  "Call it with two numbers.",
].join("\n");

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

function idleRun(): Run {
  return {
    id: "",
    status: "idle",
    started_at: null,
    last_activity_at: null,
    current_step: "",
    reason: "",
  };
}

function chat(): Chat {
  return {
    id: CHAT_ID,
    bot_id: BOT_ID,
    title: "Ada",
    messages: [
      { id: "u1", client_id: "u1", role: "user", content: "Write a function." },
      { id: "older", client_id: "older", role: "assistant", content: "An earlier answer stays on this row." },
      { id: "a1", client_id: "live-a1", role: "assistant", content: REPLY },
      { id: "u2", client_id: "u2", role: "user", content: "Thanks, leave this row where it is." },
    ],
    message_count: 4,
    window_start: 0,
    run: idleRun(),
  };
}

function liveReply(phase: Live["phase"]): Live {
  return {
    phase,
    label: "Writing",
    text: REPLY,
    reasoning: "",
    replyId: "a1",
    chars: REPLY.length,
    heardAt: Date.now(),
    // A running server run would make the page attach to /live and replace this flight.
    run: null,
  };
}

function rowNodes(root: ParentNode): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>("ol > li.message-row")];
}

describe("highlight arrives when a reply finishes", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("paints hljs spans within 500ms and leaves existing rows where they are", async () => {
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", BOT_ID], { id: CHAT_ID });
    queryClient.setQueryData(["chat", BOT_ID, CHAT_ID], chat());
    queryClient.setQueryData(["approvals", BOT_ID], []);
    useApp.getState().openBot(BOT_ID, CHAT_ID);
    setFlightForTests(BOT_ID, CHAT_ID, liveReply("reply"));

    const view = render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );

    const list = view.container.querySelector("ol");
    expect(list).toBeTruthy();
    const beforeRows = rowNodes(view.container);
    expect(beforeRows.map((row) => row.dataset.messageId)).toEqual(["u1", "older", "a1", "u2"]);
    expect(view.container.querySelectorAll("code.hljs span").length).toBe(0);
    const plain = list?.outerHTML || "";

    const started = performance.now();
    act(() => {
      setFlightForTests(BOT_ID, CHAT_ID, liveReply("stopped"));
    });

    let spans = view.container.querySelectorAll("code.hljs span").length;
    const deadline = started + 500;
    while (spans === 0 && performance.now() < deadline) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 16));
      });
      spans = view.container.querySelectorAll("code.hljs span").length;
    }
    const latency = performance.now() - started;
    expect(spans).toBeGreaterThan(0);
    expect(latency).toBeLessThan(500);

    const afterRows = rowNodes(view.container);
    expect(afterRows.map((row) => row.dataset.messageId)).toEqual(["u1", "older", "a1", "u2"]);
    expect(view.container.querySelector("ol")).toBe(list);
    afterRows.forEach((row, index) => expect(row).toBe(beforeRows[index]));

    const finished = view.container.querySelector("ol")?.outerHTML || "";
    const jump = await rowJumpPx(plain, finished);
    expect(jump.max).toBeLessThanOrEqual(1);
    console.info(`highlight latency ${latency.toFixed(1)}ms, row jump ${jump.max.toFixed(2)}px`);
  });
});

async function rowJumpPx(beforeHtml: string, afterHtml: string): Promise<{ max: number }> {
  const css = [
    readFileSync(resolve("src/index.css"), "utf8"),
    readFileSync(resolve("node_modules/highlight.js/styles/github.css"), "utf8"),
    '[data-testid="transcript"]{display:flex;flex-direction:column;gap:16px;width:760px}',
  ].join("\n");
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    await page.setContent(`<!doctype html><html><head><style>${css}</style></head><body>${beforeHtml}</body></html>`, {
      waitUntil: "load",
    });
    const read = () =>
      page.evaluate(() =>
        [...document.querySelectorAll<HTMLElement>(".message-row")].map((row) => ({
          id: row.dataset.messageId || "",
          top: row.getBoundingClientRect().top,
        })),
      );
    const before = await read();
    await page.evaluate((html) => {
      document.body.innerHTML = html;
    }, afterHtml);
    const after = await read();
    let max = 0;
    for (const row of before) {
      const next = after.find((item) => item.id === row.id);
      if (!next) continue;
      max = Math.max(max, Math.abs(next.top - row.top));
    }
    return { max };
  } finally {
    await browser.close();
  }
}
