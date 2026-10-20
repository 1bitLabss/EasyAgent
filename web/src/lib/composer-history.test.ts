import { describe, expect, it } from "vitest";
import { browseBackward, browseForward, deriveUserHistory, resetBrowse } from "./composer-history";

describe("composer history", () => {
  it("walks previous user lines and restores the draft", () => {
    resetBrowse("s");
    const history = deriveUserHistory([
      { role: "user", content: "first" },
      { role: "assistant", content: "ok" },
      { role: "user", content: "second" },
    ]);
    expect(history).toEqual(["second", "first"]);
    expect(browseBackward("s", "wip", history)).toBe("second");
    expect(browseBackward("s", "second", history)).toBe("first");
    expect(browseForward("s", history)?.text).toBe("second");
    expect(browseForward("s", history)).toEqual({ text: "wip", returned: true });
  });
});
