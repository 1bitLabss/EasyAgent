import { describe, expect, it } from "vitest";
import { FACE_PALETTE, STATE_COLORS } from "@/components/Face";
import { hideWhileSending, messageHasBubble, reactionWho } from "@/lib/transcript";

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
});
