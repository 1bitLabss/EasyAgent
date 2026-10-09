import { describe, expect, it } from "vitest";
import { pickOpenBot } from "./open";

describe("which bot opens", () => {
  it("uses the last bot, then the first, and stays empty when there are none", () => {
    expect(pickOpenBot([], "ada")).toBeNull();
    expect(pickOpenBot(["ada", "bea"], "bea")).toBe("bea");
    expect(pickOpenBot(["ada", "bea"], "gone")).toBe("ada");
    expect(pickOpenBot(["ada", "bea"], null)).toBe("ada");
  });
});
