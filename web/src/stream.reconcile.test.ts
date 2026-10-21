import { describe, expect, it } from "vitest";
import { canResume, eventMatches, holdShownReply, keepLongerReply, noteSettled, resetStreamForTests, runIsFinished, type Flight } from "@/stream";
import type { Chat } from "@/types";

function flight(runId = ""): Flight {
  return {
    botId: "bot",
    chatId: "chat",
    runId,
    control: new AbortController(),
    live: { phase: "thinking", label: "Waiting on model", text: "", reasoning: "", run: null, heardAt: Date.now() },
    sending: true,
    replaced: false,
    settled: false,
  };
}

describe("server run state", () => {
  it("adopts a new run id and does not treat that run as already finished", () => {
    resetStreamForTests();
    noteSettled("bot", "chat", "run-old");
    expect(runIsFinished("bot", "chat", "running", "run-new")).toBe(false);
    const current = flight("run-old");
    expect(eventMatches({ bot_id: "bot", chat_id: "chat", run_id: "run-new", type: "status", text: "Thinking" }, current)).toBe(true);
    expect(current.runId).toBe("run-new");
    const stopped = {
      id: "chat",
      bot_id: "bot",
      title: "Chat",
      messages: [{ role: "assistant", content: "Stopped: the reply failed." }],
      run: { id: "run-old", status: "stopped", started_at: null, last_activity_at: null, current_step: "", reason: "" },
    } as Chat;
    expect(canResume(stopped, flight("run-old")).retry).toBe(true);
  });

  it("keeps the row key when a live reply is saved", () => {
    const chat = {
      id: "chat",
      bot_id: "bot",
      title: "Chat",
      messages: [{ id: "a1", role: "assistant", content: "x".repeat(4000) }],
    } as Chat;
    const held = holdShownReply(chat, { replyId: "a1", text: "x".repeat(80), reasoning: "" });
    expect(held.messages?.[0]?.content).toHaveLength(4000);
    expect(held.messages?.[0]?.client_id).toBe("live-a1");
  });

  it("does not replace a reply with a shorter copy", () => {
    const chat = {
      id: "chat",
      bot_id: "bot",
      title: "Chat",
      messages: [{ id: "a1", role: "assistant", content: "x".repeat(4000) }],
    } as Chat;
    const kept = keepLongerReply(chat, "a1", "x".repeat(80));
    expect(kept.messages?.[0]?.content).toHaveLength(4000);
    const grown = keepLongerReply(chat, "a1", "y".repeat(4001));
    expect(grown.messages?.[0]?.content).toHaveLength(4001);
  });
});
