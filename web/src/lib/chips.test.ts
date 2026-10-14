import { describe, expect, it } from "vitest";
import { chipText } from "./chips";

describe("chipText", () => {
  it("names queued, seen, answered, and a refused message", () => {
    expect(chipText({ chip: "queued" })).toBe("Queued");
    expect(chipText({ chip: "seen", seen_step: 2 })).toBe("Seen by bot (folded in at step 2)");
    expect(chipText({ chip: "answered" })).toBe("Answered");
    expect(chipText({ chip: "refused", refuse_reason: "The queue is full, so that message was not added. The job was not restarted." })).toBe(
      "The queue is full, so that message was not added. The job was not restarted.",
    );
    expect(chipText({ chip: "unsent" })).toBe("Not sent (job stopped)");
  });
});
