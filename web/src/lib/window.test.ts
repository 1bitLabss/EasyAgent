import { describe, expect, it } from "vitest";
import { approvalPollMs, keepLoadedHistory, mergeChat, retainLocalTail, shareChat, shareMessages, visibleRange, windowChat } from "./window";
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

describe("approvalPollMs", () => {
  it("asks every five seconds only while a run is active and the window is showing", () => {
    expect(approvalPollMs(true, false)).toBe(5000);
    expect(approvalPollMs(false, false)).toBe(false);
    expect(approvalPollMs(true, true)).toBe(false);
  });
});

describe("shareMessages", () => {
  it("returns the same row objects when a poll repeats the transcript", () => {
    const first = [message("m1", "Hello"), message("m2", "The reply is done.")];
    const again = [message("m1", "Hello"), message("m2", "The reply is done.")];
    const shared = shareMessages(first, again);
    expect(shared).toBe(first);
    expect(shared[1]).toBe(first[1]);
    const run = { id: "", status: "idle" as const, started_at: null, last_activity_at: null, current_step: "", reason: "" };
    const prev = chat(first, { run });
    expect(shareChat(prev, chat([message("m1", "Hello"), message("m2", "The reply is done.")], { run }))).toBe(prev);
    expect(shareMessages(first, [message("m1", "Hello"), message("m2", "Changed")])[1]).not.toBe(first[1]);
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

describe("retainLocalTail", () => {
  it("keeps a just-sent message when a poll snapshot does not have it yet", () => {
    const prev = [message("a", "Hello"), { role: "user", content: "what are you doing?", chip: "queued" }];
    const polled = [message("a", "Hello")];
    const kept = retainLocalTail(prev, polled);
    expect(kept.map((item) => item.content)).toEqual(["Hello", "what are you doing?"]);
    const fresh = [message("a", "Hello"), { id: "b", role: "user", content: "what are you doing?", chip: "queued" }];
    expect(retainLocalTail(prev, fresh).map((item) => item.id || item.content)).toEqual(["a", "b"]);
    const run = { id: "r", status: "running" as const, started_at: null, last_activity_at: null, current_step: "Thinking", reason: "" };
    const shared = shareChat(chat(prev, { run }), chat(polled, { run })) as Chat;
    expect(shared.messages.map((item) => item.content)).toEqual(["Hello", "what are you doing?"]);
  });

  it("does not duplicate a failed send when the same snapshot is merged again", () => {
    const local = { role: "user", content: "do not lose this", chip: "queued", client_id: "c-1" };
    const prev = [message("a", "Hello"), local];
    let merged = retainLocalTail(prev, [message("a", "Hello")]);
    for (let n = 0; n < 20; n += 1) {
      merged = retainLocalTail(merged, [message("a", "Hello")]);
    }
    expect(merged.filter((item) => item.content === "do not lose this")).toHaveLength(1);
    const echoed = retainLocalTail(merged, [message("a", "Hello"), { id: "s", role: "user", content: "do not lose this", client_id: "c-1", chip: "queued" }]);
    expect(echoed.filter((item) => item.client_id === "c-1" || item.content === "do not lose this")).toHaveLength(1);
  });

  it("replaces a row when its chip changes", () => {
    const first = [{ id: "u", role: "user", content: "hi", chip: "queued" }];
    const seen = [{ id: "u", role: "user", content: "hi", chip: "seen", seen_step: 2 }];
    expect(shareMessages(first, seen)[0]).toBe(seen[0]);
    expect(shareMessages(first, seen)[0]?.chip).toBe("seen");
  });
});

describe("keepLoadedHistory", () => {
  it("keeps rows already loaded above a windowed refetch and still appends a new reply", () => {
    const loaded = Array.from({ length: 150 }, (_, index) => message(`m${index}`, `Question ${index}`));
    const prev = chat(loaded, { window_start: 0, message_count: 150 });
    const tail = loaded.slice(-80);
    const refetch = keepLoadedHistory(prev, chat(tail, { window_start: 70, message_count: 150 })) as Chat;
    expect(refetch.messages).toHaveLength(150);
    expect(refetch.messages[0]?.content).toBe("Question 0");
    expect(refetch.window_start).toBe(0);

    const slid = tail.slice(1);
    slid.push(message("m150", "Question 150"));
    const appended = keepLoadedHistory(prev, chat(slid, { window_start: 71, message_count: 151 })) as Chat;
    expect(appended.messages).toHaveLength(151);
    expect(appended.messages[0]?.id).toBe("m0");
    expect(appended.messages[150]?.id).toBe("m150");
    expect(appended.window_start).toBe(0);

    const sameWindow = chat(tail, { window_start: 70, message_count: 150 });
    expect(keepLoadedHistory(sameWindow, chat(tail, { window_start: 70, message_count: 150 }))).toMatchObject({
      window_start: 70,
      message_count: 150,
    });
  });
});
