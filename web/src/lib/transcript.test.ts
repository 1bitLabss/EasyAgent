import { describe, expect, it } from "vitest";
import { FACE_PALETTE, STATE_COLORS } from "@/components/Face";
import { arrangeBusy, hideWhileSending, messageHasBubble, placeLiveReply, reactionWho } from "@/lib/transcript";

describe("chat transcript", () => {
  it("does not draw an empty bubble for a reaction-only turn", () => {
    expect(messageHasBubble({ content: "", reaction: "👀" } as never)).toBe(false);
    expect(messageHasBubble({ content: "   " })).toBe(false);
    expect(messageHasBubble({ content: "hello" })).toBe(true);
    expect(messageHasBubble({ content: "", error: true })).toBe(true);
  });

  it("names who reacted and hides a live retry while the reply is still streaming", () => {
    expect(reactionWho("bot", "BOT2")).toBe("BOT2");
    expect(reactionWho("person", "BOT2")).toBe("you");
    expect(reactionWho(undefined, "BOT2")).toBe("you");
    expect(hideWhileSending({ live: true }, true)).toBe(true);
    expect(hideWhileSending({ live: true }, false)).toBe(false);
    expect(hideWhileSending({}, true)).toBe(false);
  });

  it("keeps bot colors off the state colors and splits waiting from reconnecting", () => {
    const state = new Set(Object.values(STATE_COLORS));
    expect(state.has(STATE_COLORS.waiting)).toBe(true);
    expect(STATE_COLORS.waiting).not.toBe(STATE_COLORS.reconnecting);
    for (const color of FACE_PALETTE) expect(state.has(color)).toBe(false);
  });

  it("keeps a busy message where it was saved", () => {
    const pinned = new Set<string>();
    const rows = [
      { id: "u0", role: "user", content: "start" },
      { id: "a0", role: "assistant", content: "the partial" },
      { id: "u1", role: "user", content: "also check the tests", chip: "queued" },
      { id: "a", role: "assistant", content: "done" },
    ];
    const placed = arrangeBusy(rows, pinned);
    expect(placed.head.map((row) => row.id)).toEqual(["u0", "a0", "u1", "a"]);
    expect(placed.tail).toEqual([]);
  });

  it("keeps a live reply above messages that arrived during it", () => {
    const live = { role: "assistant", content: "the partial", client_id: "live-r1" };
    const during = placeLiveReply(
      [
        { id: "u0", role: "user", content: "start" },
        { id: "u1", role: "user", content: "also this", chip: "queued" },
        { id: "u2", role: "user", content: "and this", chip: "queued" },
      ],
      live,
      "live-r1",
      "r1",
    );
    expect(during.map((row) => row.content)).toEqual(["start", "the partial", "also this", "and this"]);

    const saved = placeLiveReply(
      [
        { id: "u0", role: "user", content: "start" },
        { id: "a0", role: "assistant", content: "old partial", client_id: "live-r1" },
        { id: "u1", role: "user", content: "also this", chip: "queued" },
      ],
      { role: "assistant", content: "the partial grew", client_id: "live-r1" },
      "live-r1",
      "r1",
    );
    expect(saved.map((row) => row.content)).toEqual(["start", "the partial grew", "also this"]);

    const next = placeLiveReply(
      [
        { id: "u0", role: "user", content: "start" },
        { id: "a0", role: "assistant", content: "old partial" },
        { id: "u1", role: "user", content: "also this", chip: "seen", before_id: "a0" },
      ],
      { role: "assistant", content: "Noted also this.", client_id: "live-r2" },
      "live-r2",
      "r2",
    );
    expect(next.map((row) => row.content)).toEqual(["start", "old partial", "also this", "Noted also this."]);
  });
});
