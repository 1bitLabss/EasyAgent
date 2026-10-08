import { describe, expect, it } from "vitest";
import { mergeChat, visibleRange, windowChat } from "./window";
import type { Chat, ChatMessage } from "@/types";

function message(id: string, content: string): ChatMessage {
  return { id, role: "user", content };
}

function chat(messages: ChatMessage[], extra: Partial<Chat> = {}): Chat {
  return { id: "c", bot_id: "b", title: "Chat", messages, message_count: messages.length, window_start: 0, ...extra };
}

describe("visibleRange", () => {
  it("keeps a short scroll on the first rows and a deep scroll off the top", () => {
    expect(visibleRange(0, 400, 5000)).toEqual({ start: 0, end: 16 });
    const deep = visibleRange(72 * 100, 400, 5000);
    expect(deep.start).toBeGreaterThan(80);
    expect(deep.end - deep.start).toBeLessThan(40);
    expect(visibleRange(0, 400, 0)).toEqual({ start: 0, end: 0 });
  });
});

describe("windowChat", () => {
  it("keeps a short transcript and slices a long one to the tail", () => {
    const short = chat([message("a", "one")]);
    expect(windowChat(short).messages).toHaveLength(1);
    const many = Array.from({ length: 100 }, (_, index) => message(`m${index}`, `row ${index}`));
    const sliced = windowChat(chat(many));
    expect(sliced.messages[0]?.content).toBe("row 20");
    expect(sliced.messages).toHaveLength(80);
    expect(sliced.message_count).toBe(100);
    expect(sliced.window_start).toBe(20);
  });
});

describe("mergeChat", () => {
  it("appends a new reply without dropping messages already loaded above the tail", () => {
    const older = [message("m0", "turn 0"), message("m1", "turn 1")];
    const prev = chat(older, { window_start: 0, message_count: 2 });
    const incoming = chat([...older, message("m2", "turn 2")], { message_count: 3 });
    expect(mergeChat(prev, incoming).messages.map((item) => item.id)).toEqual(["m0", "m1", "m2"]);

    const history = chat(
      [message("old", "from earlier"), message("tail", "recent")],
      { window_start: 40, message_count: 42 },
    );
    const full = Array.from({ length: 42 }, (_, index) => message(index === 40 ? "old" : index === 41 ? "tail" : `x${index}`, `row ${index}`));
    full.push(message("new", "just now"));
    const merged = mergeChat(history, chat(full, { message_count: 43, window_start: 0 }));
    expect(merged.messages.map((item) => item.id)).toEqual(["old", "tail", "new"]);
    expect(merged.window_start).toBe(40);
    expect(merged.message_count).toBe(43);
  });
});
