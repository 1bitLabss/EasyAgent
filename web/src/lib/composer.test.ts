import { describe, expect, it } from "vitest";
import { composerHeight } from "./composer";

describe("composerHeight", () => {
  it("keeps a floor, grows with the draft, then stops at the cap", () => {
    expect(composerHeight(10, 44, 200)).toBe(44);
    expect(composerHeight(80, 44, 200)).toBe(80);
    expect(composerHeight(500, 44, 200)).toBe(200);
  });
});
