import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import type { Bot, Chat } from "@/types";
import { ChatPane } from "./ChatPane";
import { ThinkingBox } from "./ThinkingBox";

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
      return chat();
    }),
  };
});

function bot(): Bot {
  return {
    id: "bot-1",
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

function chat(): Chat {
  return {
    id: "chat-1",
    bot_id: "bot-1",
    title: "Ada",
    messages: [
      { id: "u1", role: "user", content: "what are you doing?", chip: "queued" },
      { id: "s1", role: "assistant", kind: "status", about: "u1", content: "Still on this job. Step 2 of 3: Write the summary." },
      { id: "u2", role: "user", content: "add a footnote", chip: "seen", seen_step: 2 },
      { id: "p2", role: "assistant", kind: "pickup", about: "u2", content: "Picked up your message" },
      { id: "u3", role: "user", content: "one more", chip: "answered" },
      {
        id: "u4",
        role: "user",
        content: "eleven",
        chip: "refused",
        refuse_reason: "The queue is full, so that message was not added. The job was not restarted.",
      },
      {
        id: "u5",
        role: "user",
        content: "hold this",
        chip: "held",
        refuse_reason: "The reply failed, so this message is held. Retry to send it again.",
      },
    ],
    message_count: 6,
    window_start: 0,
    run: {
      id: "run-1",
      status: "running",
      started_at: "2026-10-08T12:00:00Z",
      last_activity_at: "2026-10-08T12:00:04Z",
      current_step: "Writing index.html",
      reason: "",
      activity_started_at: "2026-10-08T12:00:00Z",
    },
  };
}

describe("busy chips", () => {
  afterEach(() => {
    cleanup();
    queryClient.clear();
    useApp.setState({ botId: null, chatId: null, screen: "chat" });
  });

  it("shows the chips, the status note, the pickup, and the refused actions", () => {
    queryClient.setQueryData(["bots"], [bot()]);
    queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
    queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat());
    queryClient.setQueryData(["approvals", "bot-1"], []);
    useApp.getState().openBot("bot-1", "chat-1");
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPane />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("chip-queued").textContent).toBe("Queued");
    expect(screen.getByTestId("chip-seen").textContent).toBe("Seen by bot (folded in at step 2)");
    expect(screen.getByTestId("chip-answered").textContent).toBe("Answered");
    expect(screen.getByTestId("chip-refused").textContent).toContain("queue is full");
    expect(screen.getByTestId("chip-held").textContent).toContain("Retry");
    expect(screen.getByTestId("retry-held").textContent).toBe("Retry");
    expect(screen.getByTestId("status-note").textContent).toContain("Still on this job.");
    expect(screen.getByTestId("pickup-marker").textContent).toBe("Picked up your message");
    expect(screen.getByTestId("send-now").textContent).toBe("Send now");
    expect(screen.getByTestId("stop-and-send-message").textContent).toBe("Stop and send");
    expect(screen.getByTestId("activity-line").textContent).toContain("Writing index.html");
    expect(screen.getByTestId("run-state").getAttribute("data-state")).toBe("working");
    expect(screen.getByTestId("run-state").textContent).toContain("Working");
    expect(screen.getByTestId("run-state").textContent).toContain("Writing index.html");
    const box = screen.getByPlaceholderText("Add to the current job") as HTMLTextAreaElement;
    expect(box.disabled).toBe(false);
    expect(screen.getByTestId("stop-and-send").textContent).toBe("Stop and send");
    expect(screen.getByText("what are you doing?")).toBeTruthy();
    expect(screen.getByText("add a footnote")).toBeTruthy();
  });
});

describe("thinking box", () => {
  afterEach(() => cleanup());

  it("shows streamed thinking while it is still arriving", () => {
    render(<ThinkingBox text="Writing the summary next." streaming />);
    expect(screen.getByTestId("thinking-body").textContent).toContain("Writing the summary next.");
    expect(screen.getByTestId("thinking-panel").textContent).toContain("Thinking");
  });
});
