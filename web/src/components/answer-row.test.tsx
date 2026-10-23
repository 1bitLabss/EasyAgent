import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
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

const PIER = "The east pier has twelve boats.";
const MARK = "The pier mark is set.";
const KILN = "The kiln note is the one that was written.";
const MODEL = `The harbor counter lives in harbor.py.

\`\`\`python
def boats():

    return 12
\`\`\`

\`\`\`yaml
ship:
  name: harbor
  count: 12
\`\`\``;
const PYTHON_BODY = "def boats():\n\n    return 12\n";
const YAML_BODY = "ship:\n  name: harbor\n  count: 12\n";

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

function chat(messages: ChatMessage[]): Chat {
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
      started_at: "2026-10-22T18:00:00Z",
      last_activity_at: "2026-10-22T18:00:53Z",
      current_step: "",
      reason: "",
      activity_started_at: "2026-10-22T18:00:00Z",
    },
  };
}

function user(text: string): ChatMessage {
  return { id: "u1", role: "user", content: text, created_at: "2026-10-22T18:00:00Z" };
}

function answer(text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id: "a1", role: "assistant", content: text, created_at: "2026-10-22T18:00:33Z", ...extra };
}

function paint(saved: string, extra: Partial<ChatMessage> = {}) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([user("Write the harbor counter into harbor.py and show the Python and the YAML."), answer(saved, extra)]));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

describe("a finished turn has an answer row", () => {
  afterEach(() => {
    cleanup();
    resetStreamForTests();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it.each([
    ["ask another bot", PIER],
    ["step cap", MARK],
    ["discarded revise", KILN],
  ])("shows the saved %s transcript in the row", (_label, saved) => {
    paint(saved);
    const rows = document.querySelectorAll("[data-role=assistant]");
    expect(rows.length).toBeGreaterThan(0);
    const bubble = Array.from(rows).map((row) => row.textContent || "").join("\n");
    expect(bubble).toContain(saved);
    expect(bubble).not.toContain("CHILD-ONLY");
    expect(document.body.textContent || "").not.toContain("Retry");
    expect(document.body.textContent || "").not.toContain("Continue");
  });

  it("renders a coding reply byte for byte with the model output", () => {
    paint(MODEL);
    const row = document.querySelector("[data-role=assistant]");
    expect(row).toBeTruthy();
    const codes = Array.from(row!.querySelectorAll("pre code")).map((node) => node.textContent || "");
    expect(codes).toEqual([PYTHON_BODY, YAML_BODY]);
    const bubble = row!.querySelector(".bubble-bot")?.textContent || "";
    expect(bubble).toContain("    return 12");
    expect(bubble).toContain("  name: harbor");
    expect(bubble).toContain("The harbor counter lives in harbor.py.");
  });

  it("does not render an empty bubble after a restart mid-turn", () => {
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
    queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([
      user("Write the harbor counter into harbor.py."),
      {
        id: "a-empty",
        role: "assistant",
        kind: "status",
        content: "Interrupted, resuming",
        created_at: "2026-10-22T18:00:33Z",
      },
      {
        id: "a-note",
        role: "assistant",
        kind: "resume-note",
        content: "Picked up where it stopped.",
        created_at: "2026-10-22T18:00:34Z",
      },
    ]));
    queryClient.setQueryData(["approvals", "bot-1"], []);
    useApp.getState().openBot("bot-1", "chat-1");
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );
    expect(document.querySelector(".bubble-bot")).toBeNull();
    expect(document.querySelector("[data-testid=thinking-panel]")).toBeNull();
    expect(document.querySelector("[data-testid=status-note]")?.textContent).toBe("Interrupted, resuming");
    expect(document.querySelector("[data-testid=interrupted-resuming]")).toBeNull();
    expect(document.querySelector("[data-testid=pickup-card]")?.textContent).toBe("Picked up where it stopped.");
  });

  it("keeps an unverified caveat off the answer text", () => {
    const words = "Done. I wrote the agenda into note.txt and that is the whole summary of this change for the record.";
    paint(words, { unverified: true });
    const bubble = document.querySelector(".bubble-bot")?.textContent || "";
    expect(bubble).toBe(words);
    expect(bubble).not.toContain("This was not verified.");
    const note = document.querySelector(".sys-line");
    expect(note?.textContent).toBe("Unverified");
  });
});
